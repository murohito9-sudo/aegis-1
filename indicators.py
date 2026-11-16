"""AEGIS-1 — indikator teknis.

Semua fungsi murni (tidak menyimpan state) dan bekerja di atas pandas Series /
DataFrame supaya mudah diuji ulang terhadap perhitungan referensi.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from config import StrategyConfig


# --------------------------------------------------------------------------- #
# Dasar
# --------------------------------------------------------------------------- #
def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI metode Wilder (smoothing alpha = 1/period)."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    # Jika tidak ada loss sama sekali, RSI = 100.
    return out.fillna(100.0).where(avg_gain.notna(), np.nan)


def macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def roc(close: pd.Series, period: int = 10) -> pd.Series:
    return close.pct_change(periods=period) * 100.0


def relative_volume(volume: pd.Series, lookback: int = 20) -> pd.Series:
    """Volume bar ini dibanding rata-rata ``lookback`` bar sebelumnya (tanpa look-ahead)."""
    baseline = volume.shift(1).rolling(lookback, min_periods=lookback).mean()
    return volume / baseline.replace(0.0, np.nan)


def atr_percentile(df: pd.DataFrame, window: int = 100) -> pd.Series:
    """Persentil peringkat ATR saat ini di dalam jendela terakhir."""
    return df["atr"].rolling(window, min_periods=20).rank(pct=True)


def ema_slope(series: pd.Series, bars: int = 10) -> pd.Series:
    return (series - series.shift(bars)) / bars


# --------------------------------------------------------------------------- #
# Struktur pasar
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MarketStructure:
    last_swing_high: float | None
    last_swing_low: float | None
    resistance: float | None
    support: float | None
    break_of_structure: str | None   # "bullish" | "bearish" | None
    bars_since_bos: int | None
    swing_high_ts: float | None
    swing_low_ts: float | None


def swing_points(df: pd.DataFrame, lookback: int = 5) -> tuple[pd.Series, pd.Series]:
    """Titik swing terkonfirmasi (fraktal ``lookback`` kiri-kanan).

    Sebuah bar baru dianggap swing high bila high-nya lebih tinggi dari
    ``lookback`` bar di kiri dan ``lookback`` bar di kanan. Karena butuh bar di
    kanan, nilai di indeks terakhir selalu False — inilah yang mencegah
    look-ahead: level hanya "ada" setelah terkonfirmasi.
    """
    highs, lows = df["high"], df["low"]
    n = len(df)
    sh = np.zeros(n, dtype=bool)
    sl = np.zeros(n, dtype=bool)
    if n < lookback * 2 + 1:
        return pd.Series(sh, index=df.index), pd.Series(sl, index=df.index)
    h = highs.to_numpy()
    lo = lows.to_numpy()
    for i in range(lookback, n - lookback):
        window_h = h[i - lookback : i + lookback + 1]
        window_l = lo[i - lookback : i + lookback + 1]
        if h[i] == window_h.max() and (window_h == h[i]).sum() == 1:
            sh[i] = True
        if lo[i] == window_l.min() and (window_l == lo[i]).sum() == 1:
            sl[i] = True
    return pd.Series(sh, index=df.index), pd.Series(sl, index=df.index)


def market_structure(df: pd.DataFrame, lookback: int = 5) -> MarketStructure:
    """Level swing, support/resistance, dan status break of structure."""
    if len(df) < lookback * 2 + 2:
        return MarketStructure(None, None, None, None, None, None, None, None)

    sh_mask, sl_mask = swing_points(df, lookback)
    # Bar terakhir belum bisa jadi swing (butuh konfirmasi kanan), jadi cari
    # swing terkonfirmasi terakhir sebelum bar berjalan.
    sh_idx = df.index[sh_mask][-1] if sh_mask.any() else None
    sl_idx = df.index[sl_mask][-1] if sl_mask.any() else None

    last_high = float(df.loc[sh_idx, "high"]) if sh_idx is not None else None
    last_low = float(df.loc[sl_idx, "low"]) if sl_idx is not None else None
    high_ts = float(df.loc[sh_idx, "ts"]) if sh_idx is not None else None
    low_ts = float(df.loc[sl_idx, "ts"]) if sl_idx is not None else None

    close = float(df["close"].iloc[-1])
    bos: str | None = None
    bars_since: int | None = None
    if last_high is not None and close > last_high:
        bos = "bullish"
        bars_since = len(df) - 1 - int(df.index.get_loc(sh_idx))
    elif last_low is not None and close < last_low:
        bos = "bearish"
        bars_since = len(df) - 1 - int(df.index.get_loc(sl_idx))

    # Support/resistance sederhana: swing terdekat di sekitar harga.
    highs = df.loc[sh_mask, "high"] if sh_mask.any() else pd.Series(dtype=float)
    lows = df.loc[sl_mask, "low"] if sl_mask.any() else pd.Series(dtype=float)
    resistance = float(highs[highs > close].min()) if (highs > close).any() else last_high
    support = float(lows[lows < close].max()) if (lows < close).any() else last_low

    return MarketStructure(
        last_swing_high=last_high,
        last_swing_low=last_low,
        resistance=resistance,
        support=support,
        break_of_structure=bos,
        bars_since_bos=bars_since,
        swing_high_ts=high_ts,
        swing_low_ts=low_ts,
    )


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
def add_indicators(df: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    """Tambahkan seluruh kolom indikator. Input tidak dimodifikasi."""
    out = df.copy()
    close = out["close"]

    out["ema_fast"] = ema(close, cfg.ema_fast)
    out["ema_slow"] = ema(close, cfg.ema_slow)
    out["rsi"] = rsi(close, cfg.rsi_period)
    macd_line, macd_sig, macd_hist = macd(
        close, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal
    )
    out["macd"] = macd_line
    out["macd_signal"] = macd_sig
    out["macd_hist"] = macd_hist
    out["atr"] = atr(out, cfg.atr_period)
    out["roc"] = roc(close, cfg.roc_period)
    out["rel_volume"] = relative_volume(out["volume"], cfg.volume_lookback)
    out["atr_pct_rank"] = atr_percentile(out, cfg.atr_percentile_window)
    out["ema_slope"] = ema_slope(out["ema_slow"], 10)
    return out
