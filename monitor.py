"""AEGIS-1 — JARVIS #15 & #21: Automation, Monitoring, dan Recovery.

Tiga hal kecil yang sering dilupakan dan justru menentukan umur bot:

* **Scheduler** — pekerjaan berkala (briefing, evaluasi, backup, health) tanpa
  dependensi cron. Idempoten: satu pekerjaan tidak jalan dua kali dalam satu
  jendela, dan kegagalan satu pekerjaan tidak menghentikan yang lain.
* **Health** — umur tick terakhir, error beruntun, ukuran basis data, ruang
  disk, dan heartbeat file. Dipakai dashboard dan watchdog.
* **Backup/restore** — salin basis data + JSONL ke folder bertanggal dengan
  retensi, dan pulihkan tanpa menimpa data yang lebih baru tanpa konfirmasi.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# --------------------------------------------------------------------------- #
# Scheduler
# --------------------------------------------------------------------------- #
@dataclass
class Job:
    name: str
    every_s: float
    fn: Callable[[], Any]
    last_run: float = 0.0
    runs: int = 0
    failures: int = 0
    last_error: str | None = None

    def due(self, now: float) -> bool:
        return self.last_run <= 0 or (now - self.last_run) >= self.every_s

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "every_s": self.every_s,
            "last_run": self.last_run,
            "runs": self.runs,
            "failures": self.failures,
            "last_error": self.last_error,
        }


class Scheduler:
    """Penjadwal sederhana dan dapat diuji: ``run_due(now)`` mengembalikan hasil."""

    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}

    def add(self, name: str, every_s: float, fn: Callable[[], Any]) -> Job:
        if every_s <= 0:
            raise ValueError("every_s harus > 0")
        job = Job(name=name, every_s=float(every_s), fn=fn)
        self.jobs[name] = job
        return job

    def run_due(self, now: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else float(now)
        results: list[dict[str, Any]] = []
        for job in self.jobs.values():
            if not job.due(now):
                continue
            job.last_run = now
            job.runs += 1
            try:
                value = job.fn()
                job.last_error = None
                results.append({"job": job.name, "ok": True, "result": value})
            except Exception as exc:  # noqa: BLE001 - satu job gagal, lainnya jalan
                job.failures += 1
                job.last_error = f"{type(exc).__name__}: {exc}"
                results.append({"job": job.name, "ok": False, "error": job.last_error})
        return results

    def status(self) -> list[dict[str, Any]]:
        return [job.as_dict() for job in self.jobs.values()]


# --------------------------------------------------------------------------- #
# Health & heartbeat
# --------------------------------------------------------------------------- #
@dataclass
class HealthReport:
    ok: bool = True
    uptime_s: float = 0.0
    tick_age_s: float | None = None
    tick_count: int = 0
    consecutive_errors: int = 0
    last_error: str | None = None
    db_size_mb: float = 0.0
    disk_free_mb: float = 0.0
    heartbeat_age_s: float | None = None
    issues: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "uptime_s": round(self.uptime_s, 1),
            "tick_age_s": None if self.tick_age_s is None else round(self.tick_age_s, 1),
            "tick_count": self.tick_count,
            "consecutive_errors": self.consecutive_errors,
            "last_error": self.last_error,
            "db_size_mb": round(self.db_size_mb, 2),
            "disk_free_mb": round(self.disk_free_mb, 1),
            "heartbeat_age_s": None if self.heartbeat_age_s is None else round(self.heartbeat_age_s, 1),
            "issues": self.issues,
        }


def check_health(
    started_at: float,
    last_tick_ts: float | None,
    tick_count: int,
    consecutive_errors: int,
    last_error: str | None,
    db_path: str | Path | None = None,
    heartbeat_path: str | Path | None = None,
    max_tick_age_s: float = 120.0,
    min_disk_mb: float = 200.0,
    now: float | None = None,
) -> HealthReport:
    now = time.time() if now is None else float(now)
    report = HealthReport(
        uptime_s=max(0.0, now - started_at),
        tick_count=tick_count,
        consecutive_errors=consecutive_errors,
        last_error=last_error,
    )
    if last_tick_ts is not None:
        report.tick_age_s = max(0.0, now - float(last_tick_ts))
        if report.tick_age_s > max_tick_age_s:
            report.issues.append(f"tick terakhir {report.tick_age_s:.0f}s lalu (> {max_tick_age_s:.0f}s)")

    if db_path:
        path = Path(db_path)
        if path.exists():
            report.db_size_mb = path.stat().st_size / (1024 * 1024)
    try:
        usage = shutil.disk_usage(Path(db_path).parent if db_path else Path.cwd())
        report.disk_free_mb = usage.free / (1024 * 1024)
        if report.disk_free_mb < min_disk_mb:
            report.issues.append(f"ruang disk {report.disk_free_mb:.0f} MB (< {min_disk_mb:.0f} MB)")
    except OSError as exc:
        report.issues.append(f"disk tidak terbaca: {exc}")

    if heartbeat_path:
        hb = Path(heartbeat_path)
        if hb.exists():
            report.heartbeat_age_s = max(0.0, now - hb.stat().st_mtime)
        else:
            report.issues.append("heartbeat belum ada")

    if consecutive_errors >= 3:
        report.issues.append(f"{consecutive_errors} error beruntun")
    report.ok = not report.issues
    return report


def write_heartbeat(path: str | Path, payload: dict[str, Any] | None = None) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = {"ts": time.time(), **(payload or {})}
    target.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return target


# --------------------------------------------------------------------------- #
# Backup & restore
# --------------------------------------------------------------------------- #
@dataclass
class BackupResult:
    ok: bool
    directory: str = ""
    files: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "directory": self.directory, "files": self.files,
                "removed": self.removed, "error": self.error}


def backup(
    files: list[str | Path],
    dest_dir: str | Path,
    keep: int = 7,
    stamp: str | None = None,
) -> BackupResult:
    """Salin berkas ke ``dest_dir/YYYYmmdd-HHMMSS/`` dan pangkas yang lama."""
    dest_root = Path(dest_dir)
    dest_root.mkdir(parents=True, exist_ok=True)
    label = stamp or time.strftime("%Y%m%d-%H%M%S")
    target = dest_root / label
    copied: list[str] = []
    try:
        target.mkdir(parents=True, exist_ok=True)
        for item in files:
            source = Path(item)
            if not source.exists() or not source.is_file():
                continue
            shutil.copy2(source, target / source.name)
            copied.append(source.name)
    except OSError as exc:
        return BackupResult(ok=False, directory=str(target), files=copied, error=str(exc))

    removed: list[str] = []
    if keep > 0:
        existing = sorted([p for p in dest_root.iterdir() if p.is_dir()], reverse=True)
        for old in existing[keep:]:
            shutil.rmtree(old, ignore_errors=True)
            removed.append(old.name)
    return BackupResult(ok=True, directory=str(target), files=copied, removed=removed)


def restore(backup_dir: str | Path, target_dir: str | Path, overwrite: bool = False) -> list[str]:
    """Pulihkan berkas dari folder backup. Tidak menimpa kecuali diminta."""
    source = Path(backup_dir)
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    restored: list[str] = []
    for item in source.iterdir():
        if not item.is_file():
            continue
        destination = target / item.name
        if destination.exists() and not overwrite:
            continue
        shutil.copy2(item, destination)
        restored.append(item.name)
    return restored
