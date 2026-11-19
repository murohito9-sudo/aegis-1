"""Deterministic, offline research utilities for AEGIS-ONE.

These tools evaluate already-recorded paper-trade outcomes. They do not generate
signals, fit a model, connect to an exchange, or place orders.
"""
from __future__ import annotations

import math
import random
import statistics
from typing import Any, Iterable


def _validated_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, float]]:
    out: list[dict[str, float]] = []
    for index, row in enumerate(rows):
        try:
            ts = float(row["ts"])
            r = float(row["r_multiple"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"baris {index}: ts dan r_multiple wajib numerik") from exc
        if not math.isfinite(ts) or not math.isfinite(r):
            raise ValueError(f"baris {index}: ts dan r_multiple harus finite")
        out.append({"ts": ts, "r_multiple": r})
    out.sort(key=lambda item: item["ts"])
    if any(a["ts"] == b["ts"] for a, b in zip(out, out[1:])):
        raise ValueError("timestamp harus unik untuk urutan evaluasi")
    return out


def _summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"samples": 0, "win_rate": None, "avg_r": None, "profit_factor_r": None}
    wins = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    return {
        "samples": len(values),
        "win_rate": sum(value > 0 for value in values) / len(values),
        "avg_r": statistics.fmean(values),
        "profit_factor_r": wins / losses if losses else None,
    }


def walk_forward(
    rows: Iterable[dict[str, Any]],
    train_size: int,
    test_size: int,
    step_size: int | None = None,
) -> dict[str, Any]:
    """Chronological rolling windows; only test-window outcomes enter test metrics.

    The training window is reported as context only. No parameter fitting is
    performed, so this is a leakage-resistant evaluation splitter, not an
    optimizer or proof of future performance.
    """
    data = _validated_rows(rows)
    if train_size < 2 or test_size < 1:
        raise ValueError("train_size minimal 2 dan test_size minimal 1")
    step = test_size if step_size is None else step_size
    if step < 1:
        raise ValueError("step_size minimal 1")
    windows = []
    start = 0
    while start + train_size + test_size <= len(data):
        train = data[start:start + train_size]
        test = data[start + train_size:start + train_size + test_size]
        train_r = [row["r_multiple"] for row in train]
        test_r = [row["r_multiple"] for row in test]
        windows.append({
            "train_start": train[0]["ts"],
            "train_end": train[-1]["ts"],
            "test_start": test[0]["ts"],
            "test_end": test[-1]["ts"],
            "train": _summary(train_r),
            "test": _summary(test_r),
        })
        start += step
    # Count each out-of-sample observation once even when test windows overlap.
    tested_timestamps = {
        row["ts"] for window in windows for row in data
        if window["test_start"] <= row["ts"] <= window["test_end"]
    }
    tested = [row["r_multiple"] for row in data if row["ts"] in tested_timestamps]
    return {
        "method": "rolling_chronological",
        "windows": windows,
        "window_count": len(windows),
        "test_summary": _summary(tested),
        "note": "Hasil test bersifat historis; training hanya konteks dan tidak dipakai untuk klaim probabilitas.",
    }


def monte_carlo_risk(
    rows: Iterable[dict[str, Any]],
    simulations: int = 1000,
    starting_equity: float = 10_000.0,
    risk_fraction: float = 0.01,
    seed: int = 42,
) -> dict[str, Any]:
    """Bootstrap trade R-multiples and report ending-equity/drawdown percentiles."""
    data = _validated_rows(rows)
    values = [row["r_multiple"] for row in data]
    if not values:
        raise ValueError("minimal satu hasil trade diperlukan")
    if not 100 <= simulations <= 100_000:
        raise ValueError("simulations harus berada pada 100..100000")
    if not math.isfinite(starting_equity) or starting_equity <= 0:
        raise ValueError("starting_equity harus finite dan positif")
    if not math.isfinite(risk_fraction) or not 0 < risk_fraction <= 0.05:
        raise ValueError("risk_fraction harus > 0 dan <= 0.05")
    rng = random.Random(seed)
    endings: list[float] = []
    drawdowns: list[float] = []
    for _ in range(simulations):
        equity = peak = starting_equity
        worst = 0.0
        for _trade in values:
            sampled_r = rng.choice(values)
            equity *= max(0.0, 1.0 + sampled_r * risk_fraction)
            peak = max(peak, equity)
            if peak > 0:
                worst = max(worst, (peak - equity) / peak)
        endings.append(equity)
        drawdowns.append(worst)

    def percentile(sample: list[float], p: float) -> float:
        ordered = sorted(sample)
        position = (len(ordered) - 1) * p
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return ordered[lower]
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

    return {
        "method": "bootstrap_with_replacement",
        "simulations": simulations,
        "trades_per_path": len(values),
        "starting_equity": starting_equity,
        "risk_fraction": risk_fraction,
        "seed": seed,
        "ending_equity": {
            "p05": percentile(endings, 0.05),
            "median": percentile(endings, 0.50),
            "p95": percentile(endings, 0.95),
        },
        "max_drawdown": {
            "p50": percentile(drawdowns, 0.50),
            "p95": percentile(drawdowns, 0.95),
            "p99": percentile(drawdowns, 0.99),
        },
        "probability_below_start": sum(value < starting_equity for value in endings) / simulations,
        "note": "Bootstrap mengasumsikan trade dapat diacak dan distribusinya tetap; bukan prediksi, jaminan, atau rekomendasi risiko.",
    }
