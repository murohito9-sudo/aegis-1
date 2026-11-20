"""Tests for the read-only LIVE operations console and status contract."""
import os
import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from dashboard import Hub, create_app


class LiveConsoleTests(unittest.TestCase):
    def setUp(self):
        self.live_config = SimpleNamespace(
            dry_run=True,
            symbols=("BTC/USDT",),
            allow_market=False,
            max_order_notional=100.0,
        )
        self.settings = SimpleNamespace(
            live_mode=False,
            live=self.live_config,
            exchange_id="binance",
        )
        self.broker = SimpleNamespace(name="paper", status=None)
        self.engine = SimpleNamespace(
            settings=self.settings,
            broker=self.broker,
            mode=SimpleNamespace(value="PAPER"),
        )
        self.client = TestClient(create_app(self.engine, Hub()))
        self.page = self.client.get("/live")
        self.assertEqual(self.page.status_code, 200)
        match = re.search(r'<meta name="aegis-csrf" content="([^"]+)"', self.page.text)
        self.assertIsNotNone(match)
        self.csrf = match.group(1)

    def test_live_console_route_injects_session_csrf(self):
        self.assertIn("Live Operations", self.page.text)
        self.assertIn("READ-ONLY CONSOLE", self.page.text)
        self.assertNotIn("__AEGIS_CSRF__", self.page.text)

    def test_live_status_requires_csrf(self):
        response = self.client.get("/api/live/status")
        self.assertEqual(response.status_code, 403)

    def test_paper_runtime_is_never_reported_as_live_execution(self):
        response = self.client.get(
            "/api/live/status",
            headers={"X-AEGIS-CSRF": self.csrf},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["execution_state"], "LOCKED")
        self.assertFalse(data["execution_enabled"])
        self.assertTrue(data["read_only"])
        self.assertEqual(data["adapter"], "paper")
        self.assertFalse(data["adapter_present"])
        self.assertEqual(data["mode"], "PAPER")

    def test_adapter_status_is_sanitized_and_still_locked(self):
        secret = "test-secret-must-not-leak"
        self.settings.live_mode = True
        self.broker.name = "live"
        self.broker.status = SimpleNamespace(
            dry_run=True,
            connected=True,
            orders_sent=2,
            orders_rejected=1,
            last_error=secret,
            last_reconcile_ts=1234.0,
            reconciled_orders=4,
        )
        with patch.dict(os.environ, {"EXCHANGE_API_SECRET": secret}):
            response = self.client.get(
                "/api/live/status",
                headers={"X-AEGIS-CSRF": self.csrf},
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["execution_state"], "LOCKED")
        self.assertFalse(data["execution_enabled"])
        self.assertTrue(data["adapter_connected"])
        self.assertEqual(data["orders_sent"], 2)
        self.assertEqual(data["orders_rejected"], 1)
        self.assertNotIn(secret, response.text)
        self.assertNotIn("last_error", data)

    def test_status_reports_only_allowlisted_operational_fields(self):
        response = self.client.get(
            "/api/live/status",
            headers={"X-AEGIS-CSRF": self.csrf},
        )
        data = response.json()
        self.assertEqual(data["allowlisted_symbols"], ["BTC/USDT"])
        self.assertEqual(data["max_order_notional"], 100.0)
        self.assertFalse(data["market_orders_allowed"])
        self.assertNotIn("api_key", data)
        self.assertNotIn("api_secret", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
