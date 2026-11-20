"""Tests for the unified read-only JARVIS intelligence contract."""
import unittest

from jarvis_core import ProviderStatus, compose


class TestJarvisCore(unittest.TestCase):
    def test_missing_providers_degrade_without_bullish_votes(self):
        report = compose(
            symbol="BTC/USDT", timeframe="15m", evaluated_at=1000,
            strategy={"side": "LONG", "score": 0.9, "vetoes": []},
            providers=[ProviderStatus("arkham", available=False)],
        )
        self.assertEqual(report.status, "degraded")
        self.assertEqual(report.decision, "wait")
        self.assertEqual(report.missing_inputs, ["arkham"])

    def test_stale_provider_is_missing(self):
        report = compose(
            symbol="BTC/USDT", timeframe="15m", evaluated_at=1000,
            strategy={"side": "SHORT", "score": 0.8},
            providers=[ProviderStatus("news", True, "rss", 900, 30)],
        )
        self.assertEqual(report.status, "degraded")
        self.assertIn("news", report.missing_inputs)

    def test_strategy_veto_has_precedence(self):
        report = compose(
            symbol="BTC/USDT", timeframe="15m", evaluated_at=1000,
            strategy={"side": "LONG", "score": 1.0, "vetoes": ["spread too wide"]},
        )
        self.assertEqual(report.status, "blocked")
        self.assertEqual(report.decision, "avoid")
        self.assertIn("spread too wide", report.vetoes)

    def test_kill_switch_blocks(self):
        report = compose(
            symbol="BTC/USDT", timeframe="15m", evaluated_at=1000,
            strategy={"side": "LONG"},
            risk={"mode": "PAPER", "kill_switch": True},
        )
        self.assertEqual(report.decision, "avoid")
        self.assertIn("kill-switch active", report.vetoes)

    def test_complete_inputs_only_request_paper_review(self):
        report = compose(
            symbol="BTC/USDT", timeframe="15m", evaluated_at=1000,
            strategy={"side": "LONG", "score": 0.8},
            providers=[ProviderStatus("news", True, "rss", 995, 30)],
        )
        self.assertEqual(report.status, "ready")
        self.assertEqual(report.decision, "review_paper")

    def test_non_finite_evaluation_time_rejected(self):
        with self.assertRaises(ValueError):
            compose(symbol="BTC/USDT", timeframe="15m", evaluated_at=float("nan"),
                    strategy={})


if __name__ == "__main__":
    unittest.main()
