"""AEGIS-1 — JARVIS #07: News & Sentiment Intelligence Engine.

Klasifikasi sentimen di sini **berbasis leksikon**, bukan model bahasa. Itu
pilihan sadar: hasilnya deterministik, bisa diuji, tidak butuh API key, dan
setiap skor bisa ditelusuri ke kata yang memicunya.

Sumber berita disuntikkan sebagai ``provider`` — pemanggil yang mengembalikan
daftar item. ``RssProvider`` disediakan sebagai implementasi nyata (memakai
``urllib`` + parser XML bawaan), tetapi modul ini tidak pernah menarik jaringan
sendiri saat diuji: test memberi provider tiruan.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from urllib.request import Request, urlopen

# Leksikon kecil dan sengaja konservatif: kata yang benar-benar menandakan arah.
POSITIVE = {
    "surge", "rally", "soar", "gain", "gains", "bullish", "breakout", "approval",
    "approved", "adoption", "inflow", "record", "upgrade", "partnership", "buyback",
    "naik", "menguat", "lonjakan", "disetujui", "adopsi", "rekor", "positif",
}
NEGATIVE = {
    "crash", "plunge", "slump", "loss", "losses", "bearish", "breakdown", "hack",
    "hacked", "exploit", "lawsuit", "ban", "banned", "outflow", "downgrade",
    "liquidation", "fraud", "delay", "rejected",
    "turun", "melemah", "anjlok", "peretasan", "gugatan", "larangan", "negatif",
}
NEGATORS = {"not", "no", "never", "tanpa", "bukan", "tidak"}

_WORD = re.compile(r"[a-zA-Z\u00C0-\u024F]+")


@dataclass
class Sentiment:
    score: float = 0.0            # -1..1
    positive_hits: list[str] = field(default_factory=list)
    negative_hits: list[str] = field(default_factory=list)
    label: str = "netral"

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 4),
            "label": self.label,
            "positive_hits": self.positive_hits[:8],
            "negative_hits": self.negative_hits[:8],
        }


def score_text(text: str) -> Sentiment:
    """Skor sentimen -1..1 dari hitungan kata positif/negatif.

    Negasi sederhana: kata positif tepat setelah "tidak"/"not" dihitung negatif.
    """
    words = [w.lower() for w in _WORD.findall(text or "")]
    pos: list[str] = []
    neg: list[str] = []
    for index, word in enumerate(words):
        negated = index > 0 and words[index - 1] in NEGATORS
        if word in POSITIVE:
            (neg if negated else pos).append(word)
        elif word in NEGATIVE:
            (pos if negated else neg).append(word)
    total = len(pos) + len(neg)
    score = 0.0 if total == 0 else (len(pos) - len(neg)) / total
    label = "positif" if score > 0.2 else "negatif" if score < -0.2 else "netral"
    return Sentiment(score=score, positive_hits=pos, negative_hits=neg, label=label)


@dataclass
class NewsItem:
    title: str
    ts: float = field(default_factory=time.time)
    source: str = "—"
    url: str = ""
    summary: str = ""

    def as_dict(self) -> dict[str, Any]:
        sentiment = score_text(f"{self.title} {self.summary}")
        return {
            "title": self.title,
            "ts": self.ts,
            "source": self.source,
            "url": self.url,
            "sentiment": sentiment.as_dict(),
        }


@dataclass
class NewsBriefing:
    items: int = 0
    score: float = 0.0            # rata-rata berbobot kesegaran
    label: str = "netral"
    positive: int = 0
    negative: int = 0
    neutral: int = 0
    top: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": self.items,
            "score": round(self.score, 4),
            "label": self.label,
            "positive": self.positive,
            "negative": self.negative,
            "neutral": self.neutral,
            "top": self.top[:5],
            "errors": self.errors[:3],
        }


class NewsEngine:
    """Ambil item dari provider, skor, lalu ringkas jadi satu briefing."""

    def __init__(self, provider: Callable[[], Iterable[Any]] | None = None,
                 half_life_hours: float = 12.0) -> None:
        self.provider = provider
        self.half_life_hours = max(0.5, half_life_hours)

    def _normalize(self, raw: Any) -> NewsItem | None:
        if isinstance(raw, NewsItem):
            return raw
        if isinstance(raw, dict):
            title = str(raw.get("title") or "").strip()
            if not title:
                return None
            return NewsItem(
                title=title,
                ts=float(raw.get("ts") or time.time()),
                source=str(raw.get("source") or "—"),
                url=str(raw.get("url") or ""),
                summary=str(raw.get("summary") or ""),
            )
        return None

    def briefing(self, now: float | None = None) -> NewsBriefing:
        now = time.time() if now is None else now
        out = NewsBriefing()
        if self.provider is None:
            out.errors.append("provider berita tidak disetel")
            return out
        try:
            raw_items = list(self.provider())
        except Exception as exc:  # noqa: BLE001
            out.errors.append(f"provider gagal: {exc}")
            return out

        weighted = 0.0
        weight_sum = 0.0
        for raw in raw_items:
            item = self._normalize(raw)
            if item is None:
                continue
            sentiment = score_text(f"{item.title} {item.summary}")
            age_h = max(0.0, (now - item.ts) / 3600.0)
            weight = 0.5 ** (age_h / self.half_life_hours)
            weighted += sentiment.score * weight
            weight_sum += weight
            out.items += 1
            if sentiment.label == "positif":
                out.positive += 1
            elif sentiment.label == "negatif":
                out.negative += 1
            else:
                out.neutral += 1
            out.top.append({**item.as_dict(), "weight": round(weight, 3)})

        out.score = (weighted / weight_sum) if weight_sum > 0 else 0.0
        out.label = "positif" if out.score > 0.2 else "negatif" if out.score < -0.2 else "netral"
        out.top.sort(key=lambda x: x["weight"], reverse=True)
        return out


class RssProvider:
    """Provider RSS sederhana tanpa dependensi eksternal.

    Dipakai di mesin pengguna, bukan di test. Kegagalan jaringan dilaporkan
    sebagai daftar kosong + pesan, tidak pernah membuat bot berhenti.
    """

    def __init__(self, feeds: Iterable[str], timeout: float = 8.0, limit: int = 25) -> None:
        self.feeds = [f for f in feeds if f]
        self.timeout = timeout
        self.limit = limit
        self.errors: list[str] = []

    def _parse(self, payload: bytes, source: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            self.errors.append(f"{source}: XML tidak valid ({exc})")
            return items
        for node in root.iter():
            if not node.tag.lower().endswith("item") and not node.tag.lower().endswith("entry"):
                continue
            title = ""
            link = ""
            summary = ""
            for child in node:
                tag = child.tag.lower().split("}")[-1]
                if tag == "title":
                    title = (child.text or "").strip()
                elif tag in ("link", "id"):
                    link = link or (child.text or "").strip()
                    if not link:
                        link = child.attrib.get("href", "")
                elif tag in ("description", "summary", "content"):
                    summary = re.sub(r"<[^>]+>", " ", child.text or "").strip()
            if title:
                items.append({"title": title, "summary": summary, "url": link,
                              "source": source, "ts": time.time()})
        return items

    def __call__(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for feed in self.feeds:
            try:
                request = Request(feed, headers={"User-Agent": "AEGIS-1/1.0"})
                with urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                    payload = response.read()
                out.extend(self._parse(payload, feed))
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"{feed}: {exc}")
        out.sort(key=lambda item: item.get("ts", 0), reverse=True)
        return out[: self.limit]
