import json
import os
import tempfile
import time
import unittest

import ccxt
import pandas as pd

from bot import TradingBot
from order_manager import OrderManager
from reversal_guard import ReversalGuard
from scanner import MarketScanner
from signal_engine import SignalEngine
from state_manager import StateManager
from trailing_manager import TrailingManager


class BaseFakeExchange:
    def market(self, symbol):
        return {"id": symbol.replace("/", "").split(":")[0]}

    def fapiPrivatePostCountdownCancelAll(self, params):
        return {"symbol": params["symbol"], "countdownTime": params["countdownTime"]}

    def safe_symbol(self, raw_symbol, market=None, delimiter=None, market_type=None):
        if raw_symbol and raw_symbol.endswith("USDT"):
            return f"{raw_symbol[:-4]}/USDT:USDT"
        return raw_symbol


class HardeningTests(unittest.TestCase):
    def make_state(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        return StateManager(os.path.join(tempdir.name, "state.json"))

    def test_state_save_is_atomic_and_valid_json(self):
        state = self.make_state()
        state.set_status("testing")
        state.set_connection_status("connected")
        with open(state.state_file, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["bot_status"], "testing")
        self.assertEqual(saved["connection_status"], "connected")
        leftovers = [name for name in os.listdir(os.path.dirname(state.state_file)) if name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_api_error_is_not_treated_as_empty_position(self):
        class Exchange(BaseFakeExchange):
            def fetch_positions(self, symbols):
                raise ccxt.NetworkError("offline")

        state = self.make_state()
        state.set_position("BTC/USDT:USDT", "long", 100, 0.1, "1")
        result = OrderManager(Exchange(), state).get_position_status("BTC/USDT:USDT")
        self.assertEqual(result["status"], "error")
        self.assertTrue(state.has_position())

    def test_open_partial_fill_cancels_remainder_and_promotes_position(self):
        class Exchange(BaseFakeExchange):
            def __init__(self):
                self.calls = 0

            def fetch_order(self, order_id, symbol):
                self.calls += 1
                status = "open" if self.calls == 1 else "canceled"
                return {
                    "id": "10", "status": status, "filled": 0.4,
                    "average": 100.0, "price": 100.0,
                }

            def cancel_order(self, order_id, symbol):
                return {"id": order_id, "status": "canceled"}

        state = self.make_state()
        state.set_pending_order(
            "BTC/USDT:USDT", "long", 100, 1, "10",
            client_order_id="bf_btc_test",
        )
        result = OrderManager(Exchange(), state).check_order_filled("BTC/USDT:USDT", "10")
        self.assertEqual(result, "partial")
        self.assertFalse(state.has_pending_order())
        self.assertEqual(state.get_position()["amount"], 0.4)
        self.assertEqual(state.state["protection_status"], "pending")

    def test_entry_uses_gtd_client_id_and_deadman(self):
        class Exchange(BaseFakeExchange):
            def __init__(self):
                self.created_params = None
                self.deadman = []

            def fetch_positions(self, symbols):
                return []

            def set_margin_mode(self, mode, symbol):
                return {}

            def set_leverage(self, leverage, symbol):
                return {}

            def fetch_balance(self):
                return {"USDT": {"free": 35}}

            def market(self, symbol):
                return {
                    "id": "ETHUSDT",
                    "limits": {"amount": {"min": 0.001}, "cost": {"min": 5}},
                }

            def price_to_precision(self, symbol, price):
                return str(round(price, 2))

            def amount_to_precision(self, symbol, amount):
                return str(round(amount, 3))

            def create_order(self, **kwargs):
                self.created_params = kwargs["params"]
                return {"id": "123", "status": "open"}

            def fapiPrivatePostCountdownCancelAll(self, params):
                self.deadman.append(params)
                return params

        exchange = Exchange()
        state = self.make_state()
        before = int(time.time() * 1000)
        order = OrderManager(exchange, state).place_entry_order(
            "ETH/USDT:USDT", "LONG", 100, score=80,
        )
        self.assertEqual(order["id"], "123")
        self.assertEqual(exchange.created_params["timeInForce"], "GTD")
        self.assertTrue(exchange.created_params["newClientOrderId"].startswith("bf_"))
        self.assertGreater(exchange.created_params["goodTillDate"], before + 600_000)
        self.assertEqual(exchange.deadman[-1]["countdownTime"], 120_000)
        self.assertEqual(state.get_pending_order()["amount"], 0.07)

    def test_scanner_rejects_non_crypto_underlying(self):
        class Exchange:
            def load_markets(self):
                return {
                    "BTC/USDT:USDT": {
                        "active": True, "type": "swap", "info": {"underlyingType": "COIN"},
                    },
                    "XAG/USDT:USDT": {
                        "active": True, "type": "swap", "info": {"underlyingType": "COMMODITY"},
                    },
                }

            def fetch_tickers(self, symbols):
                return {
                    symbol: {
                        "symbol": symbol, "quoteVolume": 2_000_000_000,
                        "percentage": 2, "bid": 100, "ask": 100.01, "last": 100,
                    }
                    for symbol in symbols
                }

        candidates = MarketScanner(Exchange()).scan()
        self.assertEqual([item["symbol"] for item in candidates], ["BTC/USDT:USDT"])

    def test_startup_reconciliation_clears_stale_pending_when_exchange_flat(self):
        class Exchange(BaseFakeExchange):
            def fetch_positions(self):
                return []

            def fapiPrivateGetOpenOrders(self):
                return []

            def fapiPrivateGetOpenAlgoOrders(self):
                return []

            def fetch_order(self, order_id, symbol):
                return {"id": order_id, "status": "canceled", "filled": 0}

        state = self.make_state()
        state.set_pending_order("XAG/USDT:USDT", "short", 45, 1, "6332557357")
        result = OrderManager(Exchange(), state).reconcile_startup()
        self.assertEqual(result["status"], "ok")
        self.assertFalse(state.has_pending_order())
        self.assertFalse(state.has_position())

    def test_startup_reconciliation_recovers_position_and_algo_stop(self):
        class Exchange(BaseFakeExchange):
            def fetch_positions(self):
                return [{
                    "symbol": "BTC/USDT:USDT", "side": "long", "contracts": 0.2,
                    "entryPrice": 100,
                }]

            def fapiPrivateGetOpenOrders(self):
                return []

            def fapiPrivateGetOpenAlgoOrders(self):
                return {"orders": [{
                    "symbol": "BTCUSDT", "algoStatus": "NEW",
                    "orderType": "STOP_MARKET", "algoId": 77, "triggerPrice": "75",
                }]}

        state = self.make_state()
        result = OrderManager(Exchange(), state).reconcile_startup()
        self.assertEqual(result["action"], "position_and_stop_recovered")
        self.assertEqual(state.state["protection_status"], "protected")
        self.assertEqual(state.get_trailing_stop()["order_id"], 77)
        self.assertEqual(state.get_trailing_stop()["amount"], 0.2)

    def test_startup_recovers_bot_entry_from_raw_futures_orders(self):
        class Exchange(BaseFakeExchange):
            def fetch_positions(self):
                return []

            def fapiPrivateGetOpenOrders(self):
                return [{
                    "symbol": "ETHUSDT", "orderId": 123,
                    "clientOrderId": "bf_eth_123", "side": "BUY",
                    "price": "2500", "origQty": "0.01",
                    "executedQty": "0", "status": "NEW",
                }]

            def fapiPrivateGetOpenAlgoOrders(self):
                return []

        state = self.make_state()
        result = OrderManager(Exchange(), state).reconcile_startup()
        self.assertEqual(result["action"], "pending_recovered")
        pending = state.get_pending_order()
        self.assertEqual(pending["symbol"], "ETH/USDT:USDT")
        self.assertEqual(pending["order_id"], "123")

    def test_reconciliation_api_error_preserves_local_state(self):
        class Exchange(BaseFakeExchange):
            def fetch_positions(self):
                raise ccxt.NetworkError("offline")

        state = self.make_state()
        state.set_pending_order("BTC/USDT:USDT", "long", 100, 1, "10")
        result = OrderManager(Exchange(), state).reconcile_startup()
        self.assertEqual(result["status"], "error")
        self.assertTrue(state.has_pending_order())

    def test_mandatory_stop_moves_position_to_protected_state(self):
        class FakeOrderManager:
            def place_stop_order(self, **kwargs):
                return {"id": "stop-1"}

            def cancel_stop_order(self, symbol, order_id):
                return "canceled"

        class FakeTrailing:
            def _verify_stop_order(self, symbol, order_id):
                return "active"

        state = self.make_state()
        state.set_position("BTC/USDT:USDT", "long", 100, 0.1, "entry-1")
        bot = TradingBot()
        bot.state = state
        bot.order_mgr = FakeOrderManager()
        bot.trailing_mgr = FakeTrailing()
        self.assertTrue(bot._ensure_stop_protection())
        self.assertEqual(state.state["protection_status"], "protected")
        self.assertEqual(state.get_trailing_stop()["amount"], 0.1)

    def test_adaptive_stop_uses_structure_and_caps_distance(self):
        rows = []
        for index in range(25):
            rows.append({
                "open": 100, "high": 101, "low": 99, "close": 100,
                "atr": 2.0, "ema_55": 98.0,
            })
        # Swing ekstrem seharusnya dijepit oleh max distance 5%, bukan -25%.
        rows[-5]["low"] = 80
        df = pd.DataFrame(rows)
        engine = SignalEngine(exchange=None)
        plan = engine.calculate_initial_stop(
            "BTC/USDT:USDT", "long", 100, dataframe=df
        )
        self.assertAlmostEqual(plan["stop_price"], 95.0)
        self.assertAlmostEqual(plan["distance_pct"], 5.0)
        self.assertEqual(plan["method"], "atr_ema55_swing")

    def test_recent_stop_ack_skips_eventual_consistency_verification(self):
        class FakeOrderManager:
            def place_stop_order(self, **kwargs):
                raise AssertionError("tidak boleh membuat stop kedua")

        class FakeTrailing:
            def __init__(self):
                self.verify_calls = 0

            def is_within_verification_grace(self, stop):
                return True

            def _verify_stop_order(self, symbol, order_id):
                self.verify_calls += 1
                return "not_found"

        state = self.make_state()
        state.set_position("BTC/USDT:USDT", "long", 100, 0.1, "entry")
        state.set_trailing_stop("stop-1", 95, 0, amount=0.1)
        trailing = FakeTrailing()
        bot = TradingBot()
        bot.state = state
        bot.order_mgr = FakeOrderManager()
        bot.trailing_mgr = trailing
        self.assertTrue(bot._ensure_stop_protection())
        self.assertEqual(trailing.verify_calls, 0)

    def test_stop_algo_dedup_keeps_most_protective_for_long(self):
        class Exchange(BaseFakeExchange):
            def __init__(self):
                self.deleted = []

            def fapiPrivateGetOpenAlgoOrders(self, params=None):
                return {"orders": [
                    {"symbol": "BTCUSDT", "algoStatus": "NEW", "orderType": "STOP_MARKET", "side": "SELL", "algoId": 1, "triggerPrice": "90", "quantity": "0.1"},
                    {"symbol": "BTCUSDT", "algoStatus": "NEW", "orderType": "STOP_MARKET", "side": "SELL", "algoId": 2, "triggerPrice": "95", "quantity": "0.1"},
                    {"symbol": "BTCUSDT", "algoStatus": "NEW", "orderType": "STOP_MARKET", "side": "SELL", "algoId": 3, "triggerPrice": "92", "quantity": "0.1"},
                ]}

            def fapiPrivateDeleteAlgoOrder(self, params):
                self.deleted.append(params["algoId"])
                return {"msg": "success"}

        exchange = Exchange()
        state = self.make_state()
        kept = OrderManager(exchange, state).deduplicate_stop_algos(
            "BTC/USDT:USDT", "long"
        )
        self.assertEqual(kept["id"], 2)
        self.assertEqual(exchange.deleted, [1, 3])

    def test_trailing_grace_treats_new_stop_as_active(self):
        state = self.make_state()
        state.set_position("BTC/USDT:USDT", "long", 100, 0.1, "entry")
        state.set_trailing_stop("99", 95, 0, amount=0.1)

        class NeverQueried:
            exchange = None

        manager = TrailingManager(NeverQueried(), state)
        self.assertTrue(manager.is_within_verification_grace(state.get_trailing_stop()))

    def test_identical_stop_create_is_idempotent(self):
        class Exchange(BaseFakeExchange):
            def __init__(self):
                self.algos = []
                self.create_calls = 0

            def price_to_precision(self, symbol, price):
                return str(round(price, 2))

            def fapiPrivateGetOpenAlgoOrders(self, params=None):
                return {"orders": list(self.algos)}

            def fapiPrivateDeleteAlgoOrder(self, params):
                self.algos = [a for a in self.algos if a["algoId"] != params["algoId"]]
                return {"msg": "success"}

            def create_order(self, **kwargs):
                self.create_calls += 1
                algo = {
                    "symbol": "BTCUSDT", "algoStatus": "NEW",
                    "orderType": "STOP_MARKET", "side": "SELL",
                    "algoId": 100 + self.create_calls,
                    "triggerPrice": str(kwargs["params"]["stopPrice"]),
                    "quantity": str(kwargs["amount"]),
                    "clientAlgoId": kwargs["params"]["clientAlgoId"],
                }
                self.algos.append(algo)
                return {"id": str(algo["algoId"]), "info": algo}

        exchange = Exchange()
        manager = OrderManager(exchange, self.make_state())
        first = manager.place_stop_order("BTC/USDT:USDT", "long", 0.1, 95)
        second = manager.place_stop_order("BTC/USDT:USDT", "long", 0.1, 95)
        self.assertEqual(exchange.create_calls, 1)
        self.assertEqual(str(first["id"]), str(second["id"]))
        self.assertTrue(exchange.algos[0]["clientAlgoId"].startswith("bf_"))

    def test_reversal_market_closes_while_hard_stop_stays_active(self):
        class Signal:
            def check_reversal(self, symbol, side):
                return {"reversed": True, "signal": "CLOSE_LONG", "details": {}}

        class Orders:
            def __init__(self):
                self.closed = False

            def close_position(self, **kwargs):
                self.closed = True
                return {"id": "close-1"}

        class Trailing:
            def remove_stop(self, symbol):
                raise AssertionError("hard-stop tidak boleh dicancel sebelum market close")

        state = self.make_state()
        state.set_position("BTC/USDT:USDT", "long", 100, 0.1, "entry")
        orders = Orders()
        guard = ReversalGuard(Signal(), orders, Trailing(), state)
        result = guard.check_and_act()
        self.assertEqual(result["action"], "closed")
        self.assertTrue(orders.closed)

    def test_legacy_wide_stop_is_replaced_place_before_cancel(self):
        events = []

        class Orders:
            def deduplicate_stop_algos(self, symbol, side, preferred_id=None):
                return {"id": "old", "stop_price": 75, "amount": 0.1}

            def place_stop_order(self, **kwargs):
                events.append(("place", kwargs["stop_price"]))
                return {"id": "new"}

            def cancel_stop_order(self, symbol, order_id):
                events.append(("cancel", order_id))
                return "canceled"

        class Trailing:
            def is_within_verification_grace(self, stop):
                return False

            def _verify_stop_order(self, symbol, order_id):
                return "active"

        class Signal:
            def calculate_initial_stop(self, symbol, side, entry):
                return {"stop_price": 96, "distance_pct": 4, "method": "test"}

        state = self.make_state()
        state.set_position("BTC/USDT:USDT", "long", 100, 0.1, "entry")
        state.set_trailing_stop("old", 75, 0, amount=0.1)
        state.state["trailing_stop"]["set_time"] = "2020-01-01T00:00:00"
        state.save()

        bot = TradingBot()
        bot.state = state
        bot.order_mgr = Orders()
        bot.trailing_mgr = Trailing()
        bot.signal_engine = Signal()
        self.assertTrue(bot._ensure_stop_protection())
        self.assertEqual(events, [("place", 96.0), ("cancel", "old")])
        self.assertEqual(state.get_trailing_stop()["order_id"], "new")


if __name__ == "__main__":
    unittest.main()
