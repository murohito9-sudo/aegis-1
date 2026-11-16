"""AEGIS-1 — lapisan analisis tambahan (multi-timeframe, divergensi, pola, squeeze).

Semua fungsi di sini menerima candle yang SUDAH ditutup dan tidak pernah
memakai bar di masa depan:

* ``resample_closed`` hanya memakai bucket waktu yang lengkap; bucket terakhir
  yang belum penuh dibuang, bukan diisi harga parsial.
* ``htf_bias`` membaca bucket lengkap terakhir, jadi nilainya sama dengan apa
  yang benar-benar terlihat oleh trader pada saat bar itu tutup.
* ``rsi_divergence`` membandingkan dua swing terakhir, bukan titik acak.
* ``candle_pattern`` hanya memakai dua bar terakhir.

Modul ini tidak mengubah apa pun; ia memberi bahan bagi ``strategy.evaluate``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

OHLCV = ["ts", "open", "high", "low", "close", "volume"]


# --------------------------------------------------------------------------- #
# Multi-timeframe
# --------------------------------------------------------------------------- #
def resample_closed(df: pd.DataFrame, base_tf_seconds: int, factor: int) -> pd.DataFrame:
    """Gabungkan ``factor`` bar menjadi satu bar HTF, hanya yang sudah lengkap."""
    if factor <= 1 or df.empty:
        return df[OHLCV].copy()
    htf = max(1, int(base_tf_seconds)) * int(factor)
    tmp = df.copy()
    tmp["bucket"] = (tmp["ts"].astype(float) // htf) * htf
    rows: list[dict[str, float]] = []
    closing_offset = htf - float(base_tf_seconds)   # offset bar penutup bucket
    for bucket, chunk in tmp.groupby("bucket", sort=True):
        # Bucket dianggap lengkap hanya bila bar penutupnya ada.
        if float(chunk["ts"].iloc[-1]) < bucket + closing_offset - 1e-6:
            continue
        rows.append({
            "ts": float(bucket),
            "open": float(chunk["open"].iloc[0]),
            "high": float(chunk["high"].max()),
            "low": float(chunk["low"].min()),
            "close": float(chunk["close"].iloc[-1]),
            "volume": float(chunk["volume"].sum()),
        })
    return pd.DataFrame(rows, columns=OHLCV)


def _ema(values: pd.Series, span: int) -> pd.Series:
    return values.ewm(span=span, adjust=False).mean()


@dataclass
class HtfBias:
    trend: str = "flat"          # "up" | "down" | "flat"
    strength: float = 0.0        # 0..1, seberapa jauh harga dari EMA lambat (dalam ATR)
    bars: int = 0
    detail: str = ""

    def as_dict(self) -> dict[str, object]:
        return {"trend": self.trend, "strength": round(self.strength, 4),
                "bars": self.bars, "detail": self.detail}


def htf_bias(
    df: pd.DataFrame,
    base_tf_seconds: int,
    factor: int = 4,
    fast: int = 20,
    slow: int = 50,
) -> HtfBias:
    """Bias timeframe lebih tinggi dari bar yang sudah lengkap."""
    htf = resample_closed(df, base_tf_seconds, factor)
    if len(htf) < slow + 2:
        return HtfBias(bars=len(htf), detail=f"HTF belum matang ({len(htf)} bar)")

    close = htf["close"]
    ema_f = _ema(close, fast)
    ema_s = _ema(close, slow)
    f_last = float(ema_f.iloc[-1])
    s_last = float(ema_s.iloc[-1])
    price = float(close.iloc[-1])

    rng = (htf["high"] - htf["low"]).rolling(14).mean()
    atr = float(rng.iloc[-1]) if not pd.isna(rng.iloc[-1]) else 0.0
    spread = abs(price - s_last)
    strength = min(1.0, spread / (atr * 3.0)) if atr > 0 else 0.0

    if f_last > s_last and price > f_last:
        trend = "up"
    elif f_last < s_last and price < f_last:
        trend = "down"
    else:
        trend = "flat"
    detail = f"HTF×{factor}: EMA{fast} {f_last:.4f} vs EMA{slow} {s_last:.4f}, harga {price:.4f}"
    return HtfBias(trend=trend, strength=strength, bars=len(htf), detail=detail)


# --------------------------------------------------------------------------- #
# Divergensi RSI
# --------------------------------------------------------------------------- #
def _swing_indices(series: pd.Series, window: int, kind: str) -> list[int]:
    out: list[int] = []
    values = series.to_numpy()
    n = len(values)
    for i in range(window, n - window):
        left = values[i - window:i]
        right = values[i + 1:i + window + 1]
        if kind == "high":
            if values[i] >= left.max() and values[i] >= right.max():
                out.append(i)
        else:
            if values[i] <= left.min() and values[i] <= right.min():
                out.append(i)
    return out


def rsi_divergence(
    df: pd.DataFrame,
    lookback: int = 60,
    window: int = 2,
    min_rsi_gap: float = 1.5,
) -> str | None:
    """Kembalikan ``"bullish"``, ``"bearish"``, atau ``None``.

    Bullish: harga membuat low lebih rendah tetapi RSI membuat low lebih tinggi.
    Bearish: harga membuat high lebih tinggi tetapi RSI membuat high lebih rendah.
    """
    if "rsi" not in df.columns or len(df) < 20:
        return None
    tail = df.tail(max(20, lookback)).reset_index(drop=True)
    if tail["rsi"].isna().tail(5).any():
        return None

    highs = _swing_indices(tail["high"], window, "high")
    if len(highs) >= 2:
        a, b = highs[-2], highs[-1]
        if (tail["high"].iloc[b] > tail["high"].iloc[a]
                and tail["rsi"].iloc[b] < tail["rsi"].iloc[a] - min_rsi_gap):
            return "bearish"

    lows = _swing_indices(tail["low"], window, "low")
    if len(lows) >= 2:
        a, b = lows[-2], lows[-1]
        if (tail["low"].iloc[b] < tail["low"].iloc[a]
                and tail["rsi"].iloc[b] > tail["rsi"].iloc[a] + min_rsi_gap):
            return "bullish"
    return None


# --------------------------------------------------------------------------- #
# Pola candle
# --------------------------------------------------------------------------- #
def candle_pattern(df: pd.DataFrame) -> str | None:
    """Pola dua bar terakhir: engulfing, hammer, shooting star."""
    if len(df) < 3:
        return None
    prev = df.iloc[-2]
    last = df.iloc[-1]

    po, pc = float(prev["open"]), float(prev["close"])
    lo, lc, lh, ll = float(last["open"]), float(last["close"]), float(last["high"]), float(last["low"])
    body = abs(lc - lo)
    rng = max(lh - ll, 1e-12)

    prev_bear = pc < po
    prev_bull = pc > po
    if prev_bear and lc > lo and lc >= po and lo <= pc:
        return "bullish_engulfing"
    if prev_bull and lc < lo and lc <= po and lo >= pc:
        return "bearish_engulfing"

    upper = lh - max(lo, lc)
    lower = min(lo, lc) - ll
    if body / rng < 0.35:
        if lower >= 2 * body and upper <= body:
            return "hammer"
        if upper >= 2 * body and lower <= body:
            return "shooting_star"
    return None


# --------------------------------------------------------------------------- #
# Squeeze / kompresi volatilitas
# --------------------------------------------------------------------------- #
def squeeze_state(df: pd.DataFrame, window: int = 40, percentile: float = 0.25) -> tuple[bool, float]:
    """True bila rentang harga sedang sangat sempit dibanding riwayatnya."""
    if len(df) < window + 10:
        return False, 0.5
    close = df["close"].astype(float)
    bandwidth = (close.rolling(window).max() - close.rolling(window).min()) / close
    bandwidth = bandwidth.dropna()
    if bandwidth.empty:
        return False, 0.5
    current = float(bandwidth.iloc[-1])
    rank = float((bandwidth < current).mean())
    return rank <= percentile, rank


def atr_percentile(df: pd.DataFrame, column: str = "atr_pct_rank") -> float:
    """Ambil peringkat ATR terakhir (0..1); 0.5 bila belum tersedia."""
    if column not in df.columns or df.empty:
        return 0.5
    value = df[column].iloc[-1]
    return 0.5 if pd.isna(value) else float(np.clip(value, 0.0, 1.0))
