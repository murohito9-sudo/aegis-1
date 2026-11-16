"""AEGIS-1 — JARVIS #20: Security & Reliability.

Empat hal yang bisa diperiksa tanpa asumsi dan tanpa layanan luar:

1. **Integritas audit log** — rantai hash SHA-256 antar entri. Mengubah satu
   baris di tengah membuat rantai putus, jadi jejak keputusan tidak bisa diedit
   diam-diam.
2. **Pembatas laju** — token bucket sederhana untuk endpoint dashboard, supaya
   satu klien tidak bisa membanjiri proses bot.
3. **Pemindai rahasia** — memeriksa berkas repo terhadap pola kunci API yang
   tidak boleh ikut ter-commit.
4. **Pemeriksa izin berkas** — memastikan basis data dan berkas rahasia tidak
   bisa dibaca semua pengguna.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


# --------------------------------------------------------------------------- #
# Rantai hash audit
# --------------------------------------------------------------------------- #
GENESIS = "0" * 64


def chain_hash(previous: str, payload: Any) -> str:
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(f"{previous}|{body}".encode("utf-8")).hexdigest()


@dataclass
class ChainResult:
    ok: bool = True
    entries: int = 0
    broken_at: int | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "entries": self.entries,
                "broken_at": self.broken_at, "reason": self.reason}


class AuditChain:
    """Rantai hash append-only untuk entri audit (dict apa pun)."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self.head: str = GENESIS

    def append(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = {"seq": len(self.entries) + 1, "ts": time.time(), "payload": payload,
                  "prev": self.head}
        record["hash"] = chain_hash(self.head, record["payload"])
        self.head = record["hash"]
        self.entries.append(record)
        return record

    def verify(self) -> ChainResult:
        head = GENESIS
        for index, record in enumerate(self.entries):
            expected = chain_hash(head, record.get("payload", {}))
            if record.get("prev") != head or record.get("hash") != expected:
                return ChainResult(ok=False, entries=len(self.entries), broken_at=index,
                                   reason="hash tidak cocok dengan isi entri")
            head = record["hash"]
        return ChainResult(ok=True, entries=len(self.entries))

    def load(self, rows: Iterable[dict[str, Any]]) -> ChainResult:
        self.entries = [dict(row) for row in rows]
        self.head = self.entries[-1]["hash"] if self.entries else GENESIS
        return self.verify()


# --------------------------------------------------------------------------- #
# Pembatas laju
# --------------------------------------------------------------------------- #
@dataclass
class RateLimiter:
    """Token bucket: ``capacity`` permintaan per ``window_s``."""

    capacity: int = 60
    window_s: float = 60.0
    _hits: dict[str, list[float]] = field(default_factory=dict)

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.time() if now is None else float(now)
        cutoff = now - self.window_s
        hits = [t for t in self._hits.get(key, []) if t > cutoff]
        if len(hits) >= self.capacity:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        return True

    def retry_after(self, key: str, now: float | None = None) -> float:
        now = time.time() if now is None else float(now)
        hits = [t for t in self._hits.get(key, []) if t > now - self.window_s]
        if len(hits) < self.capacity:
            return 0.0
        return max(0.0, self.window_s - (now - min(hits)))


# --------------------------------------------------------------------------- #
# Pemindai rahasia
# --------------------------------------------------------------------------- #
SECRET_PATTERNS = [
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"), "token bot telegram"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "kunci OpenAI"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "access key AWS"),
    (re.compile(r"(?i)(api[_-]?secret|api[_-]?key)\s*[=:]\s*['\"]?([A-Za-z0-9/+_-]{16,})"),
     "kredensial hardcoded"),
    (re.compile(r"-----BEGIN (RSA|OPENSSH|EC) PRIVATE KEY-----"), "private key"),
]
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".sqlite3", ".pyc"}


@dataclass
class SecretFinding:
    path: str
    line: int
    kind: str
    preview: str

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "line": self.line, "kind": self.kind,
                "preview": self.preview}


def scan_secrets(paths: Iterable[str | Path], max_bytes: int = 2_000_000) -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    for item in paths:
        path = Path(item)
        if not path.is_file() or path.suffix.lower() in SKIP_SUFFIX:
            continue
        try:
            if path.stat().st_size > max_bytes:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if len(line) > 400:
                continue
            for pattern, kind in SECRET_PATTERNS:
                if pattern.search(line):
                    masked = re.sub(r"[A-Za-z0-9]", "•", line.strip())[:60]
                    findings.append(SecretFinding(str(path), number, kind, masked))
                    break
    return findings


def scan_tree(root: str | Path, skip_dirs: Iterable[str] = (".git", "__pycache__", ".venv", "data")) -> list[SecretFinding]:
    root = Path(root)
    skip = set(skip_dirs)
    targets: list[Path] = []
    for path in root.rglob("*"):
        if any(part in skip for part in path.parts):
            continue
        if path.is_file():
            targets.append(path)
    return scan_secrets(targets)


# --------------------------------------------------------------------------- #
# Izin berkas
# --------------------------------------------------------------------------- #
def check_permissions(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    """Laporkan berkas yang bisa dibaca/ditulis pengguna lain."""
    issues: list[dict[str, Any]] = []
    for item in paths:
        path = Path(item)
        if not path.exists():
            continue
        mode = path.stat().st_mode
        world_readable = bool(mode & stat.S_IROTH)
        world_writable = bool(mode & stat.S_IWOTH)
        if world_readable or world_writable:
            issues.append({
                "path": str(path),
                "mode": oct(stat.S_IMODE(mode)),
                "world_readable": world_readable,
                "world_writable": world_writable,
            })
    return issues


def harden_permissions(paths: Iterable[str | Path]) -> list[str]:
    """Setel izin 0600 pada berkas sensitif. Kembalikan yang berhasil diubah."""
    changed: list[str] = []
    for item in paths:
        path = Path(item)
        if not path.exists():
            continue
        try:
            os.chmod(path, 0o600)
            changed.append(str(path))
        except OSError:
            continue
    return changed
