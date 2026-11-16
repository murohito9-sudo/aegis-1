"""AEGIS-1 — lapisan data.

Sumber data:

* ``CCXTFeed``  — data publik (read-only, tanpa API key) dari exchange yang
  didukung ccxt. Hanya memakai endpoint publik: ``fetch_ohlcv`` dan
  ``fetch_ticker``. Tidak ada endpoint yang dikarang: semuanya metode resmi
  ccxt (https://docs.ccxt.com/#/README).
* ``SyntheticFeed`` — generator deterministik untuk demo offline dan unit test.
* ``CSVFeed`` — baca candle dari file CSV milik sendiri.

``DataQualityGate`` adalah gerbang fail-closed: kalau satu saja pemeriksaan
gagal, tidak ada sinyal yang boleh diproses.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np
import pandas as pd

from config import Settings

OHLCV_COLUMNS = ["ts", "open", "high", "low", "close", "volume"]

TIMEFRAME_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "12h": 43200,
    "1d": 86400,
}


def timeframe_seconds(tf: str) -> int:
    if tf not in TIMEFRAME_SECONDS:
        raise ValueError(f"timeframe tidak dikenal: {tf}")
    return TIMEFRAME_SECONDS[tf]


@dataclass
class MarketSnapshot:
    symbol: str
    timeframe: str
    candles: pd.DataFrame
    best_bid: float
    best_ask: float
    last_price: float
    ts: float
    source: str

    @property
    def spread_bps(self) -> float:
        mid = (self.best_bid + self.best_ask) / 2.0
        if mid <= 0:
            return float("inf")
        return (self.best_ask - self.best_bid) / mid * 10_000.0

    @property
    def last_bar_ts(self) -> float:
        return float(self.candles["ts"].iloc[-1]) if len(self.candles) else 0.0


class Feed(Protocol):
    def snapshot(self, bars: int) -> MarketSnapshot: ...


# --------------------------------------------------------------------------- #
# Feed implementasi
# --------------------------------------------------------------------------- #
class SyntheticFeed:
    """Random walk deterministik — untuk demo, CI, dan uji guardrail."""

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        timeframe: str = "15m",
        seed: int = 7,
        start_price: float = 60_000.0,
        bars_ready: int = 900,
    ) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.seed = seed
        self.start_price = start_price
        self._tf = timeframe_seconds(timeframe)
        self._candles = self._generate(bars_ready)
        self._step = 0

    def _generate(self, n: int) -> pd.DataFrame:
        rng = np.random.default_rng(self.seed)
        # Dua rezim bergantian supaya trend dan range sama-sama teruji.
        drift = np.concatenate(
            [
                np.full(n // 3, 0.00035),
                np.full(n // 3, -0.00025),
                np.full(n - 2 * (n // 3), 0.0001),
            ]
        )
        vol = 0.0035
        shocks = rng.normal(drift, vol, n)
        close = self.start_price * np.cumprod(1.0 + shocks)
        open_ = np.concatenate([[self.start_price], close[:-1]])
        spread = np.abs(rng.normal(0, vol * 0.6, n))
        high = np.maximum(open_, close) * (1.0 + spread)
        low = np.minimum(open_, close) * (1.0 - spread)
        volume = np.abs(rng.normal(120.0, 40.0, n)) + 10.0
        now = time.time()
        ts = now - self._tf * np.arange(n, 0, -1)
        return pd.DataFrame(
            {
                "ts": ts,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }
        )

    def snapshot(self, bars: int) -> MarketSnapshot:
        self._step += 1
        # Tambahkan satu bar baru tiap panggilan agar loop terasa "hidup".
        rng = np.random.default_rng(self.seed + self._step)
        last = self._candles.iloc[-1]
        new_close = float(last["close"] * (1.0 + rng.normal(0.0002, 0.0030)))
        new_open = float(last["close"])
        hi = max(new_open, new_close) * (1.0 + abs(rng.normal(0, 0.0018)))
        lo = min(new_open, new_close) * (1.0 - abs(rng.normal(0, 0.0018)))
        new_row = {
            "ts": float(last["ts"] + self._tf),
            "open": new_open,
            "high": hi,
            "low": lo,
            "close": new_close,
            "volume": float(abs(rng.normal(120.0, 40.0)) + 10.0),
        }
        self._candles = pd.concat(
            [self._candles, pd.DataFrame([new_row])], ignore_index=True
        ).tail(3000)
        price = float(self._candles["close"].iloc[-1])
        return MarketSnapshot(
            symbol=self.symbol,
            timeframe=self.timeframe,
            candles=self._candles.tail(bars).reset_index(drop=True),
            best_bid=price * 0.99995,
            best_ask=price * 1.00005,
            last_price=price,
            ts=time.time(),
            source="synthetic",
        )


class CSVFeed:
    """Baca candle dari CSV (kolom: ts,open,high,low,close,volume)."""

    def __init__(self, path: str | Path, symbol: str, timeframe: str) -> None:
        self.path = Path(path)
        self.symbol = symbol
        self.timeframe = timeframe
        df = pd.read_csv(self.path)
        missing = set(OHLCV_COLUMNS) - set(df.columns)
        if missing:
            raise ValueError(f"CSV kehilangan kolom: {sorted(missing)}")
        if "ts" in df.columns and df["ts"].dtype == object:
            df["ts"] = pd.to_datetime(df["ts"], utc=True).astype("int64") / 1e9
        self._candles = df[OHLCV_COLUMNS].astype(float)

    def snapshot(self, bars: int) -> MarketSnapshot:
        candles = self._candles.tail(bars).reset_index(drop=True)
        price = float(candles["close"].iloc[-1])
        return MarketSnapshot(
            symbol=self.symbol,
            timeframe=self.timeframe,
            candles=candles,
            best_bid=price * 0.99995,
            best_ask=price * 1.00005,
            last_price=price,
            ts=time.time(),
            source=f"csv:{self.path.name}",
        )


class CCXTFeed:
    """Data publik via ccxt. Read-only: tidak memuat kredensial apa pun."""

    def __init__(self, exchange_id: str, symbol: str, timeframe: str) -> None:
        try:
            import ccxt  # import lokal agar modul lain tidak bergantung ccxt
        except ImportError as exc:  # pragma: no cover - jalur dependensi
            raise RuntimeError(
                "ccxt belum terpasang. `pip install ccxt` atau jalankan OFFLINE=true."
            ) from exc
        if not hasattr(ccxt, exchange_id):
            raise ValueError(f"exchange tidak dikenal ccxt: {exchange_id}")
        self.exchange_id = exchange_id
        self.symbol = symbol
        self.timeframe = timeframe
        # Tanpa apiKey/secret: hanya endpoint publik.
        self._ex = getattr(ccxt, exchange_id)({"enableRateLimit": True})

    def snapshot(self, bars: int) -> MarketSnapshot:
        ohlcv = self._ex.fetch_ohlcv(self.symbol, timeframe=self.timeframe, limit=bars)
        if not ohlcv:
            raise RuntimeError("exchange mengembalikan data kosong")
        df = pd.DataFrame(ohlcv, columns=["ts_ms", "open", "high", "low", "close", "volume"])
        df["ts"] = df["ts_ms"] / 1000.0
        df = df[OHLCV_COLUMNS].astype(float)
        ticker = self._ex.fetch_ticker(self.symbol)
        last = float(ticker.get("last") or df["close"].iloc[-1])
        bid = float(ticker.get("bid") or last * 0.9999)
        ask = float(ticker.get("ask") or last * 1.0001)
        return MarketSnapshot(
            symbol=self.symbol,
            timeframe=self.timeframe,
            candles=df.reset_index(drop=True),
            best_bid=bid,
            best_ask=ask,
            last_price=last,
            ts=time.time(),
            source=f"ccxt:{self.exchange_id}",
        )


def build_feed(settings: Settings, csv_path: str | None = None) -> Feed:
    if csv_path:
        return CSVFeed(csv_path, settings.symbol, settings.timeframe)
    if settings.offline:
        return SyntheticFeed(
            symbol=settings.symbol,
            timeframe=settings.timeframe,
            seed=settings.paper_seed,
        )
    return CCXTFeed(settings.exchange_id, settings.symbol, settings.timeframe)


# --------------------------------------------------------------------------- #
# Gate kualitas data
# --------------------------------------------------------------------------- #
@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class GateResult:
    passed: bool
    checks: list[Check] = field(default_factory=list)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def summary(self) -> str:
        if self.passed:
            return "semua pemeriksaan kualitas data lolos"
        return "; ".join(f"{c.name}: {c.detail}" for c in self.failures)


class DataQualityGate:
    """Fail-closed. Satu kegagalan = tidak ada trading."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def check(self, snap: MarketSnapshot) -> GateResult:
        cfg = self.settings.strategy
        df = snap.candles
        checks: list[Check] = []

        checks.append(
            Check(
                "jumlah_bar",
                len(df) >= cfg.min_bars,
                f"{len(df)} bar, minimum {cfg.min_bars}",
            )
        )

        if len(df):
            nulls = int(df[OHLCV_COLUMNS].isna().sum().sum())
            checks.append(Check("nilai_kosong", nulls == 0, f"{nulls} sel kosong"))

            sane = bool(
                (df["high"] >= df["low"]).all()
                and (df["high"] >= df["close"]).all()
                and (df["low"] <= df["close"]).all()
                and (df["close"] > 0).all()
            )
            checks.append(Check("harga_masuk_akal", sane, "high/low/close konsisten"))

            dup = int(df["ts"].duplicated().sum())
            checks.append(Check("timestamp_unik", dup == 0, f"{dup} duplikat"))

            monotonic = bool(df["ts"].is_monotonic_increasing)
            checks.append(Check("timestamp_urut", monotonic, "ts naik monoton"))

            zero_vol = int((df["volume"] <= 0).sum())
            ratio = zero_vol / max(len(df), 1)
            checks.append(
                Check(
                    "volume_hidup",
                    ratio <= 0.02,
                    f"{ratio:.1%} bar bervolume nol (maks 2%)",
                )
            )

            tf = timeframe_seconds(self.settings.timeframe)
            age = snap.ts - float(df["ts"].iloc[-1])
            checks.append(
                Check(
                    "kesegaran_data",
                    age <= tf * 3,
                    f"bar terakhir {age:.0f}s lalu (maks {tf * 3}s)",
                )
            )

            big_gap = float(df["close"].pct_change().abs().tail(50).max() or 0.0)
            checks.append(
                Check(
                    "tanpa_lompatan_ekstrem",
                    big_gap < 0.25,
                    f"perubahan maks 50 bar terakhir {big_gap:.2%}",
                )
            )
        else:
            checks.append(Check("nilai_kosong", False, "tidak ada bar"))

        spread = snap.spread_bps
        checks.append(
            Check(
                "spread_wajar",
                spread <= self.settings.risk.max_spread_bps,
                f"spread {spread:.1f} bps (maks {self.settings.risk.max_spread_bps})",
            )
        )
        checks.append(
            Check(
                "kuotasi_valid",
                snap.best_bid > 0 and snap.best_ask > snap.best_bid,
                f"bid {snap.best_bid:.4f} / ask {snap.best_ask:.4f}",
            )
        )

        return GateResult(passed=all(c.ok for c in checks), checks=checks)
