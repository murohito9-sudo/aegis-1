"""AEGIS-1 — JARVIS #14: AI Evaluation Engine.

Menilai kualitas keputusan sendiri, bukan hanya hasilnya:

* **Kalibrasi** — saat sistem bilang skor 0.8, seberapa sering benar?
  Skor konfluensi dipakai sebagai probabilitas, lalu dibandingkan dengan
  kenyataan per keranjang (bucket).
* **Brier score** — rata-rata kuadrat selisih probabilitas vs hasil (0..1,
  makin kecil makin baik).
* **Drift** — apakah performa separuh terakhir memburuk dibanding separuh awal.

Semua deterministik dan bisa dihitung ulang dari riwayat; tidak ada model yang
"belajar diam-diam" dan tidak ada angka yang ditebak.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass
class Prediction:
    ts: float
    score: float          # 0..1, dipakai sebagai probabilitas menang
    side: str
    r_multiple: float = 0.0
    pnl: float = 0.0
    symbol: str = "—"
    regime: str = "—"

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
        return (self.wins / self.n) if self.n else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "range": f"{self.lower:.2f}-{self.upper:.2f}",
            "n": self.n,
            "win_rate": round(self.win_rate, 4),
            "avg_r": round(self.avg_r, 4),
        }


@dataclass
class EvaluationReport:
    samples: int = 0
    brier: float = 0.0
    hit_rate: float = 0.0
    avg_r: float = 0.0
    calibration: list[dict[str, Any]] = field(default_factory=list)
    drift: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "samples": self.samples,
            "brier": round(self.brier, 4),
            "hit_rate": round(self.hit_rate, 4),
            "avg_r": round(self.avg_r, 4),
            "calibration": self.calibration,
            "drift": self.drift,
            "note": self.note,
        }


class EvaluationEngine:
    def __init__(self, buckets: int = 5) -> None:
        self.buckets = max(2, buckets)
        self.records: list[Prediction] = []

    def record(
        self,
        score: float,
        side: str,
        r_multiple: float,
        ts: float,
        pnl: float = 0.0,
        symbol: str = "—",
        regime: str = "—",
    ) -> Prediction:
        item = Prediction(
            ts=float(ts),
            score=max(0.0, min(1.0, float(score))),
            side=str(side),
            r_multiple=float(r_multiple),
            pnl=float(pnl),
            symbol=symbol,
            regime=regime,
        )
        self.records.append(item)
        return item

    def _bucketise(self) -> list[Bucket]:
        width = 1.0 / self.buckets
        buckets = [Bucket(lower=i * width, upper=(i + 1) * width) for i in range(self.buckets)]
        sums: dict[int, list[float]] = {i: [] for i in range(self.buckets)}
        for item in self.records:
            index = min(self.buckets - 1, int(item.score / width)) if width > 0 else 0
            buckets[index].n += 1
            buckets[index].wins += item.outcome
            sums[index].append(item.r_multiple)
        for index, bucket in enumerate(buckets):
            if sums[index]:
                bucket.avg_r = statistics.fmean(sums[index])
        return buckets

    def report(self) -> EvaluationReport:
        if not self.records:
            return EvaluationReport(note="belum ada prediksi tercatat")

        report = EvaluationReport(samples=len(self.records))
        report.brier = statistics.fmean(
            (item.score - item.outcome) ** 2 for item in self.records
        )
        report.hit_rate = statistics.fmean(item.outcome for item in self.records)
        report.avg_r = statistics.fmean(item.r_multiple for item in self.records)
        report.calibration = [b.as_dict() for b in self._bucketise() if b.n]

        if len(self.records) >= 6:
            half = len(self.records) // 2
            first = statistics.fmean(i.r_multiple for i in self.records[:half])
            second = statistics.fmean(i.r_multiple for i in self.records[half:])
            report.drift = {
                "avg_r_awal": round(first, 4),
                "avg_r_akhir": round(second, 4),
                "delta": round(second - first, 4),
                "status": ("memburuk" if second < first - 0.25
                           else "membaik" if second > first + 0.25 else "stabil"),
            }
        else:
            report.drift = {"status": "sampel belum cukup"}
        return report

    def worst_bucket(self) -> dict[str, Any] | None:
        """Keranjang skor dengan rata-rata R terburuk yang punya cukup sampel."""
        buckets = [b for b in self._bucketise() if b.n >= 3]
        if not buckets:
            return None
        worst = min(buckets, key=lambda b: b.avg_r)
        return worst.as_dict()

    def load(self, rows: Iterable[dict[str, Any]]) -> int:
        """Muat riwayat dari baris ``{ts, score, side, r_multiple, ...}``."""
        count = 0
        for row in rows:
            try:
                self.record(
                    score=float(row.get("score") or 0),
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
