"""Deterministic, offline tests for the selectable SMC-EMA entry setup."""

import unittest

import pandas as pd

import config
from signal_engine import SignalEngine
from smc_ema_strategy import SMCEMAStrategy


def candles(side="long"):
    rows = []
    for index in range(44):
        rows.append({
            "timestamp": index, "open": 99.7, "high": 101.5,
            "low": 99.0, "close": 100.0, "volume": 120,
            "vol_sma": 100, "atr": 1.0, "ema_21": 99.5,
            "ema_55": 98.5,
        })
    if side == "long":
        rows[25].update(high=110)
        rows[35].update(open=97.8, high=99, low=97.5, close=98.0)
        rows[40].update(open=99, high=101, low=97.8, close=99.8)
        rows[43].update(open=101, high=103, low=100, close=102.5, ema_21=100)
    else:
        rows[25].update(low=90)
        rows[35].update(open=102.7, high=103, low=101, close=102.5)
        rows[40].update(open=101, high=102.8, low=99.5, close=100.5)
        rows[43].update(open=100, high=100.5, low=98, close=98.5,
                        ema_21=100, ema_55=101)
        rows[42]["ema_21"] = 101
    return pd.DataFrame(rows)


class SMCEMATests(unittest.TestCase):
    def make_engine(self, live_price=102):
        frame = pd.concat([candles(), pd.DataFrame([{
            "timestamp": 44, "open": live_price, "high": live_price + 0.2,
            "low": live_price - 0.2, "close": live_price,
            "volume": 100, "vol_sma": 100, "atr": 1,
            "ema_21": 100, "ema_55": 98.5,
        }])], ignore_index=True)
        engine = SignalEngine(None)
        engine.fetch_candles = lambda *args, **kwargs: frame.copy()
        engine.calculate_indicators = lambda data: data
        engine._get_macro_trend = lambda symbol: "bullish"
        engine._get_entry_anchor = lambda symbol: "neutral"
        engine._get_btc_market_context = lambda: {"trend": "bullish", "reason": "btc_uptrend"}
        engine._get_btcdom_context = lambda: {"trend": "neutral", "projection": "balanced"}
        return engine, frame

    def test_long_has_zone_retest_break_target_and_structure_stop(self):
        signal = SMCEMAStrategy.evaluate(candles())
        self.assertEqual(signal["signal"], "LONG")
        self.assertGreaterEqual(signal["details"]["reward_risk"], config.SMC_MIN_REWARD_RISK)
        self.assertLess(signal["initial_stop_plan"]["stop_price"], signal["details"]["zone_edge"])
        self.assertEqual(signal["initial_stop_plan"]["method"], "smc_demand_zone")

    def test_short_is_symmetric(self):
        signal = SMCEMAStrategy.evaluate(candles("short"))
        self.assertEqual(signal["signal"], "SHORT")
        self.assertGreater(signal["initial_stop_plan"]["stop_price"], signal["details"]["zone_edge"])
        self.assertEqual(signal["initial_stop_plan"]["method"], "smc_supply_zone")

    def test_no_break_cannot_open(self):
        frame = candles()
        frame.loc[43, ["open", "high", "close"]] = [100.8, 101.2, 101]
        self.assertEqual(SMCEMAStrategy.evaluate(frame)["details"]["reason"], "smc_no_structure_break")

    def test_weak_volume_cannot_open(self):
        frame = candles()
        frame.loc[43, "volume"] = 50
        self.assertEqual(SMCEMAStrategy.evaluate(frame)["details"]["reason"], "smc_volume_weak")

    def test_stop_beyond_cap_cannot_open(self):
        frame = candles()
        frame.loc[35, ["open", "high", "low", "close"]] = [95.3, 97, 95, 95.5]
        frame.loc[40, "low"] = 95.3
        signal = SMCEMAStrategy.evaluate(frame)
        self.assertEqual(signal["signal"], "WAIT")
        self.assertEqual(signal["details"]["reason"], "smc_stop_exceeds_risk_cap")

    def test_strategy_choice_is_validated(self):
        self.assertEqual(config._coerce_admin_value("ENTRY_STRATEGY", "smc_ema"), "SMC_EMA")
        with self.assertRaises(ValueError):
            config._coerce_admin_value("ENTRY_STRATEGY", "unknown")

    def test_engine_dispatches_selected_strategy_and_preserves_stop_plan(self):
        engine, _ = self.make_engine()
        result = engine.analyze("ETH/USDT:USDT", strategy="SMC_EMA")
        self.assertEqual(result["signal"], "LONG")
        self.assertEqual(result["strategy"], "SMC_EMA")
        self.assertEqual(result["entry_signal_snapshot"]["strategy"], "SMC_EMA")
        self.assertEqual(result["initial_stop_plan"]["method"], "smc_demand_zone")

    def test_active_candle_cannot_create_new_setup(self):
        engine, frame = self.make_engine()
        frame.loc[43, ["open", "high", "close"]] = [100.8, 101.2, 101]
        frame.loc[44, ["open", "high", "close"]] = [102, 104, 103]
        engine.fetch_candles = lambda *args, **kwargs: frame.copy()
        result = engine.analyze("ETH/USDT:USDT", strategy="SMC_EMA")
        self.assertEqual(result["signal"], "WAIT")
        self.assertEqual(result["details"]["reason"], "smc_no_structure_break")

    def test_pending_retest_remains_valid_without_new_breakout(self):
        engine, frame = self.make_engine()
        signal = engine.analyze("ETH/USDT:USDT", strategy="SMC_EMA")
        frame.loc[44, "close"] = 101.8
        frame = pd.concat([frame, frame.iloc[[-1]].assign(timestamp=45, close=101.7)], ignore_index=True)
        engine.fetch_candles = lambda *args, **kwargs: frame.copy()
        validation = engine.validate_pending_entry(
            "ETH/USDT:USDT", "long", signal["entry_signal_snapshot"]
        )
        self.assertTrue(validation["valid"])
        frame.loc[44, "close"] = 97
        engine.fetch_candles = lambda *args, **kwargs: frame.copy()
        self.assertFalse(engine.validate_pending_entry(
            "ETH/USDT:USDT", "long", signal["entry_signal_snapshot"]
        )["valid"])


if __name__ == "__main__":
    unittest.main()
