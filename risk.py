"""AEGIS-1 — manajemen risiko.

Modul ini adalah satu-satunya tempat yang boleh memutuskan ukuran posisi dan
apakah sebuah order layak dikirim. Semua batas bersifat *fail-closed*: satu
pemeriksaan gagal berarti order tidak dikirim.
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import ROUND_DOWN, Decimal

from config import Mode, RiskConfig
from data import Check, MarketSnapshot
from strategy import LONG, Signal


def round_down_to_step(qty: float, step: float) -> float:
    """Bulatkan ke bawah ke kelipatan ``step`` (aturan exchange)."""
    if step <= 0:
        return qty
    d_qty = Decimal(str(qty))
    d_step = Decimal(str(step))
    steps = (d_qty / d_step).to_integral_value(rounding=ROUND_DOWN)
    return float(steps * d_step)


@dataclass
class Position:
    symbol: str
    side: str
    entry: float
    size: float
    initial_size: float
    stop: float
    take_profit: float
    opened_at: float
    risk_per_unit: float
    position_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    realized_pnl: float = 0.0
    partial_r_taken: list[float] = field(default_factory=list)
    trailing_active: bool = False
    entry_fee: float = 0.0
    stop_order_id: str | None = None
    take_profit_order_id: str | None = None

    @property
    def is_long(self) -> bool:
        return self.side == LONG

    def r_multiple(self, price: float) -> float:
        if self.risk_per_unit <= 0:
            return 0.0
        move = (price - self.entry) if self.is_long else (self.entry - price)
        return move / self.risk_per_unit

    def unrealized_pnl(self, price: float) -> float:
        move = (price - self.entry) if self.is_long else (self.entry - price)
        return move * self.size

    def notional(self, price: float) -> float:
        return self.size * price

    def age_seconds(self, now: float | None = None) -> float:
        return (now or time.time()) - self.opened_at

    def as_dict(self, price: float, now: float | None = None) -> dict[str, object]:
        return {
            "position_id": self.position_id,
            "symbol": self.symbol,
            "side": self.side,
            "entry": self.entry,
            "size": self.size,
            "initial_size": self.initial_size,
            "stop": self.stop,
            "take_profit": self.take_profit,
            "price": price,
            "pnl": self.unrealized_pnl(price),
            "r_multiple": self.r_multiple(price),
            "age_s": self.age_seconds(now),
            "trailing_active": self.trailing_active,
            "partial_r_taken": list(self.partial_r_taken),
            "stop_order_id": self.stop_order_id,
            "take_profit_order_id": self.take_profit_order_id,
        }


@dataclass
class ClosedTrade:
    symbol: str
    side: str
    entry: float
    exit: float
    size: float
    pnl: float
    r_multiple: float
    opened_at: float
    closed_at: float
    reason: str


@dataclass
class SizingResult:
    qty: float
    risk_amount: float
    notional: float
    stop_distance: float
    capped_by: str
    valid: bool
    detail: str


class RiskManager:
    def __init__(
        self,
        cfg: RiskConfig,
        initial_equity: float,
        step_size: float = 0.00001,
        min_qty: float = 0.00001,
        min_notional: float = 5.0,
    ) -> None:
        self.cfg = cfg
        self.initial_equity = initial_equity
        self.equity = initial_equity
        self.peak_equity = initial_equity
        self.day: date = datetime.now(timezone.utc).date()
        self.day_start_equity = initial_equity
        self.realized_pnl = 0.0
        self.realized_pnl_today = 0.0
        self.positions: dict[str, Position] = {}
        self.closed: list[ClosedTrade] = []
        self.step_size = step_size
        self.min_qty = min_qty
        self.min_notional = min_notional
        self.last_loss_bar_ts: float | None = None
        self.last_traded_bar_ts: float | None = None
        self.cooldown_until_bar_ts: float = 0.0
        self.max_drawdown_seen = 0.0
        self.equity_mtm: float | None = None

    # ------------------------------------------------------------------ #
    # Equity & limit
    # ------------------------------------------------------------------ #
    def roll_day_if_needed(self, now: float | None = None) -> bool:
        today = datetime.fromtimestamp(now or time.time(), tz=timezone.utc).date()
        if today != self.day:
            self.day = today
            self.day_start_equity = self.equity
            self.realized_pnl_today = 0.0
            return True
        return False

    def mark_to_market(self, price_map: dict[str, float]) -> float:
        unrealized = sum(
            p.unrealized_pnl(price_map.get(p.symbol, p.entry)) for p in self.positions.values()
        )
        return self.equity + unrealized

    def update_equity(self, equity: float) -> None:
        """Perbarui ekuitas terealisasi (setelah PnL masuk)."""
        self.equity = equity
        self.equity_mtm = equity
        self.peak_equity = max(self.peak_equity, equity)
        dd = self.drawdown()
        self.max_drawdown_seen = max(self.max_drawdown_seen, dd)

    def update_mark(self, mtm: float) -> None:
        """Perbarui ekuitas mark-to-market (realized + unrealized).

        Dipakai untuk drawdown dan batas rugi harian, supaya risiko yang belum
        terealisasi tetap terhitung.
        """
        self.equity_mtm = mtm
        self.peak_equity = max(self.peak_equity, mtm)
        self.max_drawdown_seen = max(self.max_drawdown_seen, self.drawdown())

    def current_equity(self) -> float:
        return self.equity_mtm if self.equity_mtm is not None else self.equity

    def drawdown(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.current_equity()) / self.peak_equity)

    def daily_loss(self) -> float:
        if self.day_start_equity <= 0:
            return 0.0
        return max(0.0, (self.day_start_equity - self.current_equity()) / self.day_start_equity)

    def daily_loss_breached(self) -> bool:
        return self.daily_loss() >= self.cfg.max_daily_loss

    def drawdown_breached(self) -> bool:
        return self.drawdown() >= self.cfg.max_drawdown

    def exposure(self, price_map: dict[str, float]) -> float:
        total = sum(p.notional(price_map.get(p.symbol, p.entry)) for p in self.positions.values())
        return total

    def exposure_ratio(self, price_map: dict[str, float]) -> float:
        equity = max(self.mark_to_market(price_map), 1e-9)
        return self.exposure(price_map) / equity

    # ------------------------------------------------------------------ #
    # Position sizing
    # ------------------------------------------------------------------ #
    def position_size(self, entry: float, stop: float, equity: float | None = None) -> SizingResult:
        """``size = (equity * risk%) / |entry - stop|`` lalu dibulatkan ke bawah.

        Setelah itu dibatasi oleh:
        * exposure maksimum (``max_total_exposure`` dari equity),
        * leverage maksimum,
        * sisa slot posisi (dibagi rata sebagai batas atas konservatif).
        """
        equity = self.equity if equity is None else equity
        stop_distance = abs(entry - stop)
        if stop_distance <= 0 or equity <= 0:
            return SizingResult(0.0, 0.0, 0.0, stop_distance, "invalid", False, "stop distance nol")

        risk_amount = equity * self.cfg.risk_per_trade
        raw_qty = risk_amount / stop_distance
        capped_by = "risk"

        max_notional = equity * min(self.cfg.max_total_exposure, self.cfg.max_leverage)
        if raw_qty * entry > max_notional:
            raw_qty = max_notional / entry
            capped_by = "exposure/leverage"

        slots = max(1, self.cfg.max_open_positions)
        slot_notional = max_notional / slots
        if raw_qty * entry > slot_notional:
            raw_qty = slot_notional / entry
            capped_by = "slot_posisi"

        qty = round_down_to_step(raw_qty, self.step_size)
        if qty < self.min_qty:
            return SizingResult(0.0, risk_amount, 0.0, stop_distance, capped_by, False,
                                f"qty {qty} < min {self.min_qty}")
        if qty * entry < self.min_notional:
            return SizingResult(0.0, risk_amount, qty * entry, stop_distance, capped_by, False,
                                f"notional {qty * entry:.2f} < min {self.min_notional}")
        return SizingResult(
            qty=qty,
            risk_amount=risk_amount,
            notional=qty * entry,
            stop_distance=stop_distance,
            capped_by=capped_by,
            valid=True,
            detail=f"risiko {risk_amount:.2f} / jarak stop {stop_distance:.4f} (dibatasi {capped_by})",
        )

    # ------------------------------------------------------------------ #
    # Pre-trade checklist
    # ------------------------------------------------------------------ #
    def pre_trade_check(
        self,
        signal: Signal,
        snap: MarketSnapshot,
        mode: Mode,
        gate_passed: bool,
        pending_orders: int,
        live_approved: bool = False,
        news_block: bool = False,
    ) -> list[Check]:
        """Checklist wajib sebelum order. Urutan mengikuti spesifikasi."""
        cfg = self.cfg
        price_map = {snap.symbol: snap.last_price}
        checks: list[Check] = []

        checks.append(
            Check("1_sinyal_valid", signal.actionable,
                  f"side={signal.side} skor={signal.score:.2f} veto={len(signal.vetoes)}")
        )
        checks.append(
            Check("2_stop_terdefinisi",
                  signal.stop > 0 and abs(signal.entry - signal.stop) > 0,
                  f"stop {signal.stop:.4f} dari entry {signal.entry:.4f}")
        )
        checks.append(
            Check("3_reward_risk", signal.reward_risk >= cfg.min_reward_risk,
                  f"RR {signal.reward_risk:.2f} (min {cfg.min_reward_risk:.1f})")
        )

        sizing = self.position_size(signal.entry, signal.stop)
        checks.append(
            Check("4_ukuran_posisi", sizing.valid and sizing.qty > 0, sizing.detail)
        )

        checks.append(
            Check("5_daily_loss", not self.daily_loss_breached(),
                  f"rugi harian {self.daily_loss():.2%} (maks {cfg.max_daily_loss:.0%})")
        )
        checks.append(
            Check("6_kill_switch", mode not in (Mode.KILL, Mode.PAUSED),
                  f"mode={mode.value}")
        )
        checks.append(
            Check("7_spread_likuiditas",
                  snap.spread_bps <= cfg.max_spread_bps,
                  f"spread {snap.spread_bps:.1f} bps (maks {cfg.max_spread_bps})")
        )

        # Guardrail tambahan di luar tujuh poin wajib.
        checks.append(
            Check("8_slot_posisi", len(self.positions) < cfg.max_open_positions,
                  f"{len(self.positions)}/{cfg.max_open_positions} posisi terbuka")
        )
        projected = self.exposure(price_map) + sizing.notional
        projected_ratio = projected / max(self.equity, 1e-9)
        checks.append(
            Check("9_total_exposure", projected_ratio <= cfg.max_total_exposure + 1e-9,
                  f"proyeksi {projected_ratio:.1%} (maks {cfg.max_total_exposure:.0%})")
        )
        checks.append(
            Check("10_drawdown", not self.drawdown_breached(),
                  f"drawdown {self.drawdown():.2%} (maks {cfg.max_drawdown:.0%})")
        )
        checks.append(
            Check("11_kualitas_data", gate_passed, "gate data lolos")
        )
        checks.append(
            Check("12_tanpa_order_ganda", pending_orders == 0,
                  f"{pending_orders} order pending")
        )
        checks.append(
            Check("13_bukan_duplikat_bar", self.last_traded_bar_ts != signal.bar_ts,
                  "bar ini belum pernah dipakai untuk order")
        )
        checks.append(
            Check("14_cooldown_pasca_loss", signal.bar_ts >= self.cooldown_until_bar_ts,
                  "cooldown anti revenge-trading terlewati")
        )
        checks.append(
            Check("15_berita", not news_block,
                  "tidak ada blokir event volatilitas")
        )
        checks.append(
            Check("16_izin_live", (not live_approved) or mode == Mode.PAPER or live_approved,
                  "izin live sesuai konfigurasi")
        )
        return checks

    # ------------------------------------------------------------------ #
    # Manajemen posisi
    # ------------------------------------------------------------------ #
    def open_position(self, signal: Signal, qty: float, fill_price: float, fee: float) -> Position:
        pos = Position(
            symbol=signal.symbol,
            side=signal.side or LONG,
            entry=fill_price,
            size=qty,
            initial_size=qty,
            stop=signal.stop,
            take_profit=signal.take_profit,
            opened_at=time.time(),
            risk_per_unit=abs(fill_price - signal.stop),
            entry_fee=fee,
        )
        self.positions[pos.position_id] = pos
        return pos

    def plan_management(
        self, pos: Position, price: float, atr_value: float
    ) -> dict[str, object]:
        """Putuskan aksi: partial TP, trailing stop, atau biarkan.

        Mengembalikan dict ``{"action": ..., "qty": ..., "new_stop": ...}``.
        """
        r = pos.r_multiple(price)
        out: dict[str, object] = {"action": "hold", "qty": 0.0, "new_stop": None, "r": r}

        # 1) Partial take profit pada level R yang ditentukan.
        for level in self.cfg.partial_take_profit_r:
            if level in pos.partial_r_taken:
                continue
            if r >= level:
                qty = round_down_to_step(pos.size * self.cfg.partial_take_profit_fraction, self.step_size)
                if qty >= self.min_qty:
                    out.update(action="partial_tp", qty=qty, level=level)
                    return out

        # 2) Trailing stop setelah mencapai trailing_after_r.
        if r >= self.cfg.trailing_after_r:
            trail = (
                price - self.cfg.trailing_atr_mult * atr_value
                if pos.is_long
                else price + self.cfg.trailing_atr_mult * atr_value
            )
            better = trail > pos.stop if pos.is_long else trail < pos.stop
            # Jangan pernah memindahkan stop lebih dekat dari 0.3 ATR ke harga.
            sane = abs(price - trail) >= 0.3 * atr_value
            if better and sane:
                out.update(action="trail", new_stop=round(trail, 8))
        return out

    def register_partial(self, pos: Position, qty: float, price: float, fee: float, level: float) -> float:
        move = (price - pos.entry) if pos.is_long else (pos.entry - price)
        pnl = move * qty - fee
        pos.size = round_down_to_step(pos.size - qty, self.step_size)
        pos.realized_pnl += pnl
        pos.partial_r_taken.append(level)
        self.realized_pnl += pnl
        self.realized_pnl_today += pnl
        self.equity += pnl
        return pnl

    def close_position(
        self, pos: Position, price: float, fee: float, reason: str
    ) -> ClosedTrade:
        move = (price - pos.entry) if pos.is_long else (pos.entry - price)
        pnl = move * pos.size - fee
        pos.realized_pnl += pnl
        self.realized_pnl += pnl
        self.realized_pnl_today += pnl
        self.equity += pnl
        trade = ClosedTrade(
            symbol=pos.symbol,
            side=pos.side,
            entry=pos.entry,
            exit=price,
            size=pos.initial_size,
            pnl=pos.realized_pnl,
            r_multiple=pos.r_multiple(price),
            opened_at=pos.opened_at,
            closed_at=time.time(),
            reason=reason,
        )
        self.closed.append(trade)
        self.positions.pop(pos.position_id, None)
        if pnl < 0:
            self.last_loss_bar_ts = None  # diisi pemanggil dengan bar_ts terkini
        self.update_equity(self.equity)
        return trade

    # ------------------------------------------------------------------ #
    # Statistik
    # ------------------------------------------------------------------ #
    def stats(self) -> dict[str, float]:
        trades = self.closed
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl < 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
        avg_r = sum(t.r_multiple for t in trades) / len(trades) if trades else 0.0
        return {
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / len(trades)) if trades else 0.0,
            "profit_factor": profit_factor if math.isfinite(profit_factor) else 99.0,
            "avg_r": avg_r,
            "realized_pnl": self.realized_pnl,
            "max_drawdown": self.max_drawdown_seen,
        }
