"""AEGIS-1 — JARVIS #01: AI Core (deterministik, tanpa model bahasa).

"AI" di sini berarti **mesin keputusan berbasis aturan** yang menyusun satu
briefing dari seluruh lapisan: analisis teknikal, microstructure, on-chain,
sentimen berita, portofolio, evaluasi diri, dan status risiko. Tidak ada
panggilan ke model bahasa, tidak ada API key, dan keluarannya bisa direproduksi
persis dari masukan yang sama — syarat mutlak untuk audit.

Tugasnya bukan menambah sinyal baru, melainkan menjawab tiga pertanyaan yang
sering hilang di dashboard: apa yang sedang terjadi, seberapa yakin kita, dan
apa yang harus dilakukan (termasuk "jangan lakukan apa pun").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BriefingInput:
    symbol: str = "—"
    timeframe: str = "—"
    price: float = 0.0
    regime: str = "unknown"
    score: float = 0.0
    side: str | None = None
    vetoes: list[str] = field(default_factory=list)
    htf_trend: str = "flat"
    divergence: str | None = None
    pattern: str | None = None
    squeeze: bool = False
    atr_pct_rank: float = 0.5
    news_score: float = 0.0
    news_label: str = "netral"
    onchain_score: float = 0.0
    onchain_label: str = "netral"
    spread_bps: float = 0.0
    book_imbalance: float = 0.5
    drawdown: float = 0.0
    daily_loss: float = 0.0
    exposure: float = 0.0
    risk_multiplier: float = 1.0
    brier: float = 0.0
    hit_rate: float = 0.0
    drift: str = "stabil"
    concentration: float = 0.0


@dataclass
class Briefing:
    situation: str = ""
    evidence: list[str] = field(default_factory=list)
    decision: str = "tunggu"
    confidence: float = 0.0
    actions: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "situation": self.situation,
            "evidence": self.evidence[:10],
            "decision": self.decision,
            "confidence": round(self.confidence, 3),
            "actions": self.actions[:6],
            "risks": self.risks[:6],
            "tags": self.tags,
        }

    def to_text(self) -> str:
        lines = [f"SITUASI: {self.situation}"]
        if self.evidence:
            lines.append("BUKTI:")
            lines.extend(f"  - {item}" for item in self.evidence[:8])
        lines.append(f"KEPUTUSAN: {self.decision} (keyakinan {self.confidence:.0%})")
        if self.actions:
            lines.append("TINDAKAN:")
            lines.extend(f"  - {item}" for item in self.actions[:5])
        if self.risks:
            lines.append("RISIKO:")
            lines.extend(f"  - {item}" for item in self.risks[:5])
        return "\n".join(lines)


def _agree(value: float, side: str | None) -> float:
    """+1 bila mendukung arah, -1 bila melawan, 0 bila netral."""
    if side not in ("LONG", "SHORT") or abs(value) < 0.05:
        return 0.0
    arah = 1.0 if side == "LONG" else -1.0
    return 1.0 if value * arah > 0 else -1.0


def compose(data: BriefingInput) -> Briefing:
    """Susun briefing dari seluruh lapisan. Murni, deterministik."""
    out = Briefing()

    # ---- 1. Situasi -------------------------------------------------- #
    arah = data.side or "tanpa arah"
    out.situation = (
        f"{data.symbol} {data.timeframe} di {data.price:,.4f} · regime {data.regime} · "
        f"sinyal {arah} skor {data.score:.2f}"
    )
    out.tags = [data.regime, data.htf_trend]
    if data.squeeze:
        out.tags.append("squeeze")

    # ---- 2. Bukti ---------------------------------------------------- #
    if data.htf_trend != "flat":
        out.evidence.append(f"bias HTF {data.htf_trend}")
    if data.divergence:
        out.evidence.append(f"divergensi RSI {data.divergence}")
    if data.pattern:
        out.evidence.append("pola candle " + data.pattern.replace("_", " "))
    if data.news_label != "netral":
        out.evidence.append(f"berita {data.news_label} (skor {data.news_score:+.2f})")
    if data.onchain_label != "netral":
        out.evidence.append(f"on-chain {data.onchain_label} (skor {data.onchain_score:+.2f})")
    if data.book_imbalance:
        sisi = "beli" if data.book_imbalance > 0.5 else "jual"
        out.evidence.append(
            f"order book condong {sisi} (imbalance {data.book_imbalance:.2f}, "
            f"spread {data.spread_bps:.1f} bps)"
        )
    if data.atr_pct_rank >= 0.8:
        out.evidence.append(f"volatilitas tinggi (ATR persentil {data.atr_pct_rank:.2f})")

    # ---- 3. Keyakinan ------------------------------------------------ #
    # Basis: skor konfluensi. Setiap lapisan yang sependapat menambah sedikit,
    # yang melawan mengurangi lebih banyak (asimetri konservatif).
    confidence = data.score
    for value in (data.news_score, data.onchain_score):
        agreement = _agree(value, data.side)
        confidence += 0.05 * agreement
    if data.side in ("LONG", "SHORT"):
        htf_agree = 1.0 if (data.htf_trend == "up" and data.side == "LONG") or \
                           (data.htf_trend == "down" and data.side == "SHORT") else (
            -1.0 if data.htf_trend in ("up", "down") else 0.0)
        confidence += 0.06 * htf_agree
        if data.divergence and (
            (data.divergence == "bearish" and data.side == "LONG")
            or (data.divergence == "bullish" and data.side == "SHORT")
        ):
            confidence -= 0.10
    if data.brier > 0:
        # Kalibrasi buruk (Brier tinggi) menurunkan keyakinan.
        confidence -= max(0.0, min(0.2, (data.brier - 0.25) * 0.8))
    out.confidence = max(0.0, min(1.0, confidence))

    # ---- 4. Keputusan ------------------------------------------------ #
    if data.vetoes:
        out.decision = "hindari"
        out.actions.append("tidak ada entry: " + data.vetoes[0])
    elif data.side is None:
        out.decision = "tunggu"
        out.actions.append("tidak ada setup yang lolos; tunggu bar berikutnya")
    elif out.confidence >= 0.75:
        out.decision = "masuk"
        out.actions.append(
            f"buka {data.side} dengan pengali risiko ×{data.risk_multiplier:.2f}"
        )
    elif out.confidence >= 0.60:
        out.decision = "masuk-kecil"
        out.actions.append(
            f"boleh {data.side} dengan ukuran diperkecil (×{data.risk_multiplier:.2f})"
        )
    else:
        out.decision = "tunggu"
        out.actions.append(
            f"skor {data.score:.2f} belum cukup untuk keyakinan {out.confidence:.2f}"
        )

    # ---- 5. Risiko --------------------------------------------------- #
    if data.drawdown >= 0.05:
        out.risks.append(f"drawdown {data.drawdown:.1%} menekan batas")
    if data.daily_loss >= 0.02:
        out.risks.append(f"rugi harian {data.daily_loss:.1%}")
    if data.exposure >= 0.15:
        out.risks.append(f"exposure {data.exposure:.1%} ekuitas")
    if data.spread_bps > 8:
        out.risks.append(f"spread lebar {data.spread_bps:.1f} bps")
    if data.drift == "memburuk":
        out.risks.append("performa 20 trade terakhir memburuk (drift)")
    if data.concentration >= 0.4:
        out.risks.append(f"portofolio terkonsentrasi (HHI {data.concentration:.2f})")
    if data.risk_multiplier <= 0.6:
        out.risks.append(f"pengali risiko diturunkan ke ×{data.risk_multiplier:.2f}")
    if not out.risks:
        out.risks.append("tidak ada pelanggaran guardrail yang tercatat")
    return out
