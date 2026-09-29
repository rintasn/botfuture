"""Mechanical supply/demand + market-structure entry using closed candles only."""

import math

from runtime_config import config


class SMCEMAStrategy:
    """Find a confirmed swing zone, retest, and close beyond local structure."""

    @staticmethod
    def _number(row, key):
        value = float(row[key])
        return value if math.isfinite(value) else float("nan")

    @classmethod
    def _pivot(cls, candles, index, side, bars):
        key = "low" if side == "long" else "high"
        value = cls._number(candles.iloc[index], key)
        neighbours = [cls._number(candles.iloc[k], key)
                      for k in range(index - bars, index + bars + 1) if k != index]
        if not math.isfinite(value) or not all(math.isfinite(v) for v in neighbours):
            return False
        return value < min(neighbours) if side == "long" else value > max(neighbours)

    @classmethod
    def _opposing_target(cls, candles, entry, side, bars):
        key = "high" if side == "long" else "low"
        levels = []
        for index in range(bars, len(candles) - bars):
            opposite = "short" if side == "long" else "long"
            if cls._pivot(candles, index, opposite, bars):
                level = cls._number(candles.iloc[index], key)
                if (side == "long" and level > entry) or (side == "short" and level < entry):
                    levels.append(level)
        return (min(levels) if side == "long" else max(levels)) if levels else None

    @classmethod
    def evaluate(cls, closed):
        """Return an entry plan or a specific rejection reason; no live candle is used."""
        result = {"signal": "WAIT", "score": 0, "details": {"reason": "smc_insufficient_candles"}}
        bars = int(config.SMC_PIVOT_BARS)
        breakout_bars = int(config.SMC_BREAK_BARS)
        lookback = int(config.SMC_LOOKBACK_CANDLES)
        if len(closed) < max(20, bars * 2 + breakout_bars + 5):
            return result
        last, prev = closed.iloc[-1], closed.iloc[-2]
        values = [cls._number(last, key) for key in
                  ("open", "high", "low", "close", "volume", "vol_sma", "ema_21", "ema_55", "atr")]
        values.append(cls._number(prev, "ema_21"))
        if not all(math.isfinite(v) for v in values) or values[-2] <= 0 or values[5] <= 0:
            result["details"]["reason"] = "smc_indicators_unavailable"
            return result
        opening, high, low, close, volume, vol_sma, ema21, ema55, atr, prev_ema21 = values
        if close > ema21 and ema21 > prev_ema21 and close > opening:
            side = "long"
        elif close < ema21 and ema21 < prev_ema21 and close < opening:
            side = "short"
        else:
            result["details"]["reason"] = "smc_ema_or_candle_not_aligned"
            return result

        volume_ratio = volume / vol_sma
        if volume_ratio < config.SMC_MIN_VOLUME_RATIO:
            result["details"]["reason"] = "smc_volume_weak"
            return result
        close_location = (close - low) / (high - low) if high > low else 0.5
        if (side == "long" and close_location < 0.6) or (side == "short" and close_location > 0.4):
            result["details"]["reason"] = "smc_trigger_candle_weak"
            return result

        prior = closed.iloc[-1 - breakout_bars:-1]
        break_level = float(prior["high"].max() if side == "long" else prior["low"].min())
        if (side == "long" and close <= break_level) or (side == "short" and close >= break_level):
            result["details"]["reason"] = "smc_no_structure_break"
            return result

        start = max(bars, len(closed) - lookback)
        last_rejection = "smc_no_confirmed_demand" if side == "long" else "smc_no_confirmed_supply"
        for index in range(len(closed) - bars - 2, start - 1, -1):
            if not cls._pivot(closed, index, side, bars):
                continue
            pivot = closed.iloc[index]
            pivot_atr = cls._number(pivot, "atr")
            if not math.isfinite(pivot_atr) or pivot_atr <= 0:
                continue
            if side == "long":
                zone_edge = cls._number(pivot, "low")
                zone_inner = max(cls._number(pivot, "open"), cls._number(pivot, "close"))
                displacement = float(closed.iloc[index + 1:index + 1 + config.SMC_DISPLACEMENT_BARS]["close"].max()) - zone_edge
                invalidated = bool((closed.iloc[index + 1:]["close"] < zone_edge - 0.1 * pivot_atr).any())
            else:
                zone_edge = cls._number(pivot, "high")
                zone_inner = min(cls._number(pivot, "open"), cls._number(pivot, "close"))
                displacement = zone_edge - float(closed.iloc[index + 1:index + 1 + config.SMC_DISPLACEMENT_BARS]["close"].min())
                invalidated = bool((closed.iloc[index + 1:]["close"] > zone_edge + 0.1 * pivot_atr).any())
            if abs(zone_inner - zone_edge) > config.SMC_MAX_ZONE_WIDTH_ATR * pivot_atr:
                last_rejection = "smc_zone_too_wide"
                continue
            if displacement < config.SMC_DISPLACEMENT_ATR * pivot_atr or invalidated:
                last_rejection = "smc_zone_not_fresh"
                continue
            recent = closed.iloc[max(index + 1, len(closed) - config.SMC_RETEST_BARS):]
            if side == "long":
                touched = bool(((recent["low"] <= zone_inner + 0.15 * atr)
                                & (recent["low"] >= zone_edge - 0.5 * atr)).any())
            else:
                touched = bool(((recent["high"] >= zone_inner - 0.15 * atr)
                                & (recent["high"] <= zone_edge + 0.5 * atr)).any())
            if not touched:
                last_rejection = "smc_no_recent_zone_retest"
                continue

            entry = break_level  # limit at structure-break retest, not market chase
            min_distance = entry * config.INITIAL_STOP_MIN_DISTANCE_PERCENT / 100
            if side == "long":
                stop = min(zone_edge - config.SMC_STOP_BUFFER_ATR * atr, entry - min_distance)
                valid_geometry = stop < zone_edge < entry < close
            else:
                stop = max(zone_edge + config.SMC_STOP_BUFFER_ATR * atr, entry + min_distance)
                valid_geometry = close < entry < zone_edge < stop
            if not valid_geometry:
                last_rejection = "smc_invalid_zone_geometry"
                continue
            distance_pct = abs(entry - stop) / entry * 100
            if distance_pct > config.INITIAL_STOP_MAX_DISTANCE_PERCENT:
                last_rejection = "smc_stop_exceeds_risk_cap"
                continue
            target = cls._opposing_target(closed.iloc[start:], entry, side, bars)
            if target is None:
                last_rejection = "smc_no_opposing_zone"
                continue
            reward = (target - entry) if side == "long" else (entry - target)
            rr = reward / abs(entry - stop)
            if rr < config.SMC_MIN_REWARD_RISK or (side == "long" and target <= close) or (side == "short" and target >= close):
                last_rejection = "smc_reward_risk_too_low"
                continue
            plan = {
                "stop_price": stop, "distance_pct": distance_pct,
                "atr": atr, "ema55": ema55,
                "swing_low": zone_edge if side == "long" else float(recent["low"].min()),
                "swing_high": zone_edge if side == "short" else float(recent["high"].max()),
                "structure_price": zone_edge, "timeframe": config.TRADING_TIMEFRAME,
                "method": "smc_demand_zone" if side == "long" else "smc_supply_zone",
            }
            return {
                "signal": side.upper(), "score": 85,
                "suggested_entry_price": entry, "initial_stop_plan": plan,
                "details": {
                    "setup": "SMC_EMA", "zone_edge": zone_edge,
                    "zone_inner": zone_inner, "structure_break": break_level,
                    "opposing_target": target, "reward_risk": round(rr, 2),
                    "volume_ratio": round(volume_ratio, 2),
                    "closed_candle_time": str(last["timestamp"]),
                },
            }
        result["details"]["reason"] = last_rejection
        return result
