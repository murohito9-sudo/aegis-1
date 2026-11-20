"""Unified JARVIS intelligence contract for AEGIS-ONE.

This module is deliberately read-only. It combines already-computed engine
outputs and provider health into one auditable report; it does not create
signals, change risk limits, or place orders.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
from typing import Any, Mapping


@dataclass(frozen=True)
class ProviderStatus:
    name: str
    available: bool = False
    source: str = "unconfigured"
    observed_at: float | None = None
    max_age_s: float = 0.0
    detail: str = ""

    @property
    def fresh(self) -> bool:
        if not self.available or self.observed_at is None:
            return False
        if not math.isfinite(self.observed_at):
            return False
        if self.max_age_s <= 0:
            return True
        # The caller supplies evaluation time to report(); this property only
        # validates provider metadata and is not used for age comparison.
        return True


@dataclass
class IntelligenceReport:
    status: str = "degraded"
    decision: str = "wait"
    symbol: str = ""
    timeframe: str = ""
    evaluated_at: float = 0.0
    strategy: dict[str, Any] = field(default_factory=dict)
    providers: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    missing_inputs: list[str] = field(default_factory=list)
    vetoes: list[str] = field(default_factory=list)
    risk: dict[str, Any] = field(default_factory=dict)
    explanation: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def compose(
    *,
    symbol: str,
    timeframe: str,
    evaluated_at: float,
    strategy: Mapping[str, Any] | None,
    market: Mapping[str, Any] | None = None,
    providers: list[ProviderStatus] | None = None,
    risk: Mapping[str, Any] | None = None,
) -> IntelligenceReport:
    """Create a deterministic, provenance-aware report from existing outputs.

    Missing providers are explicitly reported and never treated as neutral
    confirmation. Existing strategy vetoes and risk lockouts take precedence.
    """
    now = float(evaluated_at)
    if not math.isfinite(now):
        raise ValueError("evaluated_at harus finite")

    strategy_data = dict(strategy or {})
    market_data = dict(market or {})
    risk_data = dict(risk or {})
    report = IntelligenceReport(
        symbol=str(symbol),
        timeframe=str(timeframe),
        evaluated_at=now,
        strategy=strategy_data,
        risk=risk_data,
    )

    provider_rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for item in providers or []:
        age = None if item.observed_at is None else max(0.0, now - float(item.observed_at))
        fresh = bool(
            item.available
            and item.observed_at is not None
            and math.isfinite(float(item.observed_at))
            and (item.max_age_s <= 0 or age is not None and age <= item.max_age_s)
        )
        provider_rows.append({
            "name": item.name,
            "available": bool(item.available),
            "fresh": fresh,
            "source": item.source,
            "observed_at": item.observed_at,
            "age_s": age,
            "detail": item.detail,
        })
        if not fresh:
            missing.append(item.name)

    report.providers = provider_rows
    report.missing_inputs = missing
    side = strategy_data.get("side")
    score = strategy_data.get("score")
    report.evidence.append(
        f"strategy side={side or 'none'} score={score if score is not None else 'unknown'}"
    )
    for key in ("regime", "htf_trend", "divergence", "pattern"):
        value = strategy_data.get(key)
        if value not in (None, "", "unknown", "flat"):
            report.evidence.append(f"{key}={value}")

    source = market_data.get("source")
    if source:
        report.evidence.append(f"market_source={source}")
    market_ts = market_data.get("observed_at")
    if market_ts is not None:
        age = max(0.0, now - float(market_ts))
        report.evidence.append(f"market_data_age_s={age:.1f}")

    vetoes = [str(x) for x in strategy_data.get("vetoes", []) if str(x).strip()]
    if risk_data.get("blocked"):
        vetoes.append(str(risk_data.get("block_reason") or "risk gate blocked"))
    if risk_data.get("kill_switch"):
        vetoes.append("kill-switch active")
    if risk_data.get("mode") not in (None, "PAPER"):
        vetoes.append(f"engine mode is {risk_data.get('mode')}")

    report.vetoes = list(dict.fromkeys(vetoes))
    if report.vetoes:
        report.status = "blocked"
        report.decision = "avoid"
        report.explanation = "Veto aktif; tidak ada tindakan simulasi yang diusulkan."
    elif side not in ("LONG", "SHORT"):
        report.status = "ready" if not missing else "degraded"
        report.decision = "wait"
        report.explanation = "Belum ada sinyal strategi yang valid."
    elif missing:
        report.status = "degraded"
        report.decision = "wait"
        report.explanation = "Sinyal tersedia, tetapi input intelligence belum lengkap atau sudah kedaluwarsa."
    else:
        report.status = "ready"
        report.decision = "review_paper"
        report.explanation = "Bukti tersedia; keputusan tetap harus melewati risk gate dan paper broker."

    if missing:
        report.evidence.append("unavailable_or_stale=" + ", ".join(missing))
    return report
