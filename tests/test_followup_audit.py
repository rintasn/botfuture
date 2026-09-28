import copy
import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

import ccxt
import config
from runtime_config import strategy_cycle, config as view
from order_manager import OrderManager
from state_manager import StateManager
from trade_ledger import TradeLedger
from evaluate_exits import simulate, walk_forward


class FollowupAuditTests(unittest.TestCase):
    def make_state(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = StateManager(os.path.join(directory.name, "state.json"))
        state.set_position("BTC/USDT:USDT", "long", 100, 1, "10",
                           initial_stop_plan={"stop_price": 98, "distance_pct": 2})
        return state

    def exchange(self):
        exchange = Mock()
        exchange.market.return_value = {"id": "BTCUSDT"}
        exchange.amount_to_precision.side_effect = lambda symbol, amount: str(amount)
        exchange.fetch_my_trades.return_value = []
        exchange.fapiPrivateGetOrder.return_value = {
            "orderId": "11", "status": "FILLED", "executedQty": "0.5", "avgPrice": "102",
        }
        return exchange

    def test_timeout_restart_recovers_once_without_resubmission(self):
        state = self.make_state()
        exchange = self.exchange()
        exchange.create_order.side_effect = ccxt.NetworkError("response lost")
        manager = OrderManager(exchange, state)
        self.assertIsNone(manager.reduce_position("BTC/USDT:USDT", "long", .5, flag="tp1_done"))
        self.assertIsNotNone(state.get_exit_intent())
        restarted = StateManager(state.state_file)
        # Simulate startup REST syncing position before processing the intent.
        restarted.state["active_position"]["amount"] = .5
        recovered = OrderManager(exchange, restarted)
        self.assertTrue(recovered.recover_exit_intent())
        self.assertTrue(recovered.recover_exit_intent())
        self.assertEqual(exchange.create_order.call_count, 1)
        self.assertEqual(restarted.get_position()["amount"], .5)
        self.assertTrue(restarted.get_position()["tp1_done"])
        self.assertEqual(len(restarted.get_position()["partial_exits"]), 1)

    def test_unknown_exit_blocks_second_submission(self):
        state = self.make_state()
        exchange = self.exchange()
        exchange.fapiPrivateGetOrder.side_effect = ccxt.OrderNotFound("not visible")
        manager = OrderManager(exchange, state)
        manager.reduce_position("BTC/USDT:USDT", "long", .5)
        manager.reduce_position("BTC/USDT:USDT", "long", .5)
        manager.close_position("BTC/USDT:USDT", "long", 1)
        self.assertEqual(exchange.create_order.call_count, 1)
        self.assertEqual(state.get_position()["amount"], 1)
        self.assertIsNotNone(state.get_exit_intent())

    def test_persistence_failure_prevents_submission(self):
        state = self.make_state()
        exchange = self.exchange()
        with patch("state_manager.os.replace", side_effect=OSError("disk full")):
            OrderManager(exchange, state).reduce_position("BTC/USDT:USDT", "long", .5)
        exchange.create_order.assert_not_called()

    def test_terminal_partial_uses_actual_filled_quantity(self):
        state = self.make_state()
        exchange = self.exchange()
        exchange.fapiPrivateGetOrder.return_value.update(status="CANCELED", executedQty="0.2")
        OrderManager(exchange, state).reduce_position("BTC/USDT:USDT", "long", .5, flag="tp1_done")
        self.assertAlmostEqual(state.get_position()["amount"], .8)
        self.assertEqual(state.get_position()["partial_exits"][0]["amount"], .2)

    def test_commit_failure_recovery_does_not_double_count(self):
        state = self.make_state()
        exchange = self.exchange()
        exchange.create_order.side_effect = ccxt.NetworkError("lost response")
        manager = OrderManager(exchange, state)
        manager.reduce_position("BTC/USDT:USDT", "long", .5)
        with patch("state_manager.os.replace", side_effect=OSError("disk full")):
            self.assertFalse(manager.recover_exit_intent())
        self.assertEqual(state.get_position()["amount"], 1)
        self.assertTrue(manager.recover_exit_intent())
        self.assertEqual(len(state.get_position()["partial_exits"]), 1)

    def test_zero_fill_rejection_does_not_mark_tp_complete(self):
        state = self.make_state()
        exchange = self.exchange()
        exchange.fapiPrivateGetOrder.return_value.update(status="REJECTED", executedQty="0")
        manager = OrderManager(exchange, state)
        manager.reduce_position("BTC/USDT:USDT", "long", .5, flag="tp1_done")
        self.assertFalse(state.get_position()["tp1_done"])
        self.assertIsNone(state.get_exit_intent())

    def ledger_fixture(self):
        exchange = self.exchange()
        rows = [
            {"id": 1, "orderId": 10, "time": 100000, "side": "BUY", "qty": "1", "realizedPnl": "0", "commission": ".04", "commissionAsset": "USDT"},
            {"id": 2, "orderId": 11, "time": 120000, "side": "SELL", "qty": ".5", "realizedPnl": "1", "commission": ".02", "commissionAsset": "USDT"},
            {"id": 3, "orderId": 12, "time": 120000, "side": "SELL", "qty": ".5", "realizedPnl": "2", "commission": ".02", "commissionAsset": "USDT"},
        ]
        base = 1700000040000
        for row in rows:
            row["time"] += base
        def fetch(params):
            if "orderId" in params:
                return rows[:1]
            return [r for r in rows if r["id"] >= params["fromId"]][:params["limit"]]
        exchange.fapiPrivateGetUserTrades.side_effect = fetch
        exchange.fapiPrivateGetIncome.return_value = [{"symbol": "BTCUSDT", "incomeType": "FUNDING_FEE", "asset": "USDT", "tranId": 1, "income": "-.1"}]
        trade = {"symbol": "BTC/USDT:USDT", "side": "long", "order_id": "10", "close_time": datetime.fromtimestamp((base + 130000) / 1000).isoformat()}
        ledger = TradeLedger(exchange)
        ledger.LIMIT = 2
        return ledger, trade, rows

    def test_ledger_pagination_costs_funding_and_same_timestamp(self):
        ledger, trade, _ = self.ledger_fixture()
        result = ledger.reconcile(trade)
        self.assertAlmostEqual(result["pnl"], 2.82)
        self.assertEqual(result["fill_ids"], ["1", "2", "3"])
        self.assertEqual(result["status"], "reconciled")

    def test_missing_fill_not_treated_as_complete(self):
        ledger, trade, rows = self.ledger_fixture()
        rows.pop()
        with self.assertRaisesRegex(ValueError, "not flat"):
            ledger.reconcile(trade)

    def test_funding_pagination_and_deduplication(self):
        ledger, trade, _ = self.ledger_fixture()
        row = {"symbol": "BTCUSDT", "incomeType": "FUNDING_FEE", "asset": "USDT", "tranId": 1, "income": "-.1"}
        ledger.exchange.fapiPrivateGetIncome.side_effect = [[row, row], []]
        self.assertAlmostEqual(ledger.reconcile(trade)["funding_usdt"], -.1)

    def test_foreign_entry_rejected(self):
        ledger, trade, rows = self.ledger_fixture()
        rows[1].update(side="BUY", orderId=99)
        with self.assertRaisesRegex(ValueError, "Other entry"):
            ledger.reconcile(trade)

    def test_non_usdt_commission_is_explicit_estimate(self):
        ledger, trade, rows = self.ledger_fixture()
        rows[0].update(commissionAsset="BNB", commission=".001")
        ledger.exchange.fetch_ohlcv.return_value = [[rows[0]["time"] // 60000 * 60000, 300, 300, 300, 300, 1]]
        result = ledger.reconcile(trade)
        self.assertEqual(result["status"], "estimated_fx")
        self.assertAlmostEqual(result["fees_usdt"], .34)

    def test_ledger_replaces_estimate_idempotently(self):
        state = self.make_state()
        state.record_partial_exit(.5, .9, "tp1")
        state.clear_position(pnl=1, start_cooldown=False)
        trade = state.get_state()["trade_history"][-1]
        ledger = {"pnl": 1.7, "status": "reconciled"}
        for _ in range(2):
            state.update_trade_ledger("10", trade["close_time"], ledger)
        self.assertAlmostEqual(state.get_state()["total_profit"], 1.7)

    def test_startup_flat_retains_trade_for_later_ledger(self):
        state = self.make_state()
        exchange = self.exchange()
        exchange.fetch_positions.return_value = []
        exchange.fapiPrivateGetOpenAlgoOrders.return_value = []
        manager = OrderManager(exchange, state)
        manager._fetch_all_open_orders = Mock(return_value=[])
        manager.get_realized_pnl = Mock(return_value=-.5)
        self.assertEqual(manager.reconcile_startup()["status"], "ok")
        trade = state.get_state()["trade_history"][-1]
        self.assertEqual(trade["pnl_status"], "estimated")
        self.assertEqual(trade["close_reason"], "startup_exchange_flat")

    def test_config_snapshot_stable_and_reload_next_cycle(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = os.path.join(directory.name, "overrides.json")
        old = config.SIGNAL_MIN_SCORE
        self.addCleanup(setattr, config, "SIGNAL_MIN_SCORE", old)
        with patch.object(config, "RUNTIME_CONFIG_FILE", path):
            with open(path, "w") as f:
                json.dump({"SIGNAL_MIN_SCORE": 86}, f)
            with strategy_cycle():
                self.assertEqual(view.SIGNAL_MIN_SCORE, 86)
                config.apply_runtime_overrides({"SIGNAL_MIN_SCORE": 90})
                with open(path, "w") as f:
                    json.dump({"SIGNAL_MIN_SCORE": 90}, f)
                self.assertEqual(view.SIGNAL_MIN_SCORE, 86)
            with strategy_cycle():
                self.assertEqual(view.SIGNAL_MIN_SCORE, 90)

    def episode(self, time=0, side="long"):
        return {"symbol": "BTC", "side": side, "regime": "trend", "entry_time": time,
                "entry_price": 100, "amount": 1, "risk_pct": 2,
                "candles": [{"time": time + 60000, "open": 100, "high": 104, "low": 97, "close": 103}]}

    def test_replay_stop_before_profit_and_costs(self):
        result = simulate(self.episode())
        self.assertLess(result["r"], -1)

    def test_replay_short_stop_and_ordering(self):
        self.assertLess(simulate(self.episode(side="short"))["r"], -1)
        episode = self.episode()
        episode["candles"][0]["time"] = 0
        with self.assertRaises(ValueError):
            simulate(episode)

    def test_walk_forward_purges_overlap(self):
        report = walk_forward([self.episode(0), self.episode(30000)], train=1, test=1)
        self.assertEqual(report["out_of_sample"]["trades"], 0)
        report = walk_forward([self.episode(0), self.episode(120000)], train=1, test=1)
        self.assertEqual(report["out_of_sample"]["trades"], 1)
        self.assertIn("long:trend", report["oos_by_side_regime"])

    def test_test_outcome_does_not_change_training_choice(self):
        data = [self.episode(0), self.episode(120000)]
        before = walk_forward(data, 1, 1)["folds"][0]["selected"]
        data[1]["candles"][0].update(open=100, high=120, low=100, close=120)
        after = walk_forward(data, 1, 1)["folds"][0]["selected"]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
