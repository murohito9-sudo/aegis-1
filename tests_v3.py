"""Regression tests for AEGIS-ONE V3 reliability foundations."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from config import ExecutionConfig, Settings, load_settings
from data import MarketSnapshot
from execution import Order, OrderSide, OrderStatus, OrderType, PaperBroker
from logger import AuditLog


class TestV3StorageIsolation(unittest.TestCase):
    def test_demo_and_live_paths_are_separate(self):
        keys = {
            "AEGIS_LIVE_MODE": "true",
            "AEGIS_DEMO_DB_PATH": "/tmp/aegis-v3/demo.sqlite3",
            "AEGIS_DEMO_JSONL_PATH": "/tmp/aegis-v3/demo.jsonl",
            "AEGIS_LIVE_DB_PATH": "/tmp/aegis-v3/live.sqlite3",
            "AEGIS_LIVE_JSONL_PATH": "/tmp/aegis-v3/live.jsonl",
        }
        with patch.dict(os.environ, keys, clear=False):
            settings = load_settings()
        self.assertEqual(settings.db_path, Path(keys["AEGIS_LIVE_DB_PATH"]))
        self.assertEqual(settings.jsonl_path, Path(keys["AEGIS_LIVE_JSONL_PATH"]))
        self.assertNotEqual(settings.db_path, Path(keys["AEGIS_DEMO_DB_PATH"]))
        self.assertNotEqual(settings.jsonl_path, Path(keys["AEGIS_DEMO_JSONL_PATH"]))

    def test_shared_database_path_is_rejected(self):
        keys = {
            "AEGIS_LIVE_MODE": "true",
            "AEGIS_DEMO_DB_PATH": "/tmp/aegis-v3/shared.sqlite3",
            "AEGIS_LIVE_DB_PATH": "/tmp/aegis-v3/shared.sqlite3",
        }
        with patch.dict(os.environ, keys, clear=False):
            with self.assertRaises(ValueError):
                load_settings()


class TestV3PaperPartialFill(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.audit = AuditLog(self.root / "audit.sqlite3", self.root / "audit.jsonl")
        self.addCleanup(self.audit._conn.close)
        self.settings = Settings(
            db_path=self.root / "audit.sqlite3",
            jsonl_path=self.root / "audit.jsonl",
            initial_equity=10_000.0,
            execution=ExecutionConfig(paper_fill_fraction=0.4, min_qty=0.00001, min_notional=1.0),
        )
        self.broker = PaperBroker(self.settings, self.audit)

    def snapshot(self):
        candles = pd.DataFrame([{
            "ts": 1_700_000_000.0, "open": 100.0, "high": 101.0,
            "low": 99.0, "close": 100.0, "volume": 1000.0,
        }])
        return MarketSnapshot(
            symbol="BTC/USDT", timeframe="15m", candles=candles,
            best_bid=99.99, best_ask=100.01, last_price=100.0,
            ts=1_700_000_001.0, source="test",
        )

    def test_market_order_progresses_partial_to_filled(self):
        order = Order(symbol="BTC/USDT", side=OrderSide.BUY, type=OrderType.MARKET, qty=1.0)
        self.broker.place(order)
        first = self.broker.on_price(self.snapshot())
        self.assertEqual(len(first), 1)
        self.assertAlmostEqual(first[0].qty, 0.4)
        self.assertEqual(order.status, OrderStatus.PARTIALLY_FILLED)
        self.assertAlmostEqual(order.remaining, 0.6)
        second = self.broker.on_price(self.snapshot())
        self.assertEqual(len(second), 1)
        self.assertEqual(order.status, OrderStatus.PARTIALLY_FILLED)
        third = self.broker.on_price(self.snapshot())
        self.assertEqual(len(third), 1)
        self.assertAlmostEqual(third[0].qty, 0.2)
        self.assertEqual(order.status, OrderStatus.FILLED)
        self.assertAlmostEqual(order.filled, 1.0)
        self.assertAlmostEqual(order.remaining, 0.0)
        self.assertGreater(order.fee, 0.0)


if __name__ == "__main__":
    unittest.main()
