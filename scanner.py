"""
Market Scanner
==============
Mencari koin-koin dengan trend terbaik dari Binance Futures.
Multi-layer filtering: volume → volatilitas → spread → blacklist.
"""

import ccxt
import math
from runtime_config import config
from logger_setup import logger


class MarketScanner:
    """Scanner pasar untuk menemukan koin trending."""
    
    def __init__(self, exchange):
        self.exchange = exchange
        self.last_error = None
        self.last_universe_count = 0
        self.last_top_count = 0

    @staticmethod
    def _is_crypto_perpetual(market):
        """Fail-closed: hanya kontrak USD-M dengan metadata underlying crypto."""
        if not getattr(config, "CRYPTO_ONLY_SCANNER", True):
            return True
        info = market.get("info") or {}
        underlying_type = str(info.get("underlyingType") or "").upper()
        allowed = {str(v).upper() for v in config.CRYPTO_UNDERLYING_TYPES}
        return underlying_type in allowed
    
    def scan(self):
        """
        Scan market dan return list koin terbaik untuk trading.
        
        Returns:
            list[dict]: List koin yang lolos filter, sorted by score.
        """
        logger.debug("🔍 Scanning market untuk koin trending...")
        self.last_error = None
        self.last_universe_count = 0
        self.last_top_count = 0
        
        try:
            # Layer 1: Ambil semua USDT perpetual futures
            markets = self.exchange.load_markets()
            usdt_perps = [
                s for s in markets
                if s.endswith(":USDT")
                and markets[s].get("active", True)
                and markets[s].get("type") == "swap"
                and self._is_crypto_perpetual(markets[s])
            ]
            self.last_universe_count = len(usdt_perps)
            
            # Layer 2: Fetch tickers dan filter by volume
            tickers = self.exchange.fetch_tickers(usdt_perps)
            
            sorted_tickers = sorted(
                tickers.values(),
                key=lambda t: t.get("quoteVolume") or 0,
                reverse=True
            )
            
            top_tickers = sorted_tickers[:config.SCANNER_TOP_N]
            self.last_top_count = len(top_tickers)
            # USD-M 24h ticker tidak membawa bid/ask. Ambil best quote dari
            # endpoint bookTicker, satu permintaan bulk untuk top market.
            quotes = self.exchange.fetch_bids_asks(
                [ticker["symbol"] for ticker in top_tickers]
            ) if top_tickers else {}
            if top_tickers and not quotes:
                raise RuntimeError("Book ticker Binance kosong; spread tidak dapat diverifikasi")
            
            # Layer 3: Multi-filter
            candidates = []
            
            for ticker in top_tickers:
                symbol = ticker["symbol"]
                quote = quotes.get(symbol) or {}
                
                # Skip blacklisted coins
                base_symbol = symbol.replace(":USDT", "")
                if base_symbol in config.BLACKLIST_COINS:
                    continue
                
                # Liquidity and quoted spread must be observable and finite.
                try:
                    quote_volume = float(ticker.get("quoteVolume") or 0)
                    bid = float(quote.get("bid") or 0)
                    ask = float(quote.get("ask") or 0)
                    change_pct = abs(float(ticker.get("percentage") or 0))
                except (TypeError, ValueError):
                    continue
                if not all(math.isfinite(v) for v in (quote_volume, bid, ask, change_pct)):
                    continue
                if quote_volume < config.MIN_QUOTE_VOLUME_USDT or bid <= 0 or ask <= bid:
                    continue

                # Filter: minimum price change 24h (volatilitas)
                if change_pct < config.MIN_24H_CHANGE_PERCENT:
                    continue
                
                # Filter: spread check
                spread_pct = ((ask - bid) / bid) * 100
                if spread_pct > config.MAX_SPREAD_PERCENT:
                    continue
                
                # Change 24h remains an eligibility check, not a momentum bonus:
                # ranking a larger 24h move higher chased extended markets.
                volume_score = min(quote_volume / 1e9, 1.0) * 70
                spread_score = max(0, (config.MAX_SPREAD_PERCENT - spread_pct) / config.MAX_SPREAD_PERCENT) * 30
                
                total_score = volume_score + spread_score
                
                candidates.append({
                    "symbol": symbol,
                    "price": ticker.get("last", 0),
                    "quote_volume": quote_volume,
                    "change_24h": ticker.get("percentage", 0),
                    "spread_pct": round(spread_pct, 4),
                    "bid": bid,
                    "ask": ask,
                    "scan_score": round(total_score, 2),
                })
            
            candidates.sort(key=lambda x: x["scan_score"], reverse=True)
            logger.debug(f"Scan selesai: {len(candidates)} koin lolos filter.")
            return candidates
            
        except Exception as e:
            self.last_error = str(e)
            logger.error(f"❌ Error scanning market: {e}")
            return []
