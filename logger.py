"""AEGIS-1 — audit log.

Dua lapis:

* SQLite (``data/aegis1.sqlite3``) untuk query dashboard dan rekonstruksi.
* JSON Lines (``data/aegis1.jsonl``) sebagai jejak append-only yang mudah
  dikirim ke sistem log lain.

Setiap keputusan — sinyal, veto, order, fill, perubahan ekuitas, aksi operator —
masuk ke sini. Tidak ada jalur keputusan yang tidak tercatat.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, symbol TEXT, bar_ts REAL, side TEXT, score REAL,
    entry REAL, stop REAL, take_profit REAL, reward_risk REAL, regime TEXT,
    reasons TEXT, vetoes TEXT, action TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    client_id TEXT PRIMARY KEY, ts REAL NOT NULL, exchange_id TEXT,
    symbol TEXT, side TEXT, type TEXT, qty REAL, price REAL, stop_price REAL,
    status TEXT, filled REAL, avg_price REAL, fee REAL, slippage_bps REAL,
    signal_id TEXT, meta TEXT, updated_at REAL
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
    client_id TEXT, symbol TEXT, qty REAL, price REAL, fee REAL
);
CREATE TABLE IF NOT EXISTS positions (
    position_id TEXT PRIMARY KEY, symbol TEXT, side TEXT, entry REAL, size REAL,
    stop REAL, take_profit REAL, opened_at REAL, closed_at REAL, exit REAL,
    pnl REAL, r_multiple REAL, close_reason TEXT, status TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, equity REAL,
    balance REAL, unrealized REAL, realized REAL, drawdown REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, level TEXT,
    kind TEXT, message TEXT, payload TEXT
);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, action TEXT,
    actor TEXT, status TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders(ts DESC);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity(ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);
"""


