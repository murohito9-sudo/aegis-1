"""AEGIS-1 — JARVIS #17: Voice System (narasi + TTS opsional).

Dua lapis, sengaja dipisah:

* ``narrate`` — mengubah briefing jadi kalimat yang enak didengar. Murni,
  deterministik, bisa diuji tanpa perangkat audio.
* ``speak`` — memanggil mesin TTS yang ada di sistem (``espeak-ng``, ``espeak``,
  atau ``say`` di macOS). Kalau tidak ada, ia melaporkan itu apa adanya dan
  tidak berpura-pura bicara.

Tidak ada layanan suara berbayar, tidak ada API key, dan tidak ada audio yang
dikirim ke luar mesin.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

ENGINES = ("espeak-ng", "espeak", "say")


def _label_decision(decision: str) -> str:
    return {
        "masuk": "masuk posisi",
        "masuk-kecil": "masuk dengan ukuran kecil",
        "tunggu": "tunggu",
        "hindari": "hindari entry",
    }.get(decision, decision)


def narrate(briefing: Any, max_evidence: int = 3) -> str:
    """Ubah ``Briefing`` (atau dict hasilnya) jadi narasi pendek."""
    data = briefing.as_dict() if hasattr(briefing, "as_dict") else dict(briefing or {})
    situation = str(data.get("situation") or "kondisi pasar belum tersedia")
    decision = _label_decision(str(data.get("decision") or "tunggu"))
    confidence = float(data.get("confidence") or 0.0)
    evidence = [str(item) for item in (data.get("evidence") or [])][:max_evidence]
    risks = [str(item) for item in (data.get("risks") or [])][:2]

    parts = [f"{situation}."]
    if evidence:
        parts.append("Bukti utama: " + ", ".join(evidence) + ".")
    parts.append(f"Keputusan: {decision}, keyakinan {confidence:.0%}.")
    if risks:
        parts.append("Perhatian: " + "; ".join(risks) + ".")
    return " ".join(parts)


def available_engines() -> list[str]:
    return [name for name in ENGINES if shutil.which(name)]


@dataclass
class SpeechResult:
    spoken: bool = False
    engine: str = ""
    reason: str = ""
    text: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"spoken": self.spoken, "engine": self.engine,
                "reason": self.reason, "chars": len(self.text)}


def speak(text: str, dry_run: bool = True, engine: str | None = None,
          timeout: float = 20.0) -> SpeechResult:
    """Bacakan ``text`` bila mesin TTS tersedia dan bukan dry-run."""
    result = SpeechResult(text=text)
    if not text.strip():
        result.reason = "teks kosong"
        return result
    if dry_run:
        result.reason = "dry-run: narasi disiapkan, tidak dibacakan"
        return result

    chosen = engine or (available_engines() or [""])[0]
    if not chosen:
        result.reason = "tidak ada mesin TTS (espeak-ng/espeak/say) di sistem"
        return result
    result.engine = chosen

    command = [chosen, text] if chosen == "say" else [chosen, "-v", "id", text]
    try:
        subprocess.run(command, check=True, timeout=timeout,  # noqa: S603
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        result.spoken = True
        return result
    except (OSError, subprocess.SubprocessError) as exc:
        result.reason = f"{chosen} gagal: {exc}"
        return result
