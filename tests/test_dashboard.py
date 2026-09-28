import json
import os
import tempfile
import unittest

import config
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