class AuditLog:
    """SQLite + JSONL. Aman dipakai dari thread dashboard maupun loop engine."""

    def __init__(self, db_path: str | Path, jsonl_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = Path(jsonl_path) if jsonl_path else None
        if self.jsonl_path:
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #
    def _mirror(self, record: dict[str, Any]) -> None:
        if not self.jsonl_path:
            return
        try:
            with self.jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass  # jejak JSONL tidak boleh menjatuhkan bot

    def _exec(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------ #
    # Tulis
    # ------------------------------------------------------------------ #
    def log_event(
        self, level: str, kind: str, message: str, payload: dict[str, Any] | None = None
    ) -> None:
        ts = time.time()
        self._exec(
            "INSERT INTO events (ts, level, kind, message, payload) VALUES (?,?,?,?,?)",
            (ts, level, kind, message, json.dumps(payload or {}, default=str)),
        )
        self._mirror({"ts": ts, "type": "event", "level": level, "kind": kind,
                      "message": message, "payload": payload or {}})

    def log_signal(self, signal: dict[str, Any], action: str) -> None:
        self._exec(
            """INSERT INTO signals
               (ts, symbol, bar_ts, side, score, entry, stop, take_profit,
                reward_risk, regime, reasons, vetoes, action)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                time.time(), signal.get("symbol"), signal.get("bar_ts"),
                signal.get("side"), signal.get("score"), signal.get("entry"),
                signal.get("stop"), signal.get("take_profit"),
                signal.get("reward_risk"), signal.get("regime"),
                json.dumps(signal.get("reasons", [])),
                json.dumps(signal.get("vetoes", [])),
                action,
            ),
        )
        self._mirror({"ts": time.time(), "type": "signal", "action": action, **signal})

    def log_order(self, order: dict[str, Any]) -> None:
        self._exec(
            """INSERT OR REPLACE INTO orders
               (client_id, ts, exchange_id, symbol, side, type, qty, price,
                stop_price, status, filled, avg_price, fee, slippage_bps,
                signal_id, meta, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                order["client_id"], order.get("ts", time.time()),
                order.get("exchange_id"), order.get("symbol"), order.get("side"),
                order.get("type"), order.get("qty"), order.get("price"),
                order.get("stop_price"), order.get("status"), order.get("filled", 0.0),
                order.get("avg_price"), order.get("fee", 0.0),
                order.get("slippage_bps", 0.0), order.get("signal_id"),
                json.dumps(order.get("meta", {}), default=str), time.time(),
            ),
        )
        self._mirror({"ts": time.time(), "type": "order", **order})

    def log_fill(self, client_id: str, symbol: str, qty: float, price: float, fee: float) -> None:
        self._exec(
            "INSERT INTO fills (ts, client_id, symbol, qty, price, fee) VALUES (?,?,?,?,?,?)",
            (time.time(), client_id, symbol, qty, price, fee),
        )
        self._mirror({"ts": time.time(), "type": "fill", "client_id": client_id,
                      "symbol": symbol, "qty": qty, "price": price, "fee": fee})

    def log_equity(
        self, equity: float, balance: float, unrealized: float, realized: float, drawdown: float
    ) -> None:
        self._exec(
            """INSERT INTO equity (ts, equity, balance, unrealized, realized, drawdown)
               VALUES (?,?,?,?,?,?)""",
            (time.time(), equity, balance, unrealized, realized, drawdown),
        )

    def log_position_open(self, pos: dict[str, Any]) -> None:
        self._exec(
            """INSERT OR REPLACE INTO positions
               (position_id, symbol, side, entry, size, stop, take_profit,
                opened_at, status)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (pos["position_id"], pos["symbol"], pos["side"], pos["entry"],
             pos["size"], pos["stop"], pos["take_profit"], pos["opened_at"], "open"),
        )

    def log_position_close(
        self, position_id: str, exit_price: float, pnl: float, r_multiple: float, reason: str
    ) -> None:
        self._exec(
            """UPDATE positions SET closed_at=?, exit=?, pnl=?, r_multiple=?,
               close_reason=?, status='closed' WHERE position_id=?""",
            (time.time(), exit_price, pnl, r_multiple, reason, position_id),
        )

    def log_action(self, action: str, actor: str, status: str, detail: str = "") -> None:
        self._exec(
            "INSERT INTO actions (ts, action, actor, status, detail) VALUES (?,?,?,?,?)",
            (time.time(), action, actor, status, detail),
        )
        self._mirror({"ts": time.time(), "type": "action", "action": action,
                      "actor": actor, "status": status, "detail": detail})

    # ------------------------------------------------------------------ #
    # Baca (dashboard)
    # ------------------------------------------------------------------ #
    def recent_orders(self, limit: int = 40) -> list[dict[str, Any]]:
        rows = self._query("SELECT * FROM orders ORDER BY ts DESC LIMIT ?", (limit,))
        for r in rows:
            try:
                r["meta"] = json.loads(r["meta"] or "{}")
            except json.JSONDecodeError:
                r["meta"] = {}
        return rows

    def recent_signals(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._query("SELECT * FROM signals ORDER BY ts DESC LIMIT ?", (limit,))
        for r in rows:
            for key in ("reasons", "vetoes"):
                try:
                    r[key] = json.loads(r[key] or "[]")
                except json.JSONDecodeError:
                    r[key] = []
        return rows

    def recent_events(self, limit: int = 60) -> list[dict[str, Any]]:
        return self._query("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,))

    def recent_trades(self, limit: int = 40) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM positions WHERE status='closed' ORDER BY closed_at DESC LIMIT ?",
            (limit,),
        )

    def equity_curve(self, limit: int = 400) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT ts, equity, drawdown FROM equity ORDER BY ts DESC LIMIT ?", (limit,)
        )
        return list(reversed(rows))

    def equity_stats(self) -> dict[str, float]:
        row = self._query(
            """SELECT COUNT(*) AS n, COALESCE(MAX(equity),0) AS peak,
                      COALESCE(MIN(equity),0) AS trough FROM equity"""
        )
        peak = row[0]["peak"] if row else 0.0
        trough = row[0]["trough"] if row else 0.0
        return {"peak_equity": peak, "trough_equity": trough}


class Alerts:
    """Kanal peringatan lokal.

    Saat ini peringatan ditulis ke audit log dan stdout. Ini titik sambung yang
    jelas bila nanti ingin menambah kanal eksternal: cukup berlangganan ke
    ``Alerts.subscribe`` dan teruskan payload-nya. Tidak ada kredensial kanal
    yang disimpan di paket ini.
    """

    def __init__(self, audit: AuditLog) -> None:
        self.audit = audit
        self._subscribers: list[Any] = []

    def subscribe(self, fn: Any) -> None:
        self._subscribers.append(fn)

    def send(self, kind: str, message: str, severity: str = "info", **payload: Any) -> None:
        self.audit.log_event(severity, f"alert:{kind}", message, payload)
        for fn in self._subscribers:
            try:
                fn(kind, message, severity, payload)
            except Exception:  # pragma: no cover - kanal tidak boleh menjatuhkan bot
                continue

    # Kanal bawaan: stdout ringkas.
    def to_stdout(self, kind: str, message: str, severity: str, payload: dict[str, Any]) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{stamp}] ALERT[{severity}] {kind}: {message}", flush=True)


def new_client_id(prefix: str = "aeg") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"
