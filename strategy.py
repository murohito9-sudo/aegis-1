"""AEGIS-1 — strategi multi-konfirmasi.

Tidak ada sinyal tunggal yang boleh memicu entry. Setiap sinyal wajib:

1. Lolos trend filter (EMA50/EMA200 atau breakout terkonfirmasi).
2. Lolos filter momentum (RSI tidak ekstrem + MACD searah + ROC searah).
3. Lolos konfirmasi volume relatif.
4. Punya level struktur yang jelas (swing high/low, support/resistance, BOS).
5. Memberi reward:risk >= minimum.
6. Mencapai skor konfluensi minimum.

Setiap keputusan mengembalikan ``Signal`` berisi ``reasons`` (kenapa layak) dan
``vetoes`` (kenapa ditolak), sehingga bisa direkonstruksi dari log.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from config import Regime, RiskConfig, StrategyConfig
from indicators import add_indicators, market_structure

LONG = "LONG"
SHORT = "SHORT"


@dataclass
class Signal:
    symbol: str
    bar_ts: float
    side: str | None
    score: float
    entry: float
    stop: float
    take_profit: float
    reward_risk: float
    regime: str
    atr: float
    reasons: list[str] = field(default_factory=list)
    vetoes: list[str] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        return self.side is not None and not self.vetoes

    @property
    def signal_id(self) -> str:
        return f"{self.symbol}:{int(self.bar_ts)}:{self.side or 'NONE'}"

    def risk_per_unit(self) -> float:
        return abs(self.entry - self.stop)

    def as_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "bar_ts": self.bar_ts,
            "side": self.side,
            "score": round(self.score, 4),
            "entry": self.entry,
            "stop": self.stop,
            "take_profit": self.take_profit,
            "reward_risk": round(self.reward_risk, 2),
            "regime": self.regime,
            "atr": self.atr,
            "reasons": self.reasons,
            "vetoes": self.vetoes,
            "actionable": self.actionable,
            "signal_id": self.signal_id,
        }


# --------------------------------------------------------------------------- #
# Regime
# --------------------------------------------------------------------------- #
def detect_regime(df: pd.DataFrame, cfg: StrategyConfig) -> Regime:
    """Klasifikasi regime dari bar terakhir yang sudah selesai."""
    last = df.iloc[-1]
    if pd.isna(last["ema_slow"]) or pd.isna(last["atr"]):
        return Regime.UNKNOWN

    rel_vol = float(last["rel_volume"]) if not pd.isna(last["rel_volume"]) else 0.0
    if rel_vol < cfg.low_liquidity_ratio:
        return Regime.LOW_LIQUIDITY

    atr_rank = float(last["atr_pct_rank"]) if not pd.isna(last["atr_pct_rank"]) else 0.5
    if atr_rank >= cfg.high_vol_percentile:
        return Regime.HIGH_VOLATILITY

    close = float(last["close"])
    ema_f = float(last["ema_fast"])
    ema_s = float(last["ema_slow"])
    slope = float(last["ema_slope"]) if not pd.isna(last["ema_slope"]) else 0.0

    if ema_f > ema_s and slope > 0 and close > ema_f:
        return Regime.TRENDING_UP
    if ema_f < ema_s and slope < 0 and close < ema_f:
        return Regime.TRENDING_DOWN
    return Regime.RANGING


# --------------------------------------------------------------------------- #
# Evaluasi
# --------------------------------------------------------------------------- #
WEIGHTS = {
    "trend": 0.25,
    "momentum": 0.15,
    "macd": 0.15,
    "volume": 0.15,
    "structure": 0.20,
    "roc": 0.10,
}


def _no_signal(symbol: str, bar_ts: float, reason: str, regime: str, atr_v: float = 0.0) -> Signal:
    return Signal(
        symbol=symbol,
        bar_ts=bar_ts,
        side=None,
        score=0.0,
        entry=0.0,
        stop=0.0,
        take_profit=0.0,
        reward_risk=0.0,
        regime=regime,
        atr=atr_v,
        vetoes=[reason],
    )


def evaluate(
    df: pd.DataFrame,
    cfg: StrategyConfig,
    risk_cfg: RiskConfig,
    symbol: str = "BTC/USDT",
) -> Signal:
    """Hitung sinyal dari candle yang SUDAH selesai (bar terakhir dianggap closed).

    Pemanggil bertanggung jawab membuang bar yang belum ditutup. Di ``main.py``
    bar berjalan dibuang sebelum fungsi ini dipanggil.
    """
    if len(df) < cfg.min_bars:
        ts = float(df["ts"].iloc[-1]) if len(df) else 0.0
        return _no_signal(symbol, ts, f"data kurang: {len(df)} < {cfg.min_bars}", Regime.UNKNOWN)

    ind = add_indicators(df, cfg)
    last = ind.iloc[-1]
    bar_ts = float(last["ts"])

    if pd.isna(last["ema_slow"]) or pd.isna(last["atr"]) or pd.isna(last["rsi"]):
        return _no_signal(symbol, bar_ts, "indikator belum matang", Regime.UNKNOWN)

    regime = detect_regime(ind, cfg)
    close = float(last["close"])
    ema_f = float(last["ema_fast"])
    ema_s = float(last["ema_slow"])
    rsi_v = float(last["rsi"])
    macd_v, macd_s, macd_h = float(last["macd"]), float(last["macd_signal"]), float(last["macd_hist"])
    atr_v = float(last["atr"])
    roc_v = float(last["roc"]) if not pd.isna(last["roc"]) else 0.0
    rel_vol = float(last["rel_volume"]) if not pd.isna(last["rel_volume"]) else 0.0

    struct = market_structure(ind, cfg.swing_lookback)

    if regime == Regime.LOW_LIQUIDITY:
        return _no_signal(symbol, bar_ts, f"likuiditas rendah (rel-vol {rel_vol:.2f})", regime.value, atr_v)
    if regime == Regime.UNKNOWN:
        return _no_signal(symbol, bar_ts, "regime tidak dapat ditentukan", regime.value, atr_v)

    long_votes: dict[str, bool] = {}
    short_votes: dict[str, bool] = {}
    long_reasons: list[str] = []
    short_reasons: list[str] = []

    # ---- Trend filter -----------------------------------------------------
    bos = struct.break_of_structure
    long_trend = close > ema_f and ema_f > ema_s
    long_breakout = bos == "bullish" and close > ema_f
    short_trend = close < ema_f and ema_f < ema_s
    short_breakdown = bos == "bearish" and close < ema_f
    long_votes["trend"] = bool(long_trend or long_breakout)
    short_votes["trend"] = bool(short_trend or short_breakdown)
    if long_trend:
        long_reasons.append(f"harga {close:.2f} di atas EMA{cfg.ema_fast} dan EMA{cfg.ema_slow} (tren naik)")
    elif long_breakout:
        long_reasons.append("breakout terkonfirmasi di atas swing high dengan harga di atas EMA cepat")
    if short_trend:
        short_reasons.append(f"harga {close:.2f} di bawah EMA{cfg.ema_fast} dan EMA{cfg.ema_slow} (tren turun)")
    elif short_breakdown:
        short_reasons.append("breakdown terkonfirmasi di bawah swing low dengan harga di bawah EMA cepat")

    # ---- Momentum: RSI ------------------------------------------------
    long_votes["momentum"] = 45.0 <= rsi_v <= cfg.rsi_overbought
    short_votes["momentum"] = cfg.rsi_oversold <= rsi_v <= 55.0
    if long_votes["momentum"]:
        long_reasons.append(f"RSI {rsi_v:.1f} mendukung tetapi belum overbought ekstrem")
    if short_votes["momentum"]:
        short_reasons.append(f"RSI {rsi_v:.1f} mendukung tetapi belum oversold ekstrem")

    # ---- MACD ---------------------------------------------------------
    long_votes["macd"] = macd_v > macd_s and macd_h > 0
    short_votes["macd"] = macd_v < macd_s and macd_h < 0
    if long_votes["macd"]:
        long_reasons.append("MACD di atas signal dengan histogram positif")
    if short_votes["macd"]:
        short_reasons.append("MACD di bawah signal dengan histogram negatif")

    # ---- Volume -------------------------------------------------------
    vol_ok = rel_vol >= risk_cfg.min_relative_volume
    long_votes["volume"] = vol_ok
    short_votes["volume"] = vol_ok
    if vol_ok:
        msg = f"volume relatif {rel_vol:.2f}x rata-rata {cfg.volume_lookback} bar"
        long_reasons.append(msg)
        short_reasons.append(msg)

    # ---- Struktur -----------------------------------------------------
    long_votes["structure"] = bool(
        bos == "bullish"
        or (struct.last_swing_low is not None and close > struct.last_swing_low and close > ema_f)
    )
    short_votes["structure"] = bool(
        bos == "bearish"
        or (struct.last_swing_high is not None and close < struct.last_swing_high and close < ema_f)
    )
    if bos == "bullish":
        long_reasons.append(f"break of structure bullish (swing high {struct.last_swing_high:.2f} ditembus)")
    elif struct.last_swing_low is not None and long_votes["structure"]:
        long_reasons.append(f"struktur higher-low terjaga di atas swing low {struct.last_swing_low:.2f}")
    if bos == "bearish":
        short_reasons.append(f"break of structure bearish (swing low {struct.last_swing_low:.2f} ditembus)")
    elif struct.last_swing_high is not None and short_votes["structure"]:
        short_reasons.append(f"struktur lower-high terjaga di bawah swing high {struct.last_swing_high:.2f}")

    # ---- ROC ----------------------------------------------------------
    long_votes["roc"] = roc_v > 0
    short_votes["roc"] = roc_v < 0
    if long_votes["roc"]:
        long_reasons.append(f"rate of change {roc_v:+.2f}% dalam {cfg.roc_period} bar")
    if short_votes["roc"]:
        short_reasons.append(f"rate of change {roc_v:+.2f}% dalam {cfg.roc_period} bar")

    long_score = sum(WEIGHTS[k] for k, v in long_votes.items() if v)
    short_score = sum(WEIGHTS[k] for k, v in short_votes.items() if v)

    # Arah dipilih dari skor tertinggi; kalau seri, tidak ada sinyal.
    if abs(long_score - short_score) < 1e-9:
        return _no_signal(symbol, bar_ts, "konfluensi seimbang dua arah", regime.value, atr_v)
    side = LONG if long_score > short_score else SHORT
    score = long_score if side == LONG else short_score
    reasons = long_reasons if side == LONG else short_reasons
    votes = long_votes if side == LONG else short_votes

    vetoes: list[str] = []
    # Hard requirement: trend, MACD, volume tidak boleh gagal.
    for key, label in (("trend", "trend filter"), ("macd", "MACD"), ("volume", "volume")):
        if not votes[key]:
            vetoes.append(f"{label} tidak mendukung arah {side}")
    if not votes["momentum"]:
        vetoes.append(f"RSI {rsi_v:.1f} ekstrem/tidak mendukung arah {side}")
    if not votes["structure"]:
        vetoes.append("tidak ada struktur pasar yang mendukung")

    required_score = cfg.min_confluence
    if regime == Regime.HIGH_VOLATILITY:
        required_score += 0.10
        reasons.append("regime volatilitas tinggi: ambang konfluensi dinaikkan")
    if regime == Regime.RANGING:
        if bos not in ("bullish", "bearish"):
            vetoes.append("regime ranging tanpa break of structure")
    # Melawan tren utama butuh konfirmasi kuat.
    if regime == Regime.TRENDING_DOWN and side == LONG:
        required_score += 0.10
        reasons.append("melawan tren turun: butuh konfluensi lebih tinggi")
    if regime == Regime.TRENDING_UP and side == SHORT:
        required_score += 0.10
        reasons.append("melawan tren naik: butuh konfluensi lebih tinggi")

    if score < required_score:
        vetoes.append(f"skor konfluensi {score:.2f} < ambang {required_score:.2f}")

    # ---- Stop-loss & target -------------------------------------------
    buffer = risk_cfg.structure_buffer_atr * atr_v
    if side == LONG:
        atr_stop = close - risk_cfg.atr_stop_mult * atr_v
        struct_stop = (
            struct.last_swing_low - buffer if struct.last_swing_low is not None else atr_stop
        )
        stop = min(atr_stop, struct_stop)
        risk_per_unit = close - stop
        if risk_per_unit > risk_cfg.atr_stop_mult * atr_v * 1.8:
            # Swing low terlalu jauh untuk dipakai sebagai stop: pakai stop ATR
            # sebagai gantinya. Ini bukan alasan membatalkan setup, tapi dicatat.
            stop = atr_stop
            risk_per_unit = close - stop
            reasons.append("swing low terlalu jauh; stop memakai 1.8 ATR")
        resistance = struct.resistance
        if resistance is not None and resistance > close:
            if (resistance - close) < risk_cfg.min_reward_risk * risk_per_unit:
                vetoes.append(
                    f"resistance {resistance:.2f} terlalu dekat (<{risk_cfg.min_reward_risk:.1f}R)"
                )
            take_profit = max(resistance, close + risk_cfg.min_reward_risk * risk_per_unit)
        else:
            take_profit = close + risk_cfg.min_reward_risk * risk_per_unit
        # Batas atas target agar tidak fantasi: maksimum 4R.
        take_profit = min(take_profit, close + 4.0 * risk_per_unit)
    else:
        atr_stop = close + risk_cfg.atr_stop_mult * atr_v
        struct_stop = (
            struct.last_swing_high + buffer if struct.last_swing_high is not None else atr_stop
        )
        stop = max(atr_stop, struct_stop)
        risk_per_unit = stop - close
        if risk_per_unit > risk_cfg.atr_stop_mult * atr_v * 1.8:
            stop = atr_stop
            risk_per_unit = stop - close
            reasons.append("swing high terlalu jauh; stop memakai 1.8 ATR")
        support = struct.support
        if support is not None and support < close:
            if (close - support) < risk_cfg.min_reward_risk * risk_per_unit:
                vetoes.append(f"support {support:.2f} terlalu dekat (<{risk_cfg.min_reward_risk:.1f}R)")
            take_profit = min(support, close - risk_cfg.min_reward_risk * risk_per_unit)
        else:
            take_profit = close - risk_cfg.min_reward_risk * risk_per_unit
        take_profit = max(take_profit, close - 4.0 * risk_per_unit)

    if risk_per_unit <= 0.25 * atr_v:
        vetoes.append("stop terlalu rapat (<0.25 ATR), risiko noise")
    reward_risk = abs(take_profit - close) / risk_per_unit if risk_per_unit > 0 else 0.0
    if reward_risk < risk_cfg.min_reward_risk:
        vetoes.append(
            f"reward:risk {reward_risk:.2f} < minimum {risk_cfg.min_reward_risk:.1f}"
        )

    return Signal(
        symbol=symbol,
        bar_ts=bar_ts,
        side=side if not vetoes else None,
        score=score,
        entry=close,
        stop=round(stop, 8),
        take_profit=round(take_profit, 8),
        reward_risk=reward_risk,
        regime=regime.value,
        atr=atr_v,
        reasons=reasons,
        vetoes=vetoes,
    )
