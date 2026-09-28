"""Offline regression tests: no exchange connections or trading."""
import unittest
from unittest.mock import Mock

import ccxt
import config
from bot import TradingBot
from order_manager import OrderManager
from trailing_manager import TrailingManager


class ExecutionAuditTests(unittest.TestCase):
    def manager(self):
        state = Mock()
        state.get_exit_intent.return_value = None
        state.get_position.return_value = {"entry_price": 100, "amount": 1}
        manager = OrderManager(Mock(), state)
        manager.cancel_all_orders = Mock()
        manager.get_position_status = Mock(return_value={"status": "found"})
        return manager, state

    def test_close_ack_is_not_flat_confirmation(self):
        manager, state = self.manager()
        manager.exchange.create_order.return_value = {"id": "close", "average": 101}
        self.assertIsNone(manager.close_position("BTC/USDT:USDT", "long", 1))
        state.clear_position.assert_not_called()
        manager.cancel_all_orders.assert_not_called()

    def test_reduce_only_rejection_preserves_stop_for_found_and_error(self):
        for status in ("found", "error"):
            with self.subTest(status=status):
                manager, state = self.manager()
                manager.get_position_status.return_value = {"status": status}
                manager.exchange.create_order.side_effect = ccxt.ExchangeError("reduce only rejected")
                manager.close_position("BTC/USDT:USDT", "long", 1)
                state.clear_position.assert_not_called()
                manager.cancel_all_orders.assert_not_called()

    def test_timeout_does_not_retry_in_same_call(self):
        manager, state = self.manager()
        manager.exchange.create_order.side_effect = ccxt.NetworkError("timeout")
        manager.close_position("BTC/USDT:USDT", "long", 1)
        self.assertEqual(manager.exchange.create_order.call_count, 1)
        state.clear_position.assert_not_called()

    def test_unrelated_fill_does_not_replace_known_close(self):
        manager, _ = self.manager()
        manager.exchange.fetch_my_trades.return_value = [{
            "order": "old", "side": "sell", "price": 150,
            "info": {"realizedPnl": "50"}, "fee": {"cost": 0},
        }]
        pnl = manager.get_realized_pnl("BTC/USDT:USDT", "long", 100, 1,
                                       fallback_close_price=101, close_order_id="new")
        self.assertAlmostEqual(pnl, 0.92)

    def test_adopted_stop_is_not_canceled(self):
        manager, state = self.manager()
        state.get_trailing_stop.return_value = {"order_id": "same", "stop_price": 101}
        manager.place_stop_order = Mock(return_value={"id": "same"})
        manager.cancel_stop_order = Mock()
        self.assertTrue(TrailingManager(manager, state)._replace_stop("BTC/USDT:USDT", "long", 1, 100, 1))
        manager.cancel_stop_order.assert_not_called()

    def test_recovery_preserves_locked_stop_and_initial_risk(self):
        bot = TradingBot.__new__(TradingBot)
        bot.state = Mock()
        bot.state.get_trailing_stop.return_value = {"stop_price": 102, "checkpoint_level": 2}
        bot.order_mgr = Mock()
        bot.order_mgr.place_stop_order.return_value = {"id": "replacement"}
        pos = {"symbol": "BTC/USDT:USDT", "entry_price": 100, "side": "long", "amount": 0.5,
               "initial_risk_pct": 2, "initial_stop_plan": {"stop_price": 98}}
        self.assertTrue(bot._place_emergency_sl(pos))
        self.assertEqual(bot.order_mgr.place_stop_order.call_args.kwargs["stop_price"], 102)
        bot.state.set_initial_risk.assert_not_called()

    def test_invalid_numeric_admin_values_rejected(self):
        for value in ("nan", "inf", "-inf"):
            with self.assertRaises(ValueError):
                config._coerce_admin_value("RISK_PER_TRADE_PERCENT", value)
        with self.assertRaises(ValueError):
            config._coerce_admin_value("TIME_STOP_MINUTES", 30.5)

    def test_scan_is_blocked_while_position_exists(self):
        bot = TradingBot.__new__(TradingBot)
        bot.state = Mock()
        bot.state.has_position.return_value = True
        bot.scanner = Mock()
        bot._scan_and_trade()
        bot.scanner.scan.assert_not_called()

    def test_failed_partial_resize_stops_further_actions_this_cycle(self):
        manager, state = self.manager()
        state.get_position.return_value = {"initial_risk_pct": 1, "initial_amount": 1, "amount": 1}
        state.get_trailing_stop.return_value = None
        trailing = TrailingManager(manager, state)
        trailing.reduce_and_resize_stop = Mock(return_value=False)
        result = trailing.update("BTC/USDT:USDT", 100, 103, "long", 1)
        self.assertEqual(result["action"], "partial_tp1_pending")
        self.assertEqual(trailing.reduce_and_resize_stop.call_count, 1)


if __name__ == "__main__":
    unittest.main()
