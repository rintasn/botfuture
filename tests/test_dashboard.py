import json
import os
import tempfile
import unittest
from unittest.mock import patch

import config
import dashboard
from dashboard import app, _login_attempts


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_runtime_file = config.RUNTIME_CONFIG_FILE
        self.original_score = config.SIGNAL_MIN_SCORE
        config.RUNTIME_CONFIG_FILE = os.path.join(self.tempdir.name, "overrides.json")
        app.config.update(TESTING=True)
        self.client = app.test_client()
        _login_attempts.clear()

    def tearDown(self):
        config.RUNTIME_CONFIG_FILE = self.original_runtime_file
        config.SIGNAL_MIN_SCORE = self.original_score
        self.tempdir.cleanup()

    def login(self):
        response = self.client.post(
            "/login",
            data={"username": "qais", "password": r"User\@mis1"},
        )
        self.assertEqual(response.status_code, 302)

    def test_dashboard_and_api_require_authentication(self):
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(self.client.get("/api/state").status_code, 401)
        self.assertEqual(self.client.get("/markets").status_code, 302)
        self.assertEqual(self.client.get("/api/scan").status_code, 401)
        self.assertEqual(self.client.get("/api/market/chart?symbol=BTC/USDT:USDT").status_code, 401)

    def test_static_credentials_open_dashboard(self):
        self.login()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Futures Command Center", response.data)
        self.assertEqual(self.client.get("/api/state").status_code, 200)
        admin = self.client.get("/admin")
        self.assertEqual(admin.status_code, 200)
        self.assertIn(b"Runtime strategy configuration", admin.data)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    def test_scanner_page_and_chart_api_only_allow_scanned_markets(self):
        self.login()
        page = self.client.get("/markets")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Market Scanner", page.data)
        state = {
            "scan_monitor": {"status": "scanning", "markets": [
                {"symbol": "ETH/USDT:USDT", "status": "qualified"},
            ]},
            "bot_status": "scanning", "connection_status": "connected",
            "cooldown_until": 0, "active_position": None,
            "pending_order": None, "last_signal": None,
        }
        with patch.object(dashboard, "_load_state", return_value=state):
            self.assertEqual(self.client.get("/api/scan").json["scan"]["status"], "scanning")
            self.assertEqual(self.client.get("/api/market/chart?symbol=XAG/USDT:USDT").status_code, 400)
            self.assertEqual(self.client.get("/api/market/chart?symbol=ETH/USDT:USDT&timeframe=1m").status_code, 400)
            with patch.object(dashboard, "_market_chart", return_value={"candles": []}) as fetch:
                response = self.client.get("/api/market/chart?symbol=ETH/USDT:USDT&timeframe=15m")
                self.assertEqual(response.status_code, 200)
                fetch.assert_called_once_with("ETH/USDT:USDT", "15m")

    def test_chart_calculates_indicators_from_public_candles(self):
        class PublicExchange:
            def fetch_ohlcv(self, symbol, timeframe, limit):
                self.request = (symbol, timeframe, limit)
                return [
                    [1_700_000_000_000 + i * 900_000, 100 + i * .1,
                     101 + i * .1, 99 + i * .1, 100.5 + i * .1, 1000 + i]
                    for i in range(240)
                ]

        exchange = PublicExchange()
        with patch.object(dashboard, "_chart_exchange", exchange), patch.dict(dashboard._chart_cache, {}, clear=True):
            result = dashboard._market_chart("BTC/USDT:USDT", "15m")
        self.assertEqual(exchange.request, ("BTC/USDT:USDT", "15m", 240))
        self.assertEqual(len(result["candles"]), 120)
        self.assertIsNotNone(result["candles"][-1]["ema21"])
        self.assertIsNotNone(result["candles"][-1]["ema200"])
        self.assertIsNotNone(result["candles"][-1]["rsi"])

    def test_admin_config_is_validated_and_persisted(self):
        self.login()
        with self.client.session_transaction() as session:
            csrf = session["csrf_token"]
        response = self.client.post(
            "/admin/config",
            data={"csrf_token": csrf, "SIGNAL_MIN_SCORE": "88"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(config.SIGNAL_MIN_SCORE, 88)
        with open(config.RUNTIME_CONFIG_FILE, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["SIGNAL_MIN_SCORE"], 88)

        response = self.client.post(
            "/admin/config",
            data={"csrf_token": csrf, "SIGNAL_MIN_SCORE": "101"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(config.SIGNAL_MIN_SCORE, 88)


if __name__ == "__main__":
    unittest.main()
