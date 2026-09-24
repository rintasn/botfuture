"""
Market Scanner
==============
Mencari koin-koin dengan trend terbaik dari Binance Futures.
Multi-layer filtering: volume → volatilitas → spread → blacklist.
"""

import ccxt
import config
from logger_setup import logger


class MarketScanner:
    """Scanner pasar untuk menemukan koin trending."""
    
    def __init__(self, exchange):
        self.exchange = exchange

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
            
            # Layer 2: Fetch tickers dan filter by volume
            tickers = self.exchange.fetch_tickers(usdt_perps)
            
            sorted_tickers = sorted(
                tickers.values(),
                key=lambda t: t.get("quoteVolume") or 0,
                reverse=True
            )
            
            top_tickers = sorted_tickers[:config.SCANNER_TOP_N]
            
            # Layer 3: Multi-filter
            candidates = []
            
            for ticker in top_tickers:
                symbol = ticker["symbol"]
                
                # Skip blacklisted coins
                base_symbol = symbol.replace(":USDT", "")
                if base_symbol in config.BLACKLIST_COINS:
                    continue
                
                # Filter: minimum price change 24h (volatilitas)
                change_pct = abs(ticker.get("percentage") or 0)
                if change_pct < config.MIN_24H_CHANGE_PERCENT:
                    continue
                
                # Filter: spread check
                bid = ticker.get("bid") or 0
                ask = ticker.get("ask") or 0
                spread_pct = 0
                if bid > 0 and ask > 0:
                    spread_pct = ((ask - bid) / bid) * 100
                    if spread_pct > config.MAX_SPREAD_PERCENT:
                        continue
                
                # Hitung skor berdasarkan kombinasi volume & volatilitas
                volume_score = min(ticker.get("quoteVolume", 0) / 1e9, 1.0) * 50
                volatility_score = min(change_pct / 10.0, 1.0) * 30
                spread_score = 20 if spread_pct == 0 else max(0, (config.MAX_SPREAD_PERCENT - spread_pct) / config.MAX_SPREAD_PERCENT) * 20
                
                total_score = volume_score + volatility_score + spread_score
                
                candidates.append({
                    "symbol": symbol,
                    "price": ticker.get("last", 0),
                    "quote_volume": ticker.get("quoteVolume", 0),
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
            logger.error(f"❌ Error scanning market: {e}")
            return []
