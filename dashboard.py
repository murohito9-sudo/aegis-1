"""AEGIS-1 — backend dashboard.

FastAPI + WebSocket. Menyajikan satu file HTML dari ``static/index.html`` dan
tiga kelompok endpoint:

* ``GET  /api/state``   — snapshot state untuk dashboard,
* ``GET  /api/history`` — order, trade tertutup, sinyal,
* ``POST /api/actions/{action}/request`` + ``/confirm`` — aksi operator dua
  langkah dengan token sekali pakai.

Keamanan yang dipakai di sini:

* Setiap halaman mendapat token CSRF acak sekali pakai yang disuntikkan ke
  ``<meta name="aegis-csrf">``. Token tidak disimpan di localStorage atau
  sessionStorage, hanya hidup di memori halaman.
* Aksi berdampak uang wajib melewati dua langkah: minta token, lalu kirim token
  + frasa konfirmasi. Token kedaluwarsa dalam 120 detik dan hangus setelah
  dipakai.
* Dashboard tidak pernah mengirim kredensial exchange ke browser. Yang dikirim
  hanya angka dan status.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response

from security import RateLimiter

STATIC_DIR = Path(__file__).resolve().parent / "static"

CONFIRM_PHRASES = {
    "pause": "PAUSE AEGIS-1",
    "resume": "RESUME AEGIS-1",
    "close_all": "TUTUP SEMUA POSISI",
    "kill": "AKTIFKAN KILL SWITCH",
}

TOKEN_TTL_S = 120.0
CSRF_TTL_S = 12 * 3600.0


class Hub:
    """Broadcast state ke semua klien WebSocket."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def add(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.add(ws)

    async def remove(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def publish(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            clients = list(self._clients)
        dead: list[WebSocket] = []
        for ws in clients:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.remove(ws)


def _purge(store: dict[str, Any]) -> None:
    """Buang entri kedaluwarsa. Nilai boleh float (epoch) atau dict berisi 'exp'."""
    now = time.time()
    for key, val in list(store.items()):
        exp = val.get("exp", 0.0) if isinstance(val, dict) else float(val)
        if exp < now:
            store.pop(key, None)



LIVE_LOCK_REASON = (
    "Eksekusi LIVE dinonaktifkan pada V3 sampai otorisasi, pemisahan state, "
    "dashboard, dan rekonsiliasi lulus pengujian end-to-end."
)


def _live_status_snapshot(engine: Any) -> dict[str, Any]:
    """Status adapter yang aman untuk UI: tidak pernah menyertakan kredensial/error mentah.

    LIVE tetap dikunci di V3. Endpoint ini hanya observabilitas dan tidak
    menyediakan kemampuan membuat, membatalkan, atau mengubah order.
    """
    settings = getattr(engine, "settings", None)
    live_config = getattr(settings, "live", None)
    broker = getattr(engine, "broker", None)
    broker_name = str(getattr(broker, "name", "unavailable"))
    adapter_status = getattr(broker, "status", None)
    mode = getattr(engine, "mode", "UNKNOWN")
    mode = str(getattr(mode, "value", mode))

    symbols = getattr(live_config, "symbols", ()) or ()
    try:
        safe_symbols = [str(symbol) for symbol in symbols if str(symbol).strip()]
    except TypeError:
        safe_symbols = []

    raw_notional = getattr(live_config, "max_order_notional", None)
    try:
        max_order_notional = float(raw_notional) if raw_notional is not None else None
        if max_order_notional is not None and (
            max_order_notional != max_order_notional or abs(max_order_notional) == float("inf")
        ):
            max_order_notional = None
    except (TypeError, ValueError, OverflowError):
        max_order_notional = None

    def count(name: str) -> int:
        try:
            return max(0, int(getattr(adapter_status, name, 0) or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    adapter_present = broker_name.lower() == "live"
    adapter_connected = bool(getattr(adapter_status, "connected", False)) if adapter_present else False
    dry_run = bool(
        getattr(adapter_status, "dry_run", getattr(live_config, "dry_run", True))
    )

    return {
        "mode": mode,
        "exchange": str(getattr(settings, "exchange_id", "—")),
        "live_requested": bool(getattr(settings, "live_mode", False)),
        "adapter": broker_name,
        "adapter_present": adapter_present,
        "adapter_connected": adapter_connected,
        "dry_run": dry_run,
        "orders_sent": count("orders_sent"),
        "orders_rejected": count("orders_rejected"),
        "last_reconcile_ts": float(getattr(adapter_status, "last_reconcile_ts", 0.0) or 0.0),
        "reconciled_orders": count("reconciled_orders"),
        "allowlisted_symbols": safe_symbols,
        "market_orders_allowed": bool(getattr(live_config, "allow_market", False)),
        "max_order_notional": max_order_notional,
        "execution_state": "LOCKED",
        "execution_enabled": False,
        "read_only": True,
        "lock_reason": LIVE_LOCK_REASON,
    }

def create_app(engine: Any, hub: Hub) -> FastAPI:
    """Bangun aplikasi FastAPI. ``engine`` harus menyediakan:

    * ``async state()``            -> dict
    * ``history()``                -> dict
    * ``async execute_action(name)`` -> dict
    * atribut ``symbol``, ``timeframe``, ``settings``
    """

    app = FastAPI(title="AEGIS-1 Trading Console", docs_url=None, redoc_url=None)
    csrf_tokens: dict[str, float] = {}
    pending: dict[str, dict[str, Any]] = {}
    limiter = RateLimiter(capacity=240, window_s=60.0)

    def check_csrf(request: Request) -> None:
        token = request.headers.get("X-AEGIS-CSRF", "")
        _purge(csrf_tokens)
        if token not in csrf_tokens:
            raise HTTPException(status_code=403, detail="token sesi tidak valid atau kedaluwarsa")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        token = secrets.token_urlsafe(24)
        csrf_tokens[token] = time.time() + CSRF_TTL_S
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__AEGIS_CSRF__", token))

    @app.get("/live", response_class=HTMLResponse)
    async def live_console() -> HTMLResponse:
        token = secrets.token_urlsafe(24)
        csrf_tokens[token] = time.time() + CSRF_TTL_S
        html = (STATIC_DIR / "live.html").read_text(encoding="utf-8")
        return HTMLResponse(
            html.replace("__AEGIS_CSRF__", token),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/live/status")
    async def live_status(request: Request) -> JSONResponse:
        check_csrf(request)
        client = request.client.host if request.client else "?"
        if not limiter.allow(client):
            raise HTTPException(status_code=429, detail="terlalu banyak permintaan")
        return JSONResponse(_live_status_snapshot(engine))

    @app.get("/api/state")
    async def state(request: Request) -> JSONResponse:
        check_csrf(request)
        client = request.client.host if request.client else "?"
        if not limiter.allow(client):
            raise HTTPException(status_code=429, detail="terlalu banyak permintaan")
        return JSONResponse(await engine.state())

    @app.get("/api/history")
    async def history(request: Request) -> JSONResponse:
        check_csrf(request)
        return JSONResponse(engine.history())

    @app.get("/api/report")
    async def report(request: Request) -> JSONResponse:
        check_csrf(request)
        return JSONResponse(engine.report())

    @app.get("/api/report.md")
    async def report_md(request: Request) -> Response:
        check_csrf(request)
        return Response(
            engine.report_markdown(),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="aegis-laporan.md"'},
        )

    @app.get("/api/report.csv")
    async def report_csv(request: Request) -> Response:
        check_csrf(request)
        return Response(
            engine.report_csv(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="aegis-trades.csv"'},
        )

    @app.get("/api/briefing")
    async def briefing(request: Request) -> JSONResponse:
        check_csrf(request)
        return JSONResponse(engine.briefing())

    @app.get("/api/health")
    async def health(request: Request) -> JSONResponse:
        check_csrf(request)
        return JSONResponse(engine.health())

    @app.get("/api/portfolio")
    async def portfolio(request: Request) -> JSONResponse:
        check_csrf(request)
        return JSONResponse(engine.portfolio())

    @app.get("/api/research")
    async def research(request: Request) -> JSONResponse:
        check_csrf(request)
        return JSONResponse(engine.research())

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"ok": True, "ts": time.time()})

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket, csrf: str = "") -> None:
        _purge(csrf_tokens)
        if csrf not in csrf_tokens:
            await ws.close(code=4403)
            return
        await ws.accept()
        await hub.add(ws)
        try:
            await ws.send_json(await engine.state())
            while True:
                # Klien tidak perlu mengirim apa pun; baca untuk deteksi putus.
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            await hub.remove(ws)

    @app.post("/api/actions/{action}/request")
    async def action_request(action: str, request: Request) -> JSONResponse:
        check_csrf(request)
        if action not in CONFIRM_PHRASES:
            raise HTTPException(status_code=404, detail="aksi tidak dikenal")
        token = secrets.token_urlsafe(16)
        _purge(pending)
        pending[token] = {"action": action, "exp": time.time() + TOKEN_TTL_S}
        return JSONResponse(
            {
                "token": token,
                "action": action,
                "phrase": CONFIRM_PHRASES[action],
                "expires_in": TOKEN_TTL_S,
            }
        )

    @app.post("/api/actions/{action}/confirm")
    async def action_confirm(action: str, request: Request) -> JSONResponse:
        check_csrf(request)
        body = await request.json()
        token = str(body.get("token", ""))
        phrase = str(body.get("phrase", "")).strip()
        _purge(pending)
        record = pending.pop(token, None)
        if not record or record["action"] != action:
            raise HTTPException(status_code=400, detail="token konfirmasi tidak valid atau kedaluwarsa")
        expected = CONFIRM_PHRASES[action]
        if phrase.upper() != expected.upper():
            raise HTTPException(status_code=400, detail="frasa konfirmasi tidak cocok")
        try:
            result = await engine.execute_action(action)
        except RuntimeError as exc:  # guardrail menolak aksi (mis. resume saat kill)
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse({"ok": True, "action": action, "result": result})

    return app


def app_factory_docs() -> str:
    """Dokumentasi ringkas endpoint untuk README."""
    return json.dumps(
        {
            "GET /": "dashboard HTML",
            "GET /api/state": "snapshot state",
            "GET /api/history": "order, trade, sinyal",
            "GET /healthz": "liveness",
            "WS /ws?csrf=": "stream state",
            "POST /api/actions/{pause|resume|close_all|kill}/request": "langkah 1",
            "POST /api/actions/{action}/confirm": "langkah 2 + frasa",
        },
        indent=2,
    )
