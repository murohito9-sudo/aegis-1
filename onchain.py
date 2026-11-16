"""AEGIS-1 — JARVIS #06: On-Chain Intelligence Engine.

Prinsip modul ini: **tidak mengarang angka**. Tidak ada sumber on-chain default
yang gratis dan stabil tanpa kunci API, jadi mesin ini bekerja dari data yang
kamu suplai sendiri (CSV ekspor atau API berbayar milikmu) lewat provider.

Yang dilakukan mesin: mengubah deret metrik (exchange inflow/outflow, transaksi
whale, alamat aktif, hashrate, stablecoin flow) menjadi z-score terhadap
riwayatnya sendiri, lalu menandai anomali. Z-score dipakai karena skalanya
berbeda-beda antar metrik; membandingkan angka mentah akan menyesatkan.
"""

from __future__ import annotations

import csv
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# Metrik yang dikenal beserta arah "bagus"-nya untuk harga.
METRIC_DIRECTION = {
    "exchange_inflow": -1,      # makin banyak masuk exchange = tekanan jual
    "exchange_outflow": +1,     # keluar exchange = akumulasi
    "whale_transactions": +1,   # whale aktif = volatilitas/akumulasi
    "active_addresses": +1,
    "stablecoin_inflow": +1,    # amunisi beli
    "hashrate": +1,
    "exchange_reserve": -1,     # cadangan di exchange turun = bullish
}

METRIC_LABEL = {
    "exchange_inflow": "aliran masuk exchange",
    "exchange_outflow": "aliran keluar exchange",
    "whale_transactions": "transaksi whale",
    "active_addresses": "alamat aktif",
    "stablecoin_inflow": "aliran stablecoin masuk",
    "hashrate": "hashrate",
    "exchange_reserve": "cadangan exchange",
}


@dataclass
class MetricPoint:
    ts: float
    values: dict[str, float] = field(default_factory=dict)


@dataclass
class OnChainSignal:
    score: float = 0.0                    # -1..1
    label: str = "netral"
    flags: list[str] = field(default_factory=list)
    zscores: dict[str, float] = field(default_factory=dict)
    samples: int = 0
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 4),
            "label": self.label,
            "flags": self.flags[:6],
            "zscores": {k: round(v, 3) for k, v in self.zscores.items()},
            "samples": self.samples,
            "note": self.note,
        }


def zscore(series: Iterable[float], value: float) -> float:
    data = [float(x) for x in series]
    if len(data) < 3:
        return 0.0
    mean = statistics.fmean(data)
    stdev = statistics.pstdev(data)
    if stdev <= 1e-12:
        return 0.0
    return (float(value) - mean) / stdev


class OnChainEngine:
    """Simpan riwayat metrik, lalu nilai titik terakhir terhadap riwayat itu."""

    def __init__(self, min_samples: int = 8, threshold: float = 1.5) -> None:
        self.min_samples = max(3, min_samples)
        self.threshold = threshold
        self.history: list[MetricPoint] = []

    def ingest(self, ts: float, values: dict[str, float]) -> None:
        clean = {k: float(v) for k, v in values.items()
                 if k in METRIC_DIRECTION and v is not None}
        if clean:
            self.history.append(MetricPoint(ts=float(ts), values=clean))

    def assess(self) -> OnChainSignal:
        if not self.history:
            return OnChainSignal(note="belum ada data on-chain")
        if len(self.history) < self.min_samples:
            return OnChainSignal(
                samples=len(self.history),
                note=f"butuh {self.min_samples} sampel, ada {len(self.history)}",
            )

        latest = self.history[-1]
        signal = OnChainSignal(samples=len(self.history))
        contributions: list[float] = []

        for metric, direction in METRIC_DIRECTION.items():
            series = [p.values[metric] for p in self.history[:-1] if metric in p.values]
            if metric not in latest.values or len(series) < 3:
                continue
            z = zscore(series, latest.values[metric])
            signal.zscores[metric] = z
            if abs(z) >= self.threshold:
                label = METRIC_LABEL.get(metric, metric)
                arah = "naik" if z > 0 else "turun"
                signal.flags.append(f"{label} {arah} ekstrem (z={z:+.1f})")
            # Kontribusi dijepit -1..1 supaya satu metrik ekstrem tidak mendominasi.
            contributions.append(max(-1.0, min(1.0, z / 3.0)) * direction)

        if contributions:
            signal.score = max(-1.0, min(1.0, statistics.fmean(contributions)))
        signal.label = ("akumulasi" if signal.score > 0.25
                        else "distribusi" if signal.score < -0.25 else "netral")
        if not signal.flags:
            signal.flags.append("tidak ada anomali di atas ambang")
        return signal


class CsvOnChainProvider:
    """Baca metrik on-chain dari CSV: kolom ``ts`` + nama metrik yang dikenal."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def rows(self) -> list[dict[str, float]]:
        out: list[dict[str, float]] = []
        with self.path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if "ts" not in row:
                    raise ValueError("CSV on-chain harus punya kolom 'ts'")
                item: dict[str, float] = {"ts": float(row["ts"])}
                for key, value in row.items():
                    if key in METRIC_DIRECTION and value not in (None, ""):
                        try:
                            item[key] = float(value)
                        except (TypeError, ValueError):
                            continue
                out.append(item)
        return out

    def __call__(self) -> list[dict[str, float]]:
        return self.rows()


def engine_from_rows(rows: Iterable[dict[str, float]]) -> OnChainEngine:
    engine = OnChainEngine()
    for row in rows:
        ts = float(row.get("ts", 0.0))
        engine.ingest(ts, {k: v for k, v in row.items() if k != "ts"})
    return engine
