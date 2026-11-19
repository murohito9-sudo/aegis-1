"""AEGIS-ONE — deterministic evaluation for paper research.

A strategy confluence score is not a calibrated probability. Brier score and
probability calibration are calculated only when a separately supplied
probability exists. Missing probabilities remain unavailable.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass
class Prediction:
    ts: float
    score: float | None
    side: str
    r_multiple: float = 0.0
    pnl: float = 0.0
    symbol: str = "—"
    regime: str = "—"
    probability: float | None = None

    @property
    def outcome(self) -> int:
        return 1 if self.r_multiple > 0 else 0


@dataclass
class Bucket:
    lower: float
    upper: float
    n: int = 0
    wins: int = 0
    avg_r: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"range": f"{self.lower:.2f}-{self.upper:.2f}", "n": self.n,
                "win_rate": round(self.win_rate, 4), "avg_r": round(self.avg_r, 4)}


@dataclass
class EvaluationReport:
    samples: int = 0
    brier: float | None = None
    probability_samples: int = 0
    hit_rate: float = 0.0
    avg_r: float = 0.0
    calibration: list[dict[str, Any]] = field(default_factory=list)
    score_performance: list[dict[str, Any]] = field(default_factory=list)
    drift: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "samples": self.samples,
            "brier": round(self.brier, 4) if self.brier is not None else None,
            "probability_samples": self.probability_samples,
            "hit_rate": round(self.hit_rate, 4),
            "avg_r": round(self.avg_r, 4),
            "calibration": self.calibration,
            "score_performance": self.score_performance,
            "drift": self.drift,
            "note": self.note,
        }


class EvaluationEngine:
    def __init__(self, buckets: int = 5) -> None:
        self.buckets = max(2, int(buckets))
        self.records: list[Prediction] = []

    def record(
        self, score: float | None, side: str, r_multiple: float, ts: float,
        pnl: float = 0.0, symbol: str = "—", regime: str = "—",
        probability: float | None = None,
    ) -> Prediction:
        if not all(math.isfinite(float(v)) for v in (ts, r_multiple, pnl)):
            raise ValueError("ts, r_multiple, dan pnl harus finite")
        normalized_score = None if score is None else float(score)
        normalized_probability = None if probability is None else float(probability)
        for name, value in (("score", normalized_score), ("probability", normalized_probability)):
            if value is not None and (not math.isfinite(value) or not 0.0 <= value <= 1.0):
                raise ValueError(f"{name} harus finite dalam rentang 0..1")
        item = Prediction(float(ts), normalized_score, str(side), float(r_multiple),
                          float(pnl), symbol, regime, normalized_probability)
        self.records.append(item)
        return item

    def _bucketise(self, field_name: str) -> list[Bucket]:
        width = 1.0 / self.buckets
        buckets = [Bucket(i * width, (i + 1) * width) for i in range(self.buckets)]
        sums: dict[int, list[float]] = {i: [] for i in range(self.buckets)}
        for item in self.records:
            value = getattr(item, field_name)
            if value is None:
                continue
            index = min(self.buckets - 1, int(value / width))
            buckets[index].n += 1
            buckets[index].wins += item.outcome
            sums[index].append(item.r_multiple)
        for index, bucket in enumerate(buckets):
            if sums[index]:
                bucket.avg_r = statistics.fmean(sums[index])
        return buckets

    def report(self) -> EvaluationReport:
        if not self.records:
            return EvaluationReport(note="belum ada hasil simulasi tercatat")
        report = EvaluationReport(samples=len(self.records))
        report.hit_rate = statistics.fmean(item.outcome for item in self.records)
        report.avg_r = statistics.fmean(item.r_multiple for item in self.records)
        probability_records = [item for item in self.records if item.probability is not None]
        report.probability_samples = len(probability_records)
        if probability_records:
            report.brier = statistics.fmean(
                (item.probability - item.outcome) ** 2 for item in probability_records
            )
            report.calibration = [b.as_dict() for b in self._bucketise("probability") if b.n]
        else:
            report.note = (
                "Brier dan kalibrasi tidak tersedia: belum ada probabilitas terkalibrasi "
                "yang dicatat terpisah dari confluence score."
            )
        report.score_performance = [
            b.as_dict() for b in self._bucketise("score") if b.n
        ]
        if len(self.records) >= 6:
            ordered = sorted(self.records, key=lambda item: item.ts)
            half = len(ordered) // 2
            first = statistics.fmean(item.r_multiple for item in ordered[:half])
            second = statistics.fmean(item.r_multiple for item in ordered[half:])
            report.drift = {
                "avg_r_awal": round(first, 4), "avg_r_akhir": round(second, 4),
                "delta": round(second - first, 4),
                "status": ("memburuk" if second < first - 0.25
                           else "membaik" if second > first + 0.25 else "stabil"),
            }
        else:
            report.drift = {"status": "sampel belum cukup"}
        return report

    def worst_bucket(self) -> dict[str, Any] | None:
        buckets = [b for b in self._bucketise("score") if b.n >= 3]
        return min(buckets, key=lambda b: b.avg_r).as_dict() if buckets else None

    def load(self, rows: Iterable[dict[str, Any]]) -> int:
        count = 0
        for row in rows:
            try:
                self.record(
                    score=float(row["score"]) if row.get("score") is not None else None,
                    probability=(float(row["probability"])
                                 if row.get("probability") is not None else None),
                    side=str(row.get("side") or "—"),
                    r_multiple=float(row.get("r_multiple") or 0),
                    ts=float(row.get("ts") or 0),
                    pnl=float(row.get("pnl") or 0),
                    symbol=str(row.get("symbol") or "—"),
                    regime=str(row.get("regime") or "—"),
                )
                count += 1
            except (TypeError, ValueError):
                continue
        return count
