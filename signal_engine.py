"""
Signal Engine - Institutional Trend-Pullback & Liquidity Rejection (TPLR)
=========================================================================
Algoritma trading kuantitatif berbasis Price Action, Dynamic Value Zones (EMA 21/55),
Macro Trend Filter (1H), dan Rejection Wick Analysis.

Prinsip:
1. Macro Trend Anchor (1H): Hanya ambil posisi searah tren besar.
2. Value Zone Pullback (15m): Masuk saat harga diskon menyentuh area EMA 21/55.
3. Price Action Rejection: Konfirmasi adanya ekor penolakan (wick defense) atau engulfing.
4. Volume & RSI Sweet Spot: Volume buyer/seller terkonfirmasi dan RSI punya ruang gerak.
"""

import time
import pandas as pd
import ta as ta_lib
from runtime_config import config
from logger_setup import logger


class SignalEngine:
    """Engine analisis teknikal institusional berbasis Trend-Pullback & Rejection."""
    
    def __init__(self, exchange):
        self.exchange = exchange
        self._btcdom_cache = {}
        self._btc_market_cache = {}
    
    def fetch_candles(self, symbol, timeframe, limit=None):
        """Fetch OHLCV candles dari exchange."""
        limit = limit or config.CANDLE_FETCH_LIMIT
        try:
            bars = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            df = pd.DataFrame(
                bars,
                columns=["timestamp", "open", "high", "low", "close", "volume"]
            )
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            return df
        except Exception as e:
            logger.error(f"❌ Gagal fetch candles {symbol} {timeframe}: {e}")
            return pd.DataFrame()
    
    def calculate_indicators(self, df):
        """Hitung EMA 21, EMA 55, EMA 200, RSI, ATR, dan Volume SMA."""
        if df.empty or len(df) < 55:
            return df
        
        # Exponential Moving Averages (Value Zones)
        df["ema_21"] = ta_lib.trend.ema_indicator(df["close"], window=config.EMA_FAST)
        df["ema_55"] = ta_lib.trend.ema_indicator(df["close"], window=config.EMA_SLOW)
        
        if len(df) >= config.EMA_TREND:
            df["ema_200"] = ta_lib.trend.ema_indicator(df["close"], window=config.EMA_TREND)
        else:
            df["ema_200"] = df["ema_55"]
        
        # RSI & ATR
        df["rsi"] = ta_lib.momentum.rsi(df["close"], window=config.RSI_PERIOD)
        df["atr"] = ta_lib.volatility.average_true_range(df["high"], df["low"], df["close"], window=14)
        df["adx"] = ta_lib.trend.adx(
            df["high"], df["low"], df["close"], window=config.ADX_PERIOD
        )
        
        # Volume SMA
        df["vol_sma"] = ta_lib.trend.sma_indicator(df["volume"], window=config.VOLUME_SMA_PERIOD)
        
        return df

    def calculate_initial_stop(self, symbol, side, entry_price, dataframe=None):
        """Bangun hard-stop awal dari ATR, EMA55, dan swing structure 15m."""
        try:
            df = dataframe
            if df is None:
                df = self.fetch_candles(symbol, config.TRADING_TIMEFRAME)
                if df.empty:
                    return None
                df = self.calculate_indicators(df)
            if df.empty or len(df) < 3 or entry_price <= 0:
                return None

            # Candle terakhir dapat masih berjalan. Struktur menggunakan candle
            # yang sudah closed agar stop tidak berubah karena wick sementara.
            closed = df.iloc[:-1] if len(df) > 3 else df
            last = closed.iloc[-1]
            atr = float(last.get("atr", 0) or 0)
            ema55 = float(last.get("ema_55", 0) or 0)
            if atr <= 0 or ema55 <= 0:
                return None

            lookback = max(3, int(getattr(config, "INITIAL_STOP_SWING_LOOKBACK", 20)))
            window = closed.tail(lookback)
            swing_low = float(window["low"].min())
            swing_high = float(window["high"].max())
            atr_mult = float(getattr(config, "INITIAL_STOP_ATR_MULTIPLIER", 1.5))
            atr_buffer = float(getattr(config, "INITIAL_STOP_ATR_BUFFER", 0.25))
            min_pct = float(getattr(config, "INITIAL_STOP_MIN_DISTANCE_PERCENT", 1.0))
            max_pct = float(getattr(config, "INITIAL_STOP_MAX_DISTANCE_PERCENT", 5.0))
            if min_pct <= 0 or max_pct < min_pct:
                raise ValueError("Konfigurasi jarak initial stop tidak valid")

            if side.lower() == "long":
                structure = min(ema55, swing_low)
                structure_stop = structure - (atr_buffer * atr)
                atr_stop = entry_price - (atr_mult * atr)
                raw_stop = min(structure_stop, atr_stop)
                lower = entry_price * (1 - max_pct / 100)
                upper = entry_price * (1 - min_pct / 100)
                stop_price = max(lower, min(upper, raw_stop))
            else:
                structure = max(ema55, swing_high)
                structure_stop = structure + (atr_buffer * atr)
                atr_stop = entry_price + (atr_mult * atr)
                raw_stop = max(structure_stop, atr_stop)
                lower = entry_price * (1 + min_pct / 100)
                upper = entry_price * (1 + max_pct / 100)
                stop_price = min(upper, max(lower, raw_stop))

            distance_pct = abs(entry_price - stop_price) / entry_price * 100
            return {
                "stop_price": float(stop_price),
                "distance_pct": float(distance_pct),
                "atr": atr,
                "ema55": ema55,
                "swing_low": swing_low,
                "swing_high": swing_high,
                "structure_price": structure,
                "timeframe": config.TRADING_TIMEFRAME,
                "method": "atr_ema55_swing",
            }
        except Exception as e:
            logger.error(f"❌ Gagal menghitung adaptive initial stop {symbol}: {e}")
            return None

    def _get_macro_trend(self, symbol):
        """
        Evaluasi tren makro pada timeframe 1H.
        Returns:
            str: 'bullish' | 'bearish' | 'neutral'
        """
        df_1h = self.fetch_candles(symbol, config.HIGHER_TIMEFRAME)
        if df_1h.empty:
            return "neutral"
        
        df_1h = self.calculate_indicators(df_1h)
        if df_1h.empty or len(df_1h) < 3:
            return "neutral"

        # Binance mengembalikan candle yang sedang berjalan pada index -1.
        # Semua keputusan arah memakai candle yang sudah final.
        last = df_1h.iloc[-2]
        prev = df_1h.iloc[-3]
        ema_21 = last.get("ema_21", 0)
        ema_55 = last.get("ema_55", 0)
        ema_200 = last.get("ema_200", 0)
        close = last["close"]
        adx = float(last.get("adx", 0) or 0)
        prev_ema_21 = float(prev.get("ema_21", 0) or 0)
        threshold = float(getattr(config, "ADX_THRESHOLD", 25))

        if (
            ema_21 > ema_55
            and close > ema_200
            and ema_21 > prev_ema_21
            and adx >= threshold
        ):
            return "bullish"
        if (
            ema_21 < ema_55
            and close < ema_200
            and ema_21 < prev_ema_21
            and adx >= threshold
        ):
            return "bearish"
        return "neutral"

    def _get_entry_anchor(self, symbol):
        """Independent 4h direction check from completed candles only."""
        df = self.calculate_indicators(self.fetch_candles(
            symbol, getattr(config, "ENTRY_CONFIRM_TIMEFRAME", "4h"),
        ))
        if df.empty or len(df) < config.EMA_TREND + 2:
            return "neutral"
        last, prev = df.iloc[-2], df.iloc[-3]
        values = [last.get(key) for key in ("close", "ema_21", "ema_55", "ema_200")]
        values.append(prev.get("ema_21"))
        if any(pd.isna(value) or float(value) <= 0 for value in values):
            return "neutral"
        close, ema21, ema55, ema200, prev_ema21 = map(float, values)
        if close > ema200 and close > ema55 and ema21 > ema55 and ema21 > prev_ema21:
            return "bullish"
        if close < ema200 and close < ema55 and ema21 < ema55 and ema21 < prev_ema21:
            return "bearish"
        return "neutral"

    @staticmethod
    def _entry_candle_quality(closed_df, side):
        """Reject weak or extended rejection candles; no active candle input."""
        last, prev = closed_df.iloc[-1], closed_df.iloc[-2]
        candle_range = float(last["high"] - last["low"])
        atr = float(last.get("atr", 0) or 0)
        ema21 = float(last.get("ema_21", 0) or 0)
        volume_sma = float(last.get("vol_sma", 0) or 0)
        if candle_range <= 0 or atr <= 0 or ema21 <= 0 or volume_sma <= 0:
            return False, "entry_candle_indicators_unavailable"
        close = float(last["close"])
        opening = float(last["open"])
        location = (close - float(last["low"])) / candle_range
        volume_ratio = float(last["volume"]) / volume_sma
        distance_atr = abs(close - ema21) / atr
        if volume_ratio < config.ENTRY_MIN_15M_VOLUME_RATIO:
            return False, "entry_candle_volume_weak"
        if distance_atr > config.ENTRY_MAX_EMA21_DISTANCE_ATR:
            return False, "entry_candle_extended_from_ema21"
        if side == "long" and not (
            close > opening and close >= float(prev["close"])
            and location >= config.ENTRY_MIN_REJECTION_CLOSE_LOCATION
        ):
            return False, "entry_candle_no_bullish_followthrough"
        if side == "short" and not (
            close < opening and close <= float(prev["close"])
            and 1 - location >= config.ENTRY_MIN_REJECTION_CLOSE_LOCATION
        ):
            return False, "entry_candle_no_bearish_followthrough"
        return True, "entry_candle_confirmed"

    def _get_btc_market_context(self):
        """Regime harga BTC absolut dari closed candle 1H."""
        neutral = {"trend": "neutral", "reason": "btc_market_unavailable"}
        if not getattr(config, "BTC_MARKET_FILTER_ENABLED", True):
            return {**neutral, "reason": "btc_market_filter_disabled"}
        now = time.time()
        ttl = float(getattr(config, "BTC_MARKET_CACHE_SECONDS", 60))
        cached = self._btc_market_cache.get("context")
        if cached and now - cached["fetched_at"] < ttl:
            return cached["value"].copy()
        symbol = getattr(config, "BTC_MARKET_SYMBOL", "BTC/USDT:USDT")
        timeframe = getattr(config, "BTC_MARKET_TIMEFRAME", "1h")
        df = self.calculate_indicators(self.fetch_candles(symbol, timeframe))
        if df.empty or len(df) < 3:
            value = neutral
        else:
            last, prev = df.iloc[-2], df.iloc[-3]
            close = float(last["close"])
            ema21 = float(last.get("ema_21", 0) or 0)
            ema55 = float(last.get("ema_55", 0) or 0)
            ema200 = float(last.get("ema_200", 0) or 0)
            adx = float(last.get("adx", 0) or 0)
            prev_ema21 = float(prev.get("ema_21", 0) or 0)
            if close > ema200 and ema21 > ema55 and ema21 > prev_ema21 and adx >= config.ADX_THRESHOLD:
                trend, reason = "bullish", "btc_uptrend"
            elif close < ema200 and ema21 < ema55 and ema21 < prev_ema21 and adx >= config.ADX_THRESHOLD:
                trend, reason = "bearish", "btc_downtrend"
            else:
                trend, reason = "neutral", "btc_mixed_structure"
            value = {
                "trend": trend, "reason": reason, "price": close,
                "adx": round(adx, 1), "timeframe": timeframe,
            }
        self._btc_market_cache["context"] = {"fetched_at": now, "value": value}
        return value.copy()

    def _is_btc_symbol(self, symbol):
        """BTC.D hanya menjadi directional filter untuk posisi altcoin."""
        return symbol.split("/")[0].upper() == "BTC"

    def _get_btcdom_context(self, timeframe=None):
        """Ambil regime BTC dominance dari closed candle BTCDOMUSDT."""
        timeframe = timeframe or getattr(config, "BTCDOM_TIMEFRAME", "1h")
        neutral = {
            "trend": "neutral",
            "projection": "balanced",
            "reason": "btcdom_unavailable",
            "timeframe": timeframe,
        }

        if not getattr(config, "BTCDOM_FILTER_ENABLED", True):
            return {**neutral, "reason": "btcdom_filter_disabled"}

        now = time.time()
        cache_ttl = getattr(config, "BTCDOM_CACHE_SECONDS", 60)
        cached = self._btcdom_cache.get(timeframe)
        if cached and now - cached["fetched_at"] < cache_ttl:
            return cached["context"].copy()

        symbol = getattr(config, "BTCDOM_SYMBOL", "BTCDOM/USDT:USDT")
        df = self.fetch_candles(symbol, timeframe)
        if df.empty:
            self._btcdom_cache[timeframe] = {
                "fetched_at": now,
                "context": neutral,
            }
            return neutral

        df = self.calculate_indicators(df)
        if df.empty or len(df) < 56:
            self._btcdom_cache[timeframe] = {
                "fetched_at": now,
                "context": neutral,
            }
            return neutral

        # Index -1 umumnya candle aktif, sehingga regime memakai candle -2.
        closed = df.iloc[-2]
        previous = df.iloc[-3]
        close = float(closed["close"])
        prev_close = float(previous["close"])
        ema_21 = float(closed.get("ema_21", 0) or 0)
        ema_55 = float(closed.get("ema_55", 0) or 0)
        prev_ema_21 = float(previous.get("ema_21", 0) or 0)

        trend = "neutral"
        projection = "balanced"
        reason = "btcdom_mixed_structure"

        if close > ema_21 > ema_55 and ema_21 >= prev_ema_21 and close >= prev_close:
            trend = "bullish"
            projection = "btc_strength_alt_pressure"
            reason = "btcdom_rising"
        elif close < ema_21 < ema_55 and ema_21 <= prev_ema_21 and close <= prev_close:
            trend = "bearish"
            projection = "alt_strength_btcdom_weakness"
            reason = "btcdom_falling"

        context = {
            "trend": trend,
            "projection": projection,
            "reason": reason,
            "timeframe": timeframe,
            "price": round(close, 6),
            "ema_21": round(ema_21, 6),
            "ema_55": round(ema_55, 6),
        }
        self._btcdom_cache[timeframe] = {
            "fetched_at": now,
            "context": context,
        }
        return context.copy()

    def _check_btcdom_exit_reversal(self, current_side):
        """Konfirmasi exit altcoin dari closed candle BTC dominance."""
        if not getattr(config, "BTCDOM_EXIT_ON_REVERSAL", True):
            return False, {}

        timeframe = getattr(config, "BTCDOM_EXIT_TIMEFRAME", "15m")
        symbol = getattr(config, "BTCDOM_SYMBOL", "BTCDOM/USDT:USDT")
        required = max(2, int(getattr(config, "BTCDOM_EXIT_CONFIRMATION_CANDLES", 2)))

        df = self.fetch_candles(symbol, timeframe)
        if df.empty:
            return False, {}

        df = self.calculate_indicators(df)
        if df.empty or len(df) < 55 + required:
            return False, {}

        # Exclude candle aktif dan validasi semua closed candle konfirmasi.
        closed = df.iloc[-(required + 1):-1]
        if len(closed) < required:
            return False, {}

        if current_side == "long":
            aligned = (
                (closed["close"] > closed["ema_21"])
                & (closed["ema_21"] > closed["ema_55"])
            ).all()
            momentum = closed["close"].iloc[-1] > closed["close"].iloc[0]
            reversed_against_position = bool(aligned and momentum)
            regime = "bullish"
            reason = "btcdom_bullish_reversal_against_alt_long"
        else:
            aligned = (
                (closed["close"] < closed["ema_21"])
                & (closed["ema_21"] < closed["ema_55"])
            ).all()
            momentum = closed["close"].iloc[-1] < closed["close"].iloc[0]
            reversed_against_position = bool(aligned and momentum)
            regime = "bearish"
            reason = "btcdom_bearish_reversal_against_alt_short"

        details = {
            "reason": reason,
            "btcdom_trend": regime,
            "btcdom_timeframe": timeframe,
            "confirmation_candles": required,
            "btcdom_close": round(float(closed["close"].iloc[-1]), 6),
        }
        return reversed_against_position, details

    def _check_pullback_long(self, df_15m):
        """
        Evaluasi pola Pullback & Bounce untuk sinyal LONG pada 15m.
        """
        if len(df_15m) < 5:
            return False, 0, {}
            
        last = df_15m.iloc[-1]
        prev = df_15m.iloc[-2]
        recent_3 = df_15m.iloc[-3:]
        
        ema_21 = last.get("ema_21", 0)
        ema_55 = last.get("ema_55", 0)
        rsi = last.get("rsi", 50)
        adx = float(last.get("adx", 0) or 0)
        vol = last.get("volume", 0)
        vol_sma = last.get("vol_sma", 0)
        
        # 1. 15m Trend Alignment: EMA 21 harus di atas atau memotong EMA 55
        if ema_21 < ema_55 * 0.998:
            return False, 0, {"reason": "15m_ema_not_bullish"}
            
        # 2. Overextended Check: Jangan beli kalau harga sudah terbang > 1.5% di atas EMA 21
        distance_to_ema = ((last["close"] - ema_21) / ema_21) * 100
        if distance_to_ema > 1.5:
            return False, 0, {"reason": f"overextended_{distance_to_ema:.2f}%"}
            
        # 3. Pullback Zone Check: Dalam 3 candle terakhir, harga harus sempat menyentuh/menguji area EMA 21
        min_low_recent = recent_3["low"].min()
        if min_low_recent > ema_21 * 1.004:
            return False, 0, {"reason": "no_pullback_to_ema_zone"}
            
        # 4. Candlestick Price Action:
        # Range total candle
        candle_range = last["high"] - last["low"]
        if candle_range <= 0:
            return False, 0, {"reason": "flat_candle"}
            
        body = abs(last["close"] - last["open"])
        lower_wick = min(last["open"], last["close"]) - last["low"]
        lower_wick_ratio = lower_wick / candle_range
        is_green_candle = last["close"] >= last["open"]
        is_engulfing = (last["close"] > prev["open"] and last["open"] <= prev["close"] and is_green_candle)
        
        # Syarat trigger: ada rejection wick bawah >= 25% ATAU Bullish Engulfing, dan candle tutup di atas EMA 21
        has_rejection = lower_wick_ratio >= 0.25 or is_engulfing
        if not has_rejection:
            return False, 0, {"reason": f"no_lower_rejection_wick_{lower_wick_ratio:.2f}"}
            
        if last["close"] < ema_21 * 0.998:
            return False, 0, {"reason": "close_below_ema21"}
            
        # 5. RSI Sweet Spot (40 s/d 65): punya ruang untuk naik kencang
        if rsi < 40 or rsi > 68:
            return False, 0, {"reason": f"rsi_out_of_sweet_spot_{rsi:.1f}"}
            
        # 6. Volume Validation
        vol_ratio = vol / vol_sma if vol_sma > 0 else 1.0
        if vol_ratio < config.MIN_VOLUME_RATIO:
            return False, 0, {"reason": f"volume_too_low_{vol_ratio:.2f}x"}

        if adx < config.ADX_THRESHOLD:
            return False, 0, {"reason": f"trend_too_weak_adx_{adx:.1f}"}

        # Skor benar-benar dibangun dari konfluensi, bukan base 80 otomatis.
        score = 55
        if lower_wick_ratio >= 0.35:
            score += 10
        if is_engulfing:
            score += 10
        if vol_ratio >= 1.2:
            score += 10
        if 45 <= rsi <= 60:
            score += 5
        if adx >= getattr(config, "ADX_STRONG_THRESHOLD", 30):
            score += 5
            
        details = {
            "setup": "TPLR_LONG",
            "lower_wick_pct": f"{lower_wick_ratio*100:.1f}%",
            "rsi": round(rsi, 1),
            "vol_ratio": f"{vol_ratio:.2f}x",
            "adx": round(adx, 1),
            "dist_ema": f"{distance_to_ema:+.2f}%",
        }
        return True, min(100, score), details

    def _check_pullback_short(self, df_15m):
        """
        Evaluasi pola Pullback & Rejection untuk sinyal SHORT pada 15m.
        """
        if len(df_15m) < 5:
            return False, 0, {}
            
        last = df_15m.iloc[-1]
        prev = df_15m.iloc[-2]
        recent_3 = df_15m.iloc[-3:]
        
        ema_21 = last.get("ema_21", 0)
        ema_55 = last.get("ema_55", 0)
        rsi = last.get("rsi", 50)
        adx = float(last.get("adx", 0) or 0)
        vol = last.get("volume", 0)
        vol_sma = last.get("vol_sma", 0)
        
        # 1. 15m Trend Alignment: EMA 21 harus di bawah atau memotong EMA 55
        if ema_21 > ema_55 * 1.002:
            return False, 0, {"reason": "15m_ema_not_bearish"}
            
        # 2. Overextended Check: Jangan sell kalau harga sudah jatuh > 1.5% di bawah EMA 21
        distance_to_ema = ((ema_21 - last["close"]) / ema_21) * 100
        if distance_to_ema > 1.5:
            return False, 0, {"reason": f"overextended_down_{distance_to_ema:.2f}%"}
            
        # 3. Pullback Zone Check: Dalam 3 candle terakhir, harga harus sempat rally/retest area EMA 21
        max_high_recent = recent_3["high"].max()
        if max_high_recent < ema_21 * 0.996:
            return False, 0, {"reason": "no_retest_to_ema_zone"}
            
        # 4. Candlestick Price Action:
        candle_range = last["high"] - last["low"]
        if candle_range <= 0:
            return False, 0, {"reason": "flat_candle"}
            
        upper_wick = last["high"] - max(last["open"], last["close"])
        upper_wick_ratio = upper_wick / candle_range
        is_red_candle = last["close"] <= last["open"]
        is_engulfing = (last["close"] < prev["open"] and last["open"] >= prev["close"] and is_red_candle)
        
        # Syarat trigger: ada rejection wick atas >= 25% ATAU Bearish Engulfing, dan candle tutup di bawah EMA 21
        has_rejection = upper_wick_ratio >= 0.25 or is_engulfing
        if not has_rejection:
            return False, 0, {"reason": f"no_upper_rejection_wick_{upper_wick_ratio:.2f}"}
            
        if last["close"] > ema_21 * 1.002:
            return False, 0, {"reason": "close_above_ema21"}
            
        # 5. RSI Sweet Spot (32 s/d 60): punya ruang untuk dump
        if rsi < 32 or rsi > 60:
            return False, 0, {"reason": f"rsi_out_of_sweet_spot_{rsi:.1f}"}
            
        # 6. Volume Validation
        vol_ratio = vol / vol_sma if vol_sma > 0 else 1.0
        if vol_ratio < config.MIN_VOLUME_RATIO:
            return False, 0, {"reason": f"volume_too_low_{vol_ratio:.2f}x"}

        if adx < config.ADX_THRESHOLD:
            return False, 0, {"reason": f"trend_too_weak_adx_{adx:.1f}"}

        score = 55
        if upper_wick_ratio >= 0.35:
            score += 10
        if is_engulfing:
            score += 10
        if vol_ratio >= 1.2:
            score += 10
        if 40 <= rsi <= 55:
            score += 5
        if adx >= getattr(config, "ADX_STRONG_THRESHOLD", 30):
            score += 5
            
        details = {
            "setup": "TPLR_SHORT",
            "upper_wick_pct": f"{upper_wick_ratio*100:.1f}%",
            "rsi": round(rsi, 1),
            "vol_ratio": f"{vol_ratio:.2f}x",
            "adx": round(adx, 1),
            "dist_ema": f"{distance_to_ema:+.2f}%",
        }
        return True, min(100, score), details

    def analyze(self, symbol):
        """
        Analisis komprehensif menggunakan Algoritma TPLR (Trend-Pullback & Liquidity Rejection).
        """
        result = {
            "symbol": symbol,
            "signal": "WAIT",
            "score": 0,
            "details": {},
            "higher_tf_bias": "neutral",
            "price": 0,
            "btcdom_bias": "neutral",
            "market_projection": "balanced",
        }
        
        try:
            # Blacklist check
            base_sym = symbol.replace(":USDT", "")
            if symbol in config.BLACKLIST_COINS or base_sym in config.BLACKLIST_COINS:
                result["details"] = {"reason": "blacklisted_coin"}
                return result
                
            # 1. Macro Trend Anchor (1H)
            macro_trend = self._get_macro_trend(symbol)
            result["higher_tf_bias"] = macro_trend

            btcdom = self._get_btcdom_context()
            btc_market = self._get_btc_market_context()
            result["btcdom_bias"] = btcdom["trend"]
            result["market_projection"] = btcdom["projection"]
            result["btc_market_bias"] = btc_market["trend"]
            
            # 2. Fetch Candle 15m
            df_15m = self.fetch_candles(symbol, config.TRADING_TIMEFRAME)
            if df_15m.empty:
                return result
                
            df_15m = self.calculate_indicators(df_15m)
            if df_15m.empty or len(df_15m) < 5:
                return result
                
            live = df_15m.iloc[-1]
            closed_df = df_15m.iloc[:-1].copy()
            if len(closed_df) < 5:
                return result
            last = closed_df.iloc[-1]
            result["price"] = float(live["close"])
            
            # 3. Evaluasi Setup TPLR
            ema_21_val = float(last.get("ema_21", 0))
            atr_val = float(last.get("atr", 0))
            closed_close = float(last["close"])
            current_close = float(live["close"])
            atr_mult = getattr(config, "ATR_PULLBACK_MULTIPLIER", 0.35)
            
            # Hanya cari LONG jika macro 1H bullish atau neutral
            if macro_trend in ("bullish", "neutral"):
                is_long, score_long, details_long = self._check_pullback_long(closed_df)
                if is_long:
                    if atr_val <= 0 or ema_21_val <= 0 or current_close > (
                        ema_21_val + config.ENTRY_MAX_LIVE_DISTANCE_ATR * atr_val
                    ):
                        result["details"] = {"reason": "live_price_chasing_long"}
                        return result
                    quality_ok, quality_reason = self._entry_candle_quality(closed_df, "long")
                    if not quality_ok:
                        result["details"] = {"reason": quality_reason}
                        return result
                    if current_close < float(last.get("ema_55", 0) or 0) * 0.995:
                        result["details"] = {"reason": "live_price_invalidates_long_structure"}
                        return result
                    if (not self._is_btc_symbol(symbol) and config.BTC_MARKET_FILTER_ENABLED
                            and config.ENTRY_SKIP_NEUTRAL_BTC
                            and btc_market["trend"] == "neutral"):
                        result["details"] = {"reason": "btc_regime_neutral_alt_entry_paused"}
                        return result
                    if (not self._is_btc_symbol(symbol) and config.BTC_MARKET_FILTER_ENABLED
                            and btc_market["trend"] == "bearish"):
                        result["details"] = {"reason": "btc_market_blocks_alt_long", "btc_market": btc_market}
                        return result

                    anchor_trend = self._get_entry_anchor(symbol) if config.ENTRY_REQUIRE_4H_ALIGNMENT else "unchecked"
                    if config.ENTRY_REQUIRE_4H_ALIGNMENT and anchor_trend != "bullish":
                        result["details"] = {"reason": "4h_trend_not_bullish", "4h_trend": anchor_trend}
                        return result
                    if (
                        not self._is_btc_symbol(symbol)
                        and getattr(config, "BTCDOM_STRICT_ENTRY_FILTER", True)
                        and btcdom["trend"] == "bullish"
                    ):
                        result["details"] = {
                            "reason": "btcdom_blocks_alt_long",
                            "btcdom": btcdom,
                        }
                        return result

                    # Bonus skor jika macro 1H selaras
                    if macro_trend == "bullish":
                        score_long = min(100, score_long + 5)
                    if not self._is_btc_symbol(symbol) and btcdom["trend"] == "bearish":
                        score_long = min(
                            100,
                            score_long + getattr(config, "BTCDOM_ALIGNED_SCORE_BONUS", 5),
                        )
                        details_long["btcdom_confirmation"] = "falling_favors_alt_long"
                    if (not self._is_btc_symbol(symbol) and config.BTC_MARKET_FILTER_ENABLED
                            and btc_market["trend"] == "bullish"):
                        score_long = min(100, score_long + 5)
                    if macro_trend == "neutral" and score_long < getattr(config, "NEUTRAL_REGIME_MIN_SCORE", 92):
                        result["details"] = {"reason": "neutral_regime_score_too_low", "score": score_long}
                        return result
                    details_long["btcdom"] = btcdom
                    details_long["btc_market"] = btc_market
                    details_long["4h_trend"] = anchor_trend
                    result["signal"] = "LONG"
                    result["score"] = score_long
                    result["details"] = details_long
                    
                    # Hitung Dynamic Confluence Entry Price (EMA 21 & ATR Pullback)
                    if ema_21_val > 0 and atr_val > 0 and getattr(config, "DYNAMIC_PULLBACK_ENTRY_ENABLED", True):
                        atr_pullback = current_close - (atr_mult * atr_val)
                        suggested_entry = max(ema_21_val, atr_pullback)
                        # Batas aman: diskon antara 0.15% s/d 1.0% dari harga saat ini
                        max_discount_price = current_close * 0.990
                        min_discount_price = current_close * 0.9985
                        suggested_entry = max(max_discount_price, min(min_discount_price, suggested_entry))
                        result["suggested_entry_price"] = suggested_entry

                    planned_entry = float(result.get("suggested_entry_price") or current_close)
                    result["initial_stop_plan"] = self.calculate_initial_stop(
                        symbol, "long", planned_entry, dataframe=df_15m
                    )
                    result["entry_signal_snapshot"] = {
                        "closed_candle_time": str(last["timestamp"]),
                        "side": "long", "score": score_long,
                        "macro": macro_trend, "4h_trend": anchor_trend,
                        "btc_market": btc_market,
                        "btcdom": btcdom, "indicators": {
                            "close": closed_close, "live_price": current_close,
                            "ema21": ema_21_val,
                            "ema55": float(last.get("ema_55", 0) or 0),
                            "ema200": float(last.get("ema_200", 0) or 0),
                            "rsi": float(last.get("rsi", 0) or 0),
                            "adx": float(last.get("adx", 0) or 0),
                        },
                    }
                    
                    logger.info(
                        f"  🎯 TPLR LONG VALIDATED for {symbol} | Score: {score_long} | "
                        f"Wick: {details_long.get('lower_wick_pct')} | RSI: {details_long.get('rsi')}"
                    )
                    return result
            
            # Hanya cari SHORT jika macro 1H bearish atau neutral
            if macro_trend in ("bearish", "neutral"):
                is_short, score_short, details_short = self._check_pullback_short(closed_df)
                if is_short:
                    if atr_val <= 0 or ema_21_val <= 0 or current_close < (
                        ema_21_val - config.ENTRY_MAX_LIVE_DISTANCE_ATR * atr_val
                    ):
                        result["details"] = {"reason": "live_price_chasing_short"}
                        return result
                    quality_ok, quality_reason = self._entry_candle_quality(closed_df, "short")
                    if not quality_ok:
                        result["details"] = {"reason": quality_reason}
                        return result
                    if current_close > float(last.get("ema_55", 0) or 0) * 1.005:
                        result["details"] = {"reason": "live_price_invalidates_short_structure"}
                        return result
                    if (not self._is_btc_symbol(symbol) and config.BTC_MARKET_FILTER_ENABLED
                            and config.ENTRY_SKIP_NEUTRAL_BTC
                            and btc_market["trend"] == "neutral"):
                        result["details"] = {"reason": "btc_regime_neutral_alt_entry_paused"}
                        return result
                    if (not self._is_btc_symbol(symbol) and config.BTC_MARKET_FILTER_ENABLED
                            and btc_market["trend"] == "bullish"):
                        result["details"] = {"reason": "btc_market_blocks_alt_short", "btc_market": btc_market}
                        return result

                    anchor_trend = self._get_entry_anchor(symbol) if config.ENTRY_REQUIRE_4H_ALIGNMENT else "unchecked"
                    if config.ENTRY_REQUIRE_4H_ALIGNMENT and anchor_trend != "bearish":
                        result["details"] = {"reason": "4h_trend_not_bearish", "4h_trend": anchor_trend}
                        return result
                    if (
                        not self._is_btc_symbol(symbol)
                        and getattr(config, "BTCDOM_STRICT_ENTRY_FILTER", True)
                        and btcdom["trend"] == "bearish"
                    ):
                        result["details"] = {
                            "reason": "btcdom_blocks_alt_short",
                            "btcdom": btcdom,
                        }
                        return result

                    if macro_trend == "bearish":
                        score_short = min(100, score_short + 5)
                    if not self._is_btc_symbol(symbol) and btcdom["trend"] == "bullish":
                        score_short = min(
                            100,
                            score_short + getattr(config, "BTCDOM_ALIGNED_SCORE_BONUS", 5),
                        )
                        details_short["btcdom_confirmation"] = "rising_favors_alt_short"
                    if (not self._is_btc_symbol(symbol) and config.BTC_MARKET_FILTER_ENABLED
                            and btc_market["trend"] == "bearish"):
                        score_short = min(100, score_short + 5)
                    if macro_trend == "neutral" and score_short < getattr(config, "NEUTRAL_REGIME_MIN_SCORE", 92):
                        result["details"] = {"reason": "neutral_regime_score_too_low", "score": score_short}
                        return result
                    details_short["btcdom"] = btcdom
                    details_short["btc_market"] = btc_market
                    details_short["4h_trend"] = anchor_trend
                    result["signal"] = "SHORT"
                    result["score"] = score_short
                    result["details"] = details_short
                    
                    # Hitung Dynamic Confluence Entry Price (EMA 21 & ATR Pullback)
                    if ema_21_val > 0 and atr_val > 0 and getattr(config, "DYNAMIC_PULLBACK_ENTRY_ENABLED", True):
                        atr_pullback = current_close + (atr_mult * atr_val)
                        suggested_entry = min(ema_21_val, atr_pullback)
                        # Batas aman: premi tawar antara 0.15% s/d 1.0% dari harga saat ini
                        max_premium_price = current_close * 1.010
                        min_premium_price = current_close * 1.0015
                        suggested_entry = min(max_premium_price, max(min_premium_price, suggested_entry))
                        result["suggested_entry_price"] = suggested_entry

                    planned_entry = float(result.get("suggested_entry_price") or current_close)
                    result["initial_stop_plan"] = self.calculate_initial_stop(
                        symbol, "short", planned_entry, dataframe=df_15m
                    )
                    result["entry_signal_snapshot"] = {
                        "closed_candle_time": str(last["timestamp"]),
                        "side": "short", "score": score_short,
                        "macro": macro_trend, "4h_trend": anchor_trend,
                        "btc_market": btc_market,
                        "btcdom": btcdom, "indicators": {
                            "close": closed_close, "live_price": current_close,
                            "ema21": ema_21_val,
                            "ema55": float(last.get("ema_55", 0) or 0),
                            "ema200": float(last.get("ema_200", 0) or 0),
                            "rsi": float(last.get("rsi", 0) or 0),
                            "adx": float(last.get("adx", 0) or 0),
                        },
                    }
                    
                    logger.info(
                        f"  🎯 TPLR SHORT VALIDATED for {symbol} | Score: {score_short} | "
                        f"Wick: {details_short.get('upper_wick_pct')} | RSI: {details_short.get('rsi')}"
                    )
                    return result
                    
            result["signal"] = "WAIT"
            result["details"] = {"reason": "no_clean_pullback_rejection_setup"}
            
        except Exception as e:
            logger.error(f"❌ Error analyzing {symbol} with TPLR: {e}")
            
        return result
    
    def validate_pending_entry(self, symbol, side):
        """Revalidasi setup sebelum limit order sempat terisi."""
        fresh = self.analyze(symbol)
        expected = side.upper()
        valid = (
            fresh.get("signal") == expected
            and float(fresh.get("score", 0)) >= float(config.SIGNAL_MIN_SCORE)
        )
        return {
            "valid": valid,
            "reason": "setup_still_valid" if valid else fresh.get("details", {}).get("reason", "setup_invalidated"),
            "analysis": fresh,
        }

    def check_reversal(self, symbol, current_side):
        """
        Deteksi pembalikan arah dinamis pada TF 15m.
        Jika posisi LONG dan candle menembus ke bawah EMA 55 → Tutup posisi.
        Jika posisi SHORT dan candle menembus ke atas EMA 55 → Tutup posisi.
        """
        result = {
            "reversed": False,
            "signal": "HOLD",
            "score": 0,
            "severity": "none",
            "details": {}
        }
        
        try:
            df = self.fetch_candles(symbol, config.TRADING_TIMEFRAME)
            if df.empty:
                return result
            
            df = self.calculate_indicators(df)
            if df.empty or len(df) < 3:
                return result
            
            last = df.iloc[-2]
            prev = df.iloc[-3]
            
            ema_21 = last.get("ema_21", 0)
            ema_55 = last.get("ema_55", 0)
            rsi = last.get("rsi", 50)
            close = last["close"]
            
            if current_side == "long":
                # Reversal LONG (Anti-Fakeout):
                # 1. Closed candle sebelumnya resmi tutup di bawah EMA 55, ATAU
                # 2. Harga live jebol valid > 0.5% di bawah EMA 55 (bukan cuma wick tipis), ATAU
                # 3. Terjadi Dead Cross EMA 21 tembus ke bawah EMA 55
                is_closed_breakdown = close < ema_55 and ema_55 > 0
                is_dead_cross = (ema_21 < ema_55 and prev.get("ema_21", 0) >= prev.get("ema_55", 0))
                
                if is_closed_breakdown or is_dead_cross:
                    reason = "closed_below_ema55" if is_closed_breakdown else "dead_cross_ema21_55"
                    result["reversed"] = True
                    result["signal"] = "CLOSE_LONG"
                    result["score"] = 90
                    result["severity"] = "close"
                    result["details"] = {"reason": reason, "rsi": round(rsi, 1)}
                    logger.warning(f"⚠️ Confirmed Reversal for LONG {symbol}: {reason} | RSI: {rsi:.1f}")
                    
                elif close < ema_21 and prev["close"] >= prev.get("ema_21", 0):
                    result.update({
                        "reversed": True,
                        "signal": "REDUCE_LONG",
                        "score": 70,
                        "severity": "reduce",
                        "details": {"reason": "closed_below_ema21", "rsi": round(rsi, 1)},
                    })

            elif current_side == "short":
                # Reversal SHORT (Anti-Fakeout):
                # 1. Closed candle sebelumnya resmi tutup di atas EMA 55, ATAU
                # 2. Harga live jebol valid > 0.5% di atas EMA 55, ATAU
                # 3. Terjadi Golden Cross EMA 21 tembus ke atas EMA 55
                is_closed_breakout = close > ema_55 and ema_55 > 0
                is_golden_cross = (ema_21 > ema_55 and prev.get("ema_21", 0) <= prev.get("ema_55", 0))
                
                if is_closed_breakout or is_golden_cross:
                    reason = "closed_above_ema55" if is_closed_breakout else "golden_cross_ema21_55"
                    result["reversed"] = True
                    result["signal"] = "CLOSE_SHORT"
                    result["score"] = 90
                    result["severity"] = "close"
                    result["details"] = {"reason": reason, "rsi": round(rsi, 1)}
                    logger.warning(f"⚠️ Confirmed Reversal for SHORT {symbol}: {reason} | RSI: {rsi:.1f}")

                elif close > ema_21 and prev["close"] <= prev.get("ema_21", 0):
                    result.update({
                        "reversed": True,
                        "signal": "REDUCE_SHORT",
                        "score": 70,
                        "severity": "reduce",
                        "details": {"reason": "closed_above_ema21", "rsi": round(rsi, 1)},
                    })

            # BTC.D adalah relative-strength filter untuk altcoin saja. Reversal
            # harga symbol tetap menjadi alasan exit dengan prioritas pertama.
            if not result["reversed"] and not self._is_btc_symbol(symbol):
                btcdom_reversed, btcdom_details = self._check_btcdom_exit_reversal(
                    current_side
                )
                if btcdom_reversed:
                    result["reversed"] = True
                    result["signal"] = f"REDUCE_{current_side.upper()}_BTCDOM"
                    result["score"] = 70
                    result["severity"] = "reduce"
                    result["details"] = btcdom_details
                    logger.warning(
                        f"BTC.D reversal confirmed against {current_side.upper()} "
                        f"{symbol}: {btcdom_details}"
                    )

        except Exception as e:
            logger.error(f"❌ Error checking reversal {symbol}: {e}")
        
        return result
