"""Fixture-based parity sentinels for Demo JS and Python indicators.

These tests intentionally distinguish confirmed matches from known formula
differences. They do not claim full strategy parity or execute any broker.
"""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from indicators import atr, ema, relative_volume, rsi, true_range


def demo_ema(values: np.ndarray, span: int) -> np.ndarray:
    alpha = 2.0 / (span + 1.0)
    out = np.empty(len(values), dtype=float)
    previous = None
    for i, value in enumerate(values):
        previous = value if previous is None else value * alpha + previous * (1.0 - alpha)
        out[i] = previous
    return out


def demo_rsi_wilder(values: np.ndarray, period: int = 14) -> np.ndarray:
    out = np.full(len(values), np.nan, dtype=float)
    if len(values) <= period:
        return out
    delta = np.diff(values)
    gains = np.maximum(delta, 0.0)
    losses = np.maximum(-delta, 0.0)
    avg_gain = float(gains[:period].mean())
    avg_loss = float(losses[:period].mean())
    out[period] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(0.0, d)) / period
        avg_loss = (avg_loss * (period - 1) + max(0.0, -d)) / period
        out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def demo_atr(frame: pd.DataFrame, period: int = 14) -> np.ndarray:
    highs, lows, closes = (frame[k].to_numpy(dtype=float) for k in ("high", "low", "close"))
    tr = np.empty(len(frame), dtype=float)
    for i in range(len(frame)):
        tr[i] = highs[i] - lows[i] if i == 0 else max(
            highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])
        )
    out = np.full(len(frame), np.nan, dtype=float)
    if len(frame) <= period:
        return out
    out[period] = tr[1:period + 1].mean()
    for i in range(period + 1, len(frame)):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def fixture(n: int = 360) -> pd.DataFrame:
    x = np.arange(n, dtype=float)
    close = 100.0 + 0.13 * x + 2.7 * np.sin(x / 8.0) + 0.8 * np.sin(x / 2.7)
    open_ = np.r_[close[0], close[:-1]] + 0.15 * np.sin(x / 3.0)
    high = np.maximum(open_, close) + 0.4 + 0.1 * (1.0 + np.sin(x / 5.0))
    low = np.minimum(open_, close) - 0.35 - 0.1 * (1.0 + np.cos(x / 6.0))
    volume = 80.0 + 12.0 * (1.0 + np.sin(x / 9.0)) + (x % 7.0)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume})


class TestDemoPythonIndicatorParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.frame = fixture()
        cls.close = cls.frame["close"]

    def test_ema_matches_demo_recursive_formula(self) -> None:
        expected = demo_ema(self.close.to_numpy(), 50)
        actual = ema(self.close, 50).to_numpy()
        np.testing.assert_allclose(actual[49:], expected[49:], rtol=1e-12, atol=1e-12)

    def test_relative_volume_matches_demo_previous_window(self) -> None:
        volume = self.frame["volume"]
        expected = np.full(len(volume), np.nan)
        for i in range(20, len(volume)):
            baseline = volume.iloc[i - 20:i].mean()
            expected[i] = volume.iloc[i] / baseline if baseline > 0 else 1.0
        actual = relative_volume(volume, 20).to_numpy()
        np.testing.assert_allclose(actual[20:], expected[20:], rtol=1e-12, atol=1e-12)

    def test_rsi_seed_difference_is_detected_and_documented(self) -> None:
        expected = demo_rsi_wilder(self.close.to_numpy(), 14)
        actual = rsi(self.close, 14).to_numpy()
        valid = np.isfinite(expected) & np.isfinite(actual)
        self.assertTrue(valid.any())
        self.assertGreater(float(np.max(np.abs(actual[valid] - expected[valid]))), 1e-4)

    def test_atr_seed_difference_is_detected_and_documented(self) -> None:
        expected = demo_atr(self.frame, 14)
        actual = atr(self.frame, 14).to_numpy()
        valid = np.isfinite(expected) & np.isfinite(actual)
        self.assertTrue(valid.any())
        self.assertGreater(float(np.max(np.abs(actual[valid] - expected[valid]))), 1e-8)

    def test_demo_atr_reference_uses_same_true_range_definition(self) -> None:
        reference = demo_atr(self.frame, 14)
        tr = true_range(self.frame).to_numpy()
        self.assertAlmostEqual(float(reference[14]), float(tr[1:15].mean()), places=12)


if __name__ == "__main__":
    unittest.main()