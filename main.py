"""AEGIS-1 — loop utama.

Alur satu siklus (``Engine._tick``):

1. Proses fill order yang menggantung (termasuk stop-loss dan take-profit).
2. Jalankan gate kualitas data. Gagal = tidak ada entry, tanpa pengecualian.
3. Kelola posisi terbuka: partial take profit, trailing stop, jaring pengaman SL/TP.
4. Evaluasi sinyal pada bar yang sudah ditutup (tanpa look-ahead).
5. Pre-trade checklist 16 poin. Satu gagal = order tidak dikirim.
6. Hitung ukuran posisi, kirim limit order, lalu kirim SL dan TP ke broker.
7. Perbarui ekuitas, tulis audit log, siarkan state ke dashboard.

Semua langkah dibungkus penanganan error: error berulang menurunkan bot ke mode
SAFE (berhenti membuka entry baru) tanpa menutup posisi secara otomatis.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
import time
import traceback
from typing import Any

import pandas as pd

from config import (
    Mode,
    Settings,
    credential_presence,
    load_settings,
)
from dashboard import Hub, create_app
from data import DataQualityGate, Feed, MarketSnapshot, build_feed, timeframe_seconds
from execution import (
    Broker,
    LiveBrokerUnavailable,
    Order,
    OrderRejected,
    OrderSide,
    OrderType,
    PaperBroker,
    build_broker,
)
from analytics import build_report, to_csv, to_markdown
from brain import BriefingInput, compose as compose_briefing
from evaluation import EvaluationEngine
from monitor import check_health, write_heartbeat
from portfolio import Holding, Portfolio
from indicators import add_indicators
from logger import Alerts, AuditLog
from risk import RiskManager
from strategy import Signal, evaluate

MAX_CONSECUTIVE_ERRORS = 3
CHART_BARS = 180


class Engine:
    def __init__(self, settings: Settings, feed: Feed | None = None) -> None:
        self.settings = settings
        self.audit = AuditLog(settings.db_path, settings.jsonl_path)
        self.alerts = Alerts(self.audit)
        self.alerts.subscribe(self.alerts.to_stdout)
        self.risk = RiskManager(
            settings.risk,
            settings.initial_equity,
            step_size=settings.execution.step_size,
            min_qty=settings.execution.min_qty,
            min_notional=settings.execution.min_notional,
        )
        self.broker: Broker = build_broker(settings, self.audit)
        self.feed: Feed = feed or build_feed(settings)
        self.gate = DataQualityGate(settings)
        self.hub = Hub()

        self.mode: Mode = Mode.PAPER
        self.running = True
        self.started_at = time.time()
        self.tick_count = 0
        self.consecutive_errors = 0
        self.last_error: str | None = None
        self.last_tick_ts: float | None = None
        self.last_snapshot: MarketSnapshot | None = None
        self.last_gate_passed = False
        self.last_gate_summary = "belum dijalankan"
        self.last_bar_evaluated: float = 0.0
        self.last_signal: Signal | None = None
        self.last_risk_multiplier: float = 1.0
        self.last_risk_notes: list[str] = []
        self.heartbeat_path = settings.db_path.parent / "heartbeat.json"
        self.started_wall = time.time()
        self.markers: list[dict[str, Any]] = []
        self._uvicorn_task: asyncio.Task[Any] | None = None
        self._server: Any = None

        presence = credential_presence()
        self.audit.log_event(
            "info",
            "startup",
            f"AEGIS-1 mulai dalam mode {self.mode.value}",
            {
                "symbol": settings.symbol,
                "timeframe": settings.timeframe,
                "exchange": settings.exchange_id,
                "live_mode": settings.live_mode,
                "offline": settings.offline,
                "credential_presence": presence,  # hanya boolean, bukan nilai
            },
        )
        self.alerts.send(
            "startup",
            f"AEGIS-1 aktif · {settings.symbol} {settings.timeframe} · mode {self.mode.value}",
        )

    # ------------------------------------------------------------------ #
    # Mode
    # ------------------------------------------------------------------ #
    def set_mode(self, mode: Mode, reason: str) -> None:
        if mode == self.mode:
            return
        old = self.mode
        self.mode = mode
        self.audit.log_event("warning", "mode_change", f"{old.value} → {mode.value}: {reason}")
        self.alerts.send("mode", f"Mode {old.value} → {mode.value} ({reason})", "warning")

    # ------------------------------------------------------------------ #
    # Jam, replay, laporan
    # ------------------------------------------------------------------ #
    def _clock(self, snap: MarketSnapshot | None = None) -> float:
        """Waktu acuan: waktu bar pada mode replay, waktu nyata selebihnya."""
        if self.settings.replay and snap is not None:
            return float(snap.ts)
        return time.time()

    def _replay_progress(self) -> dict[str, Any] | None:
        progress = getattr(self.feed, "progress", None)
        return dict(progress) if isinstance(progress, dict) else None

    def report(self) -> dict[str, Any]:
        """Laporan kinerja: metrik, daftar trade, daftar order, breakdown."""
        curve = [row["equity"] for row in self.audit.equity_curve(600)]
        snap = self.last_snapshot
        meta = {
            "symbol": self.settings.symbol,
            "timeframe": self.settings.timeframe,
            "mode": self.mode.value,
            "sumber_data": snap.source if snap else "—",
            "quote": self.settings.quote_currency,
            "replay": "ya" if self.settings.replay else "tidak",
            "sesi": self.settings.session.describe(),
            "dibuat": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        }
        return build_report(
            self.risk.closed,
            self.audit.recent_orders(300),
            initial_equity=self.settings.initial_equity,
            equity_curve=curve or None,
            meta=meta,
        )

    def report_markdown(self) -> str:
        return to_markdown(self.report())

    def report_csv(self) -> str:
        return to_csv(self.report())

    # ------------------------------------------------------------------ #
    # Kesehatan, briefing, portofolio
    # ------------------------------------------------------------------ #
    def health(self) -> dict[str, Any]:
        report = check_health(
            started_at=self.started_at,
            last_tick_ts=self.last_tick_ts,
            tick_count=self.tick_count,
            consecutive_errors=self.consecutive_errors,
            last_error=self.last_error,
            db_path=self.settings.db_path,
            heartbeat_path=self.heartbeat_path,
            max_tick_age_s=max(120.0, self.settings.loop_interval_s * 6),
        )
        return report.as_dict()

    def _evaluation(self) -> EvaluationEngine:
        engine = EvaluationEngine()
        engine.load(
            {
                "ts": trade.closed_at,
                "score": 0.5 + min(0.5, abs(trade.r_multiple) / 4.0),
                "side": trade.side,
                "r_multiple": trade.r_multiple,
                "pnl": trade.pnl,
                "symbol": trade.symbol,
                "regime": "—",
            }
            for trade in self.risk.closed[-200:]
        )
        return engine

    def briefing(self) -> dict[str, Any]:
        """Ringkasan situasi + keputusan, disusun mesin aturan (bukan LLM)."""
        signal = self.last_signal
        snap = self.last_snapshot
        evaluation = self._evaluation().report().as_dict()
        payload = BriefingInput(
            symbol=self.settings.symbol,
            timeframe=self.settings.timeframe,
            price=float(snap.last_price) if snap else 0.0,
            regime=getattr(signal, "regime", "unknown"),
            score=float(getattr(signal, "score", 0.0)),
            side=getattr(signal, "side", None),
            vetoes=list(getattr(signal, "vetoes", [])),
            htf_trend=getattr(signal, "htf_trend", "flat"),
            divergence=getattr(signal, "divergence", None),
            pattern=getattr(signal, "pattern", None),
            squeeze=bool(getattr(signal, "squeeze", False)),
            atr_pct_rank=float(getattr(signal, "atr_pct_rank", 0.5)),
            spread_bps=float(snap.spread_bps) if snap else 0.0,
            drawdown=self.risk.drawdown(),
            daily_loss=self.risk.daily_loss(),
            exposure=self.risk.exposure_ratio({snap.symbol: snap.last_price}) if snap else 0.0,
            risk_multiplier=self.last_risk_multiplier,
            brier=evaluation["brier"],
            hit_rate=evaluation["hit_rate"],
            drift=evaluation["drift"].get("status", "stabil") if evaluation["drift"] else "stabil",
            concentration=self._portfolio().concentration(),
        )
        briefing = compose_briefing(payload)
        return {**briefing.as_dict(), "text": briefing.to_text()}

    def _portfolio(self) -> Portfolio:
        snap = self.last_snapshot
        price = float(snap.last_price) if snap else 0.0
        portfolio = Portfolio(quote=self.settings.quote_currency,
                              cash=max(0.0, self.broker.balance()))
        for pos in self.risk.positions.values():
            portfolio.holdings[pos.symbol] = Holding(
                symbol=pos.symbol,
                qty=pos.size if pos.is_long else 0.0,
                cost_basis=pos.entry,
                price=price or pos.entry,
            )
        return portfolio

    def portfolio(self) -> dict[str, Any]:
        return self._portfolio().as_dict()

    # ------------------------------------------------------------------ #
    # State untuk dashboard
    # ------------------------------------------------------------------ #
    async def state(self) -> dict[str, Any]:
        snap = self.last_snapshot
        price = snap.last_price if snap else 0.0
        price_map = {snap.symbol: price} if snap else {}
        unrealized = sum(p.unrealized_pnl(price) for p in self.risk.positions.values())
        equity = self.risk.current_equity()
        exposure = self.risk.exposure(price_map)

        chart = {"close": [], "ema_fast": [], "ema_slow": [], "ts": []}
        if snap is not None and len(snap.candles) > 10:
            ind = add_indicators(snap.candles, self.settings.strategy).tail(CHART_BARS)
            chart = {
                "open": [None if pd.isna(v) else round(float(v), 6) for v in ind["open"]],
                "high": [None if pd.isna(v) else round(float(v), 6) for v in ind["high"]],
                "low": [None if pd.isna(v) else round(float(v), 6) for v in ind["low"]],
                "close": [None if pd.isna(v) else round(float(v), 6) for v in ind["close"]],
                "ema_fast": [None if pd.isna(v) else round(float(v), 6) for v in ind["ema_fast"]],
                "ema_slow": [None if pd.isna(v) else round(float(v), 6) for v in ind["ema_slow"]],
                "ts": [float(v) for v in ind["ts"]],
            }

        return {
            "ts": time.time(),
            "uptime_s": time.time() - self.started_at,
            "symbol": self.settings.symbol,
            "timeframe": self.settings.timeframe,
            "exchange": self.settings.exchange_id if not self.settings.offline else "offline/synthetic",
            "data_source": snap.source if snap else "—",
            "connected": snap is not None and self.consecutive_errors == 0,
            "mode": self.mode.value,
            "live_mode": self.settings.live_mode,
            "equity": equity,
            "initial_equity": self.settings.initial_equity,
            "quote": self.settings.quote_currency,
            "balance": self.broker.balance(),
            "unrealized_pnl": unrealized,
            "realized_pnl": self.risk.realized_pnl,
            "realized_pnl_today": self.risk.realized_pnl_today,
            "daily_loss": self.risk.daily_loss(),
            "drawdown": self.risk.drawdown(),
            "exposure_ratio": (exposure / equity) if equity > 0 else 0.0,
            "pending_orders": len([o for o in self.broker.open_orders()
                                   if o.type in (OrderType.LIMIT, OrderType.MARKET)]),
            "positions": [p.as_dict(price) for p in self.risk.positions.values()],
            "stats": self.risk.stats(),
            "risk": {
                "risk_per_trade": self.settings.risk.risk_per_trade,
                "max_daily_loss": self.settings.risk.max_daily_loss,
                "max_total_exposure": self.settings.risk.max_total_exposure,
                "max_drawdown": self.settings.risk.max_drawdown,
                "max_open_positions": self.settings.risk.max_open_positions,
                "max_leverage": self.settings.risk.max_leverage,
            },
            "gate": {"passed": self.last_gate_passed, "summary": self.last_gate_summary},
            "session": {
                "jadwal": self.settings.session.describe(),
                "terbuka": self.settings.session.is_open(self._clock(snap)),
                "trade_hari_ini": self.risk.trades_today,
                "batas_harian": self.settings.session.max_trades_per_day,
            },
            "instrument": {
                "pip_size": self.settings.execution.pip_size,
                "tick_size": self.settings.execution.tick_size,
                "step_size": self.settings.execution.step_size,
                "min_notional": self.settings.execution.min_notional,
                "spread_bps": snap.spread_bps if snap else 0.0,
                "market": self.settings.market,
            },
            "replay": self._replay_progress(),
            "broker": {
                "name": self.broker.name,
                "live": (self.broker.status.as_dict()
                         if hasattr(self.broker, "status") else None),
            },
            "analysis": {
                "htf_trend": getattr(self.last_signal, "htf_trend", "flat"),
                "htf_strength": round(getattr(self.last_signal, "htf_strength", 0.0), 4),
                "divergence": getattr(self.last_signal, "divergence", None),
                "pattern": getattr(self.last_signal, "pattern", None),
                "squeeze": getattr(self.last_signal, "squeeze", False),
                "atr_pct_rank": round(getattr(self.last_signal, "atr_pct_rank", 0.5), 4),
            },
            "adaptive_risk": {
                "multiplier": round(self.last_risk_multiplier, 4),
                "notes": self.last_risk_notes,
                "expectancy": round(
                    self.risk.recent_expectancy(self.settings.risk.adaptive.expectancy_lookback), 4),
            },
            "chart": chart,
            "markers": self.markers[-40:],
            "events": self.audit.recent_events(45),
            "last_tick_ts": self.last_tick_ts,
            "last_error": self.last_error,
            "tick_count": self.tick_count,
        }

    def history(self) -> dict[str, Any]:
        return {
            "orders": self.audit.recent_orders(40),
            "trades": self.audit.recent_trades(40),
            "signals": self.audit.recent_signals(25),
        }

    # ------------------------------------------------------------------ #
    # Aksi operator
    # ------------------------------------------------------------------ #
    async def execute_action(self, action: str) -> dict[str, Any]:
        if action == "pause":
            self.set_mode(Mode.PAUSED, "dihentikan operator dari dashboard")
            self.audit.log_action("pause", "operator", "done")
            return {"mode": self.mode.value}

        if action == "resume":
            if self.mode == Mode.KILL:
                self.audit.log_action("resume", "operator", "refused", "kill-switch aktif")
                raise RuntimeError(
                    "KILL-SWITCH aktif. Hapus file kill-switch dan restart bot untuk melanjutkan."
                )
            self.set_mode(Mode.PAPER, "dilanjutkan operator dari dashboard")
            self.audit.log_action("resume", "operator", "done")
            return {"mode": self.mode.value}

        if action == "close_all":
            closed = self._close_all("close_all_operator")
            self.audit.log_action("close_all", "operator", "done", f"{len(closed)} posisi")
            return {"closed": closed}

        if action == "kill":
            self.set_mode(Mode.KILL, "EMERGENCY KILL-SWITCH ditekan operator")
            canceled = self._cancel_all_pending()
            closed = self._close_all("kill_switch")
            try:
                self.settings.kill_switch_file.parent.mkdir(parents=True, exist_ok=True)
                self.settings.kill_switch_file.write_text(
                    f"kill-switch aktif {time.strftime('%Y-%m-%d %H:%M:%S')}\n", encoding="utf-8"
                )
            except OSError:
                pass
            self.audit.log_action(
                "kill", "operator", "done", f"{len(canceled)} order dibatalkan, {len(closed)} posisi ditutup"
            )
            self.alerts.send(
                "kill_switch",
                f"KILL-SWITCH aktif: {len(canceled)} order dibatalkan, {len(closed)} posisi ditutup",
                "error",
            )
            return {"canceled_orders": len(canceled), "closed": closed, "mode": self.mode.value}

        raise ValueError(f"aksi tidak dikenal: {action}")

    def _cancel_all_pending(self) -> list[str]:
        canceled: list[str] = []
        for order in self.broker.open_orders():
            if self.broker.cancel(order.client_id):
                canceled.append(order.client_id)
        return canceled

    def _close_all(self, reason: str) -> list[dict[str, Any]]:
        snap = self.last_snapshot
        price = snap.last_price if snap else 0.0
        closed: list[dict[str, Any]] = []
        for pos in list(self.risk.positions.values()):
            # Batalkan SL/TP agar tidak ada order yatim.
            for oid in (pos.stop_order_id, pos.take_profit_order_id):
                if oid:
                    self.broker.cancel(oid)
            flatten = getattr(self.broker, "flatten", None)
            if callable(flatten):
                if not flatten(pos.symbol, pos.size, pos.is_long):
                    self.audit.log_event(
                        "error", "live_flatten",
                        f"{pos.symbol} gagal ditutup di exchange; posisi masih terbuka di sana",
                    )
                    continue
            fee = pos.size * price * self.settings.execution.taker_fee
            trade = self.risk.close_position(pos, price, fee, reason)
            self.audit.log_position_close(
                pos.position_id, price, trade.pnl, trade.r_multiple, reason,
                mae_r=trade.mae_r, mfe_r=trade.mfe_r, fees=trade.fees, entry_fee=trade.entry_fee,
            )
            self.markers.append({"ts": snap.candles["ts"].iloc[-1] if snap else time.time(),
                                 "price": price, "kind": "exit", "side": pos.side})
            closed.append(
                {"position_id": pos.position_id, "pnl": trade.pnl, "r": trade.r_multiple}
            )
            self.alerts.send(
                "exit",
                f"Posisi {pos.side} {pos.symbol} ditutup ({reason}) PnL {trade.pnl:+.2f}",
                "info",
            )
        self.risk.update_equity(self.risk.equity)
        return closed

    # ------------------------------------------------------------------ #
    # Siklus
    # ------------------------------------------------------------------ #
    async def _tick(self) -> None:
        snap = self.feed.snapshot(bars=max(self.settings.strategy.min_bars + 40, 400))
        self.risk.roll_day_if_needed(self._clock(snap))
        self.last_snapshot = snap
        self.last_tick_ts = time.time()
        self.tick_count += 1

        # 1. Fill order menggantung (entry, SL, TP, partial).
        self._process_fills(self.broker.on_price(snap), snap)

        # 2. Gate kualitas data.
        gate = self.gate.check(snap)
        self.last_gate_passed = gate.passed
        self.last_gate_summary = gate.summary()
        if not gate.passed:
            self.audit.log_event(
                "warning", "data_gate", f"gate gagal: {gate.summary()}",
                {"checks": [c.as_dict() for c in gate.checks]},
            )

        # 3. Kelola posisi (hanya bila ada harga).
        closed_bars = snap.candles.iloc[:-1].reset_index(drop=True)
        self._manage_positions(snap, closed_bars)

        # 4. Entry baru.
        if gate.passed:
            await self._maybe_enter(snap, closed_bars)

        # 5. Ekuitas & siaran.
        price_map = {snap.symbol: snap.last_price}
        unrealized = sum(p.unrealized_pnl(snap.last_price) for p in self.risk.positions.values())
        mtm = self.risk.equity + unrealized
        self.risk.update_mark(mtm)
        self.audit.log_equity(
            mtm, self.broker.balance(), unrealized, self.risk.realized_pnl, self.risk.drawdown()
        )

        # 6. Guardrail global pasca-tick.
        if self.risk.drawdown_breached() and self.mode == Mode.PAPER:
            self.set_mode(Mode.SAFE, f"drawdown {self.risk.drawdown():.2%} ≥ batas "
                                     f"{self.settings.risk.max_drawdown:.0%}; butuh konfirmasi manual")
            self.alerts.send("drawdown", "Mode SAFE aktif karena batas drawdown tercapai", "error")
        if self.risk.daily_loss_breached() and self.mode == Mode.PAPER:
            self.set_mode(Mode.SAFE, f"rugi harian {self.risk.daily_loss():.2%} ≥ batas "
                                     f"{self.settings.risk.max_daily_loss:.0%}")
            self.alerts.send("daily_loss", "Batas rugi harian tercapai; entry dihentikan", "warning")

        with contextlib.suppress(OSError):
            write_heartbeat(self.heartbeat_path, {"tick": self.tick_count, "mode": self.mode.value})

        await self.hub.publish(await self.state())

        # 7. Mode replay: majukan jendela data satu bar untuk siklus berikutnya.
        if self.settings.replay:
            advance = getattr(self.feed, "advance", None)
            if callable(advance):
                advance(self.settings.replay_bars)

    def _manage_positions(self, snap: MarketSnapshot, closed_bars: pd.DataFrame) -> None:
        if not self.risk.positions:
            return
        for pos in self.risk.positions.values():
            pos.update_excursion(snap.last_price)
        atr_value = 0.0
        if len(closed_bars) > self.settings.strategy.atr_period:
            ind = add_indicators(closed_bars, self.settings.strategy)
            last_atr = ind["atr"].iloc[-1]
            atr_value = float(last_atr) if not pd.isna(last_atr) else 0.0

        tf_seconds = timeframe_seconds(self.settings.timeframe)
        for pos in list(self.risk.positions.values()):
            price = snap.last_price
            plan = self.risk.plan_management(
                pos, price, atr_value or pos.risk_per_unit, tf_seconds=tf_seconds
            )

            if plan["action"] == "partial_tp":
                qty = float(plan["qty"])
                level = float(plan.get("level", 0.0))
                side = OrderSide.SELL if pos.is_long else OrderSide.BUY
                try:
                    order = self.broker.place(
                        Order(
                            symbol=pos.symbol, side=side, type=OrderType.MARKET, qty=qty,
                            signal_id=None,
                            meta={"kind": "partial", "position_id": pos.position_id, "level": level},
                        )
                    )
                    self.audit.log_event(
                        "info", "partial_tp",
                        f"partial take profit {qty:.6f} pada {level:.1f}R untuk {pos.symbol}",
                    )
                    self._process_fills(self.broker.on_price(snap), snap)
                except OrderRejected as exc:
                    self.audit.log_event("warning", "order_rejected", f"partial TP ditolak: {exc}")

            elif plan["action"] == "breakeven" and plan["new_stop"] is not None:
                entry_stop = float(plan["new_stop"])
                if pos.stop_order_id:
                    self.broker.modify(pos.stop_order_id, stop_price=entry_stop)
                pos.stop = entry_stop
                pos.breakeven_done = True
                self.audit.log_event(
                    "info", "breakeven",
                    f"stop {pos.symbol} dipindah ke harga entry {entry_stop:.4f} "
                    f"(R {plan['r']:.2f})",
                )

            elif plan["action"] == "time_stop":
                bars_held = float(plan.get("bars_held", 0.0))
                self.audit.log_event(
                    "info", "time_stop",
                    f"{pos.symbol} dilepas setelah {bars_held:.0f} bar tanpa kemajuan "
                    f"(R {plan['r']:.2f})",
                )
                self._force_close(pos, price, "time_stop")
                continue

            elif plan["action"] == "trail" and plan["new_stop"] is not None:
                new_stop = float(plan["new_stop"])
                if pos.stop_order_id:
                    self.broker.modify(pos.stop_order_id, stop_price=new_stop)
                pos.stop = new_stop
                pos.trailing_active = True
                self.audit.log_event(
                    "info", "trailing_stop",
                    f"stop {pos.symbol} dipindah ke {new_stop:.4f} "
                    f"(R {plan['r']:.2f})",
                )

            # Jaring pengaman: bila order SL/TP tidak ada atau tidak terisi
            # padahal harga sudah melewati level, tutup posisi secara eksplisit.
            hit_stop = price <= pos.stop if pos.is_long else price >= pos.stop
            hit_tp = price >= pos.take_profit if pos.is_long else price <= pos.take_profit
            if (hit_stop or hit_tp) and not self._has_triggered_order(pos):
                reason = "stop_loss_safety" if hit_stop else "take_profit_safety"
                self._force_close(pos, price, reason)

    def _has_triggered_order(self, pos: Any) -> bool:
        for oid in (pos.stop_order_id, pos.take_profit_order_id):
            if not oid:
                continue
            for order in self.broker.all_orders():
                if order.client_id == oid and order.is_open:
                    return True
        return False

    def _force_close(self, pos: Any, price: float, reason: str) -> None:
        fee = pos.size * price * self.settings.execution.taker_fee
        trade = self.risk.close_position(pos, price, fee, reason)
        self.audit.log_position_close(
            pos.position_id, price, trade.pnl, trade.r_multiple, reason,
            mae_r=trade.mae_r, mfe_r=trade.mfe_r, fees=trade.fees, entry_fee=trade.entry_fee,
        )
        self.markers.append({"ts": time.time(), "price": price, "kind": "exit", "side": pos.side})
        self.alerts.send(
            "exit", f"{pos.side} {pos.symbol} ditutup ({reason}) PnL {trade.pnl:+.2f}",
            "warning" if trade.pnl < 0 else "info",
        )
        if trade.pnl < 0:
            self._apply_loss_cooldown()

    def _apply_loss_cooldown(self) -> None:
        snap = self.last_snapshot
        if snap is None:
            return
        tf = timeframe_seconds(self.settings.timeframe)
        bar_ts = float(snap.candles["ts"].iloc[-1])
        self.risk.cooldown_until_bar_ts = bar_ts + self.settings.risk.cooldown_bars_after_loss * tf
        self.audit.log_event(
            "info", "cooldown",
            f"cooldown {self.settings.risk.cooldown_bars_after_loss} bar setelah loss",
        )

    # ------------------------------------------------------------------ #
    # Fill
    # ------------------------------------------------------------------ #
    def _process_fills(self, fills: list[Any], snap: MarketSnapshot) -> None:
        if not fills:
            return
        orders = {o.client_id: o for o in self.broker.all_orders()}
        for fill in fills:
            if isinstance(self.broker, PaperBroker):
                self.broker.apply_fill_to_balance(fill)
            order = orders.get(fill.client_id)
            meta = order.meta if order else {}
            kind = str(meta.get("kind", "entry"))

            if kind == "entry":
                self._on_entry_fill(fill, meta, snap)
            elif kind == "partial":
                self._on_partial_fill(fill, meta)
            elif kind in ("sl", "tp"):
                self._on_exit_fill(fill, meta, kind)

    def _on_entry_fill(self, fill: Any, meta: dict[str, Any], snap: MarketSnapshot) -> None:
        signal: Signal | None = meta.get("signal")  # type: ignore[assignment]
        if signal is None:
            return
        pos = self.risk.open_position(signal, fill.qty, fill.price, fill.fee)
        self.audit.log_position_open(
            {
                "position_id": pos.position_id, "symbol": pos.symbol, "side": pos.side,
                "entry": pos.entry, "size": pos.size, "stop": pos.stop,
                "take_profit": pos.take_profit, "opened_at": pos.opened_at,
            }
        )
        # Kirim SL dan TP ke broker SEGERA setelah entry — bukan hanya dipantau bot.
        exit_side = OrderSide.SELL if pos.is_long else OrderSide.BUY
        try:
            sl = self.broker.place(
                Order(
                    symbol=pos.symbol, side=exit_side, type=OrderType.STOP_MARKET,
                    qty=pos.size, stop_price=pos.stop,
                    meta={"kind": "sl", "position_id": pos.position_id},
                )
            )
            pos.stop_order_id = sl.client_id
            tp = self.broker.place(
                Order(
                    symbol=pos.symbol, side=exit_side, type=OrderType.TAKE_PROFIT_MARKET,
                    qty=pos.size, stop_price=pos.take_profit,
                    meta={"kind": "tp", "position_id": pos.position_id},
                )
            )
            pos.take_profit_order_id = tp.client_id
        except OrderRejected as exc:
            self.audit.log_event("error", "sl_tp_rejected", f"gagal memasang SL/TP: {exc}")
            self.alerts.send("risk", f"SL/TP gagal dipasang: {exc}", "error")
            self._force_close(pos, fill.price, "sl_tp_gagal")

        self.markers.append({"ts": float(snap.candles["ts"].iloc[-1]), "price": fill.price,
                             "kind": "entry", "side": pos.side})
        self.alerts.send(
            "entry",
            f"{pos.side} {pos.symbol} {pos.size:.6f} @ {fill.price:.2f} "
            f"SL {pos.stop:.2f} TP {pos.take_profit:.2f}",
            "info",
        )

    def _on_partial_fill(self, fill: Any, meta: dict[str, Any]) -> None:
        pos = self.risk.positions.get(str(meta.get("position_id")))
        if not pos:
            return
        level = float(meta.get("level", 0.0))
        pnl = self.risk.register_partial(pos, fill.qty, fill.price, fill.fee, level)
        # Sesuaikan qty SL/TP agar tidak melebihi sisa posisi.
        for oid in (pos.stop_order_id, pos.take_profit_order_id):
            if oid:
                self.broker.modify(oid, qty=pos.size)
        self.audit.log_event(
            "info", "partial_fill",
            f"partial {level:.1f}R terealisasi {pnl:+.2f}, sisa {pos.size:.6f}",
        )

    def _on_exit_fill(self, fill: Any, meta: dict[str, Any], kind: str) -> None:
        pos = self.risk.positions.get(str(meta.get("position_id")))
        if not pos:
            return
        other = pos.take_profit_order_id if kind == "sl" else pos.stop_order_id
        if other:
            self.broker.cancel(other)
        if kind == "tp":
            reason = "take_profit"
        elif pos.trailing_active:
            reason = "trailing_stop"
        else:
            reason = "stop_loss"
        trade = self.risk.close_position(pos, fill.price, fill.fee, reason)
        self.audit.log_position_close(pos.position_id, fill.price, trade.pnl, trade.r_multiple, reason)
        self.markers.append({"ts": time.time(), "price": fill.price, "kind": "exit", "side": pos.side})
        self.alerts.send(
            "exit", f"{pos.side} {pos.symbol} {reason} @ {fill.price:.2f} PnL {trade.pnl:+.2f} "
                    f"({trade.r_multiple:+.2f}R)",
            "info" if trade.pnl >= 0 else "warning",
        )
        if trade.pnl < 0:
            self._apply_loss_cooldown()

    # ------------------------------------------------------------------ #
    # Entry
    # ------------------------------------------------------------------ #
    async def _maybe_enter(self, snap: MarketSnapshot, closed_bars: pd.DataFrame) -> None:
        if len(closed_bars) < self.settings.strategy.min_bars:
            return
        bar_ts = float(closed_bars["ts"].iloc[-1])
        if bar_ts <= self.last_bar_evaluated:
            return  # bar ini sudah dinilai
        self.last_bar_evaluated = bar_ts

        if self.mode != Mode.PAPER:
            self.audit.log_event("info", "entry_skipped", f"mode {self.mode.value}: entry dihentikan")
            return
        if self.settings.kill_switch_file.exists():
            self.set_mode(Mode.KILL, "file kill-switch ditemukan")
            return

        clock = self._clock(snap)
        if not self.settings.session.is_open(clock):
            self.audit.log_event(
                "info", "session_closed",
                f"di luar jadwal sesi ({self.settings.session.describe()}); entry ditahan",
            )
            return
        cap = self.settings.session.max_trades_per_day
        if cap and self.risk.trades_today >= cap:
            self.audit.log_event(
                "warning", "daily_trade_cap",
                f"batas {cap} trade/hari tercapai ({self.risk.trades_today}); entry ditahan",
            )
            return

        if any(p.symbol == snap.symbol for p in self.risk.positions.values()):
            return  # satu posisi per simbol

        signal = evaluate(
            closed_bars, self.settings.strategy, self.settings.risk, snap.symbol,
            timeframe=self.settings.timeframe,
        )
        self.last_signal = signal
        self.audit.log_signal(signal.as_dict(), "EVALUATED")

        if not signal.actionable:
            self.audit.log_event(
                "info", "no_trade",
                f"tidak ada entry: {'; '.join(signal.vetoes) or 'skor belum cukup'}",
            )
            return

        pending_entries = [
            o for o in self.broker.open_orders() if o.type in (OrderType.LIMIT, OrderType.MARKET)
        ]
        risk_mult, risk_notes = self.risk.adaptive_multiplier(
            signal.score, signal.atr_pct_rank
        )
        self.last_risk_multiplier = risk_mult
        self.last_risk_notes = risk_notes
        checks = self.risk.pre_trade_check(
            signal=signal,
            snap=snap,
            mode=self.mode,
            gate_passed=self.last_gate_passed,
            pending_orders=len(pending_entries),
            live_approved=False,
            risk_multiplier=risk_mult,
        )
        failed = [c for c in checks if not c.ok]
        if failed:
            self.audit.log_signal(signal.as_dict(), "BLOCKED")
            self.audit.log_event(
                "warning", "pretrade_blocked",
                "order tidak dikirim: " + "; ".join(f"{c.name} ({c.detail})" for c in failed),
                {"checks": [c.as_dict() for c in checks]},
            )
            return

        sizing = self.risk.position_size(
            signal.entry, signal.stop, risk_multiplier=risk_mult
        )
        if not sizing.valid:
            self.audit.log_signal(signal.as_dict(), "BLOCKED")
            self.audit.log_event("warning", "sizing_invalid", sizing.detail)
            return

        # Ringkasan pra-order — selalu dicatat, bisa direkonstruksi.
        fee_est = sizing.notional * self.settings.execution.taker_fee * 2
        risk_money = sizing.qty * abs(signal.entry - signal.stop)
        self.audit.log_event(
            "info", "order_summary",
            f"{signal.side} {snap.symbol} @ {signal.entry:.2f} qty {sizing.qty:.6f} "
            f"SL {signal.stop:.2f} TP {signal.take_profit:.2f} "
            f"risiko {risk_money:.2f} {self.settings.quote_currency} fee est {fee_est:.2f} "
            f"pengali risiko ×{risk_mult:.2f} (HTF {signal.htf_trend})",
            {
                "pair": snap.symbol, "side": signal.side, "entry": signal.entry,
                "qty": sizing.qty, "stop": signal.stop, "take_profit": signal.take_profit,
                "risk_money": risk_money, "fee_estimate": fee_est,
                "reward_risk": signal.reward_risk, "score": signal.score,
                "risk_multiplier": round(risk_mult, 4), "risk_notes": risk_notes,
                "htf_trend": signal.htf_trend, "divergence": signal.divergence,
                "pattern": signal.pattern, "squeeze": signal.squeeze,
                "checks": [c.as_dict() for c in checks],
            },
        )

        side = OrderSide.BUY if signal.side == "LONG" else OrderSide.SELL
        price = snap.best_ask if side == OrderSide.BUY else snap.best_bid
        try:
            self.broker.place(
                Order(
                    symbol=snap.symbol,
                    side=side,
                    type=OrderType.LIMIT if self.settings.execution.prefer_limit_orders else OrderType.MARKET,
                    qty=sizing.qty,
                    price=price,
                    signal_id=signal.signal_id,
                    meta={"kind": "entry", "signal": signal},
                )
            )
        except OrderRejected as exc:
            self.audit.log_signal(signal.as_dict(), "REJECTED")
            self.audit.log_event("error", "order_rejected", f"order ditolak broker: {exc}")
            return

        self.risk.last_traded_bar_ts = bar_ts
        self.audit.log_signal(signal.as_dict(), "EXECUTED")
        # Isi segera pada tick ini juga supaya SL/TP terpasang tanpa jeda.
        self._process_fills(self.broker.on_price(snap), snap)

    # ------------------------------------------------------------------ #
    # Runner
    # ------------------------------------------------------------------ #
    async def _start_dashboard(self) -> None:
        if not self.settings.dashboard_enabled:
            return
        import uvicorn

        app = create_app(self, self.hub)
        config = uvicorn.Config(
            app,
            host=self.settings.dashboard_host,
            port=self.settings.dashboard_port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._uvicorn_task = asyncio.create_task(self._server.serve())
        self.audit.log_event(
            "info", "dashboard",
            f"dashboard di http://{self.settings.dashboard_host}:{self.settings.dashboard_port}",
        )

    async def run(self, max_ticks: int | None = None) -> None:
        loop = asyncio.get_running_loop()
        for sig_name in ("SIGINT", "SIGTERM"):
            with contextlib.suppress(NotImplementedError, AttributeError, RuntimeError):
                loop.add_signal_handler(
                    getattr(signal, sig_name), lambda: asyncio.create_task(self.shutdown(sig_name))
                )
        await self._start_dashboard()
        try:
            while self.running:
                try:
                    await self._tick()
                    self.consecutive_errors = 0
                except Exception as exc:  # noqa: BLE001 - loop tidak boleh mati
                    self.consecutive_errors += 1
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    self.audit.log_event(
                        "error", "tick_error", self.last_error,
                        {"traceback": traceback.format_exc()[-2000:]},
                    )
                    self.alerts.send("error", f"siklus gagal ({self.consecutive_errors}x): {exc}", "error")
                    if self.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        self.set_mode(Mode.SAFE, "error berulang; entry baru diblokir")
                if max_ticks is not None and self.tick_count >= max_ticks:
                    break
                await asyncio.sleep(self.settings.loop_interval_s)
        finally:
            await self.shutdown("selesai")

    async def shutdown(self, why: str) -> None:
        if not self.running:
            return
        self.running = False
        self.audit.log_event("info", "shutdown", f"berhenti: {why}")
        self.alerts.send("shutdown", f"AEGIS-1 berhenti ({why})")
        cancel_all = getattr(self.broker, "cancel_all", None)
        if callable(cancel_all):
            try:
                count = cancel_all()
                if count:
                    self.audit.log_event(
                        "warning", "shutdown_orders",
                        f"{count} order terbuka dibatalkan saat bot berhenti",
                    )
            except Exception as exc:  # noqa: BLE001
                self.audit.log_event("error", "shutdown_orders", f"cancel_all gagal: {exc}")
        if self._server is not None:
            self._server.should_exit = True
        if self._uvicorn_task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._uvicorn_task, timeout=5.0)


def _print_banner(settings: Settings) -> None:
    presence = credential_presence()
    print("=" * 68)
    print(" AEGIS-1 — Adaptive Execution & Guarded Investment System")
    print("=" * 68)
    print(f" pair       : {settings.symbol}  ({settings.timeframe})")
    print(f" exchange   : {settings.exchange_id}   market: {settings.market}")
    print(f" mode       : {'LIVE diminta' if settings.live_mode else 'PAPER (simulasi)'}")
    print(f" equity     : {settings.initial_equity:,.2f} {settings.quote_currency}")
    print(f" data       : {'offline/synthetic' if settings.offline else 'ccxt publik'}")
    print(f" dashboard  : http://{settings.dashboard_host}:{settings.dashboard_port}")
    print(f" kredensial : key={presence['EXCHANGE_API_KEY']} "
          f"secret={presence['EXCHANGE_API_SECRET']} "
          f"passphrase={presence['EXCHANGE_API_PASSPHRASE']}  (hanya status terisi)")
    print("=" * 68)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AEGIS-1 trading engine")
    parser.add_argument("--offline", action="store_true", help="pakai data sintetis (tanpa jaringan)")
    parser.add_argument("--csv", type=str, default=None, help="baca candle dari file CSV")
    parser.add_argument("--ticks", type=int, default=None, help="berhenti setelah N siklus")
    parser.add_argument("--interval", type=float, default=None, help="jeda antar siklus (detik)")
    parser.add_argument("--port", type=int, default=None, help="port dashboard")
    parser.add_argument("--no-dashboard", action="store_true", help="jalankan tanpa dashboard")
    parser.add_argument("--replay", action="store_true", help="replay historis bar demi bar dari CSV")
    parser.add_argument("--speed", type=int, default=None, help="bar per siklus pada mode replay")
    args = parser.parse_args(argv)

    settings = load_settings()
    if args.offline:
        settings = Settings(**{**settings.__dict__, "offline": True})
    if args.interval is not None:
        settings = Settings(**{**settings.__dict__, "loop_interval_s": args.interval})
    if args.port is not None:
        settings = Settings(**{**settings.__dict__, "dashboard_port": args.port})
    if args.no_dashboard:
        settings = Settings(**{**settings.__dict__, "dashboard_enabled": False})
    if args.replay:
        settings = Settings(**{**settings.__dict__, "replay": True})
    if args.speed is not None:
        settings = Settings(**{**settings.__dict__, "replay_bars": max(1, args.speed)})

    _print_banner(settings)
    try:
        feed = build_feed(settings, csv_path=args.csv)
        engine = Engine(settings, feed=feed)
    except LiveBrokerUnavailable as exc:
        print(f"\nDITOLAK: {exc}\n", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"\nGAGAL START: {type(exc).__name__}: {exc}\n", file=sys.stderr)
        return 1

    try:
        asyncio.run(engine.run(max_ticks=args.ticks))
    except KeyboardInterrupt:
        print("\nDihentikan pengguna.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
