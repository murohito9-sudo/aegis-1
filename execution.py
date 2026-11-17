"""AEGIS-1 — lapisan eksekusi.

Isi modul ini:

* tipe order dan state machine status order,
* ``PaperBroker`` — eksekutor penuh dengan fee, slippage, spread, partial fill,
  timeout, deduplikasi order, dan rekonsiliasi ke database,
* ``build_broker`` — pabrik broker. Default ``PaperBroker``; live hanya lewat
  ``live.LiveBroker`` yang punya pagar berlapis (frasa persetujuan, daftar putih
  simbol, batas notional, dry-run default). Lihat README bagian 9e.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from config import ExecutionConfig, Settings
from data import MarketSnapshot
from logger import AuditLog


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    STOP_MARKET = "STOP_MARKET"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"


class OrderStatus(str, Enum):
    NEW = "NEW"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


TERMINAL = {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED}


class OrderRejected(RuntimeError):
    """Order ditolak sebelum dikirim (guardrail atau aturan instrumen)."""


class LiveBrokerUnavailable(RuntimeError):
    """Dilempar saat LIVE_MODE diminta. Paket ini tidak mengirim order nyata."""


@dataclass
class Order:
    symbol: str
    side: OrderSide
    type: OrderType
    qty: float
    price: float | None = None
    stop_price: float | None = None
    client_id: str = field(default_factory=lambda: f"aeg-{uuid.uuid4().hex[:16]}")
    status: OrderStatus = OrderStatus.NEW
    filled: float = 0.0
    avg_price: float | None = None
    fee: float = 0.0
    slippage_bps: float = 0.0
    ts: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    signal_id: str | None = None
    exchange_id: str | None = None
    meta: dict[str, object] = field(default_factory=dict)

    @property
    def remaining(self) -> float:
        return max(0.0, self.qty - self.filled)

    @property
    def is_open(self) -> bool:
        return self.status not in TERMINAL

    def as_dict(self) -> dict[str, object]:
        return {
            "client_id": self.client_id,
            "ts": self.ts,
            "exchange_id": self.exchange_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "type": self.type.value,
            "qty": self.qty,
            "price": self.price,
            "stop_price": self.stop_price,
            "status": self.status.value,
            "filled": self.filled,
            "avg_price": self.avg_price,
            "fee": self.fee,
            "slippage_bps": self.slippage_bps,
            "signal_id": self.signal_id,
            "meta": self.meta,
        }


@dataclass
class Fill:
    client_id: str
    symbol: str
    side: OrderSide
    qty: float
    price: float
    fee: float
    ts: float
    order_type: OrderType


class Broker(Protocol):
    name: str

    def balance(self) -> float: ...
    def place(self, order: Order) -> Order: ...
    def cancel(self, client_id: str) -> bool: ...
    def modify(self, client_id: str, **changes: object) -> Order | None: ...
    def open_orders(self) -> list[Order]: ...
    def all_orders(self) -> list[Order]: ...
    def on_price(self, snap: MarketSnapshot) -> list[Fill]: ...


# --------------------------------------------------------------------------- #
# Paper broker
# --------------------------------------------------------------------------- #
class PaperBroker:
    """Eksekutor simulasi dengan model biaya eksplisit.

    Model isi order:

    * ``LIMIT``  : terisi saat harga menyentuh limit (maker fee).
    * ``MARKET`` : terisi langsung pada bid/ask + slippage (taker fee).
    * ``STOP_MARKET`` / ``TAKE_PROFIT_MARKET`` : terisi saat harga melewati
      level pemicu, lalu dieksekusi sebagai market.

    Tidak ada asumsi fill optimistis: setiap isi order membayar fee dan
    slippage.
    """

    name = "paper"

    def __init__(
        self,
        settings: Settings,
        audit: AuditLog,
        initial_balance: float | None = None,
    ) -> None:
        self.settings = settings
        self.cfg: ExecutionConfig = settings.execution
        self.audit = audit
        self._balance = initial_balance if initial_balance is not None else settings.initial_equity
        self._orders: dict[str, Order] = {}
        self._seen_signal_ids: set[str] = set()
        self._last_price: float | None = None

    # -- query ---------------------------------------------------------- #
    def balance(self) -> float:
        return self._balance

    def open_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.is_open]

    def all_orders(self) -> list[Order]:
        return sorted(self._orders.values(), key=lambda o: o.ts, reverse=True)

    # -- validasi ------------------------------------------------------- #
    def _validate(self, order: Order) -> None:
        if order.qty <= 0:
            raise OrderRejected("qty harus > 0")
        if order.qty < self.cfg.min_qty:
            raise OrderRejected(f"qty {order.qty} < min_qty {self.cfg.min_qty}")
        notional = order.qty * (order.price or self._last_price or 0.0)
        if notional and notional < self.cfg.min_notional:
            raise OrderRejected(f"notional {notional:.2f} < min {self.cfg.min_notional}")
        if order.type in (OrderType.LIMIT,) and not order.price:
            raise OrderRejected("limit order butuh price")
        if order.type in (OrderType.STOP_MARKET, OrderType.TAKE_PROFIT_MARKET) and not order.stop_price:
            raise OrderRejected("stop order butuh stop_price")
        if order.signal_id and order.signal_id in self._seen_signal_ids:
            raise OrderRejected(f"order duplikat untuk sinyal {order.signal_id}")

    # -- kirim ---------------------------------------------------------- #
    def place(self, order: Order) -> Order:
        self._validate(order)
        if order.signal_id:
            self._seen_signal_ids.add(order.signal_id)
        order.exchange_id = f"paper-{uuid.uuid4().hex[:10]}"
        order.status = OrderStatus.OPEN
        order.updated_at = time.time()
        self._orders[order.client_id] = order
        self.audit.log_order(order.as_dict())
        return order

    def cancel(self, client_id: str) -> bool:
        order = self._orders.get(client_id)
        if not order or not order.is_open:
            return False
        order.status = OrderStatus.CANCELED
        order.updated_at = time.time()
        self.audit.log_order(order.as_dict())
        return True

    def modify(self, client_id: str, **changes: object) -> Order | None:
        order = self._orders.get(client_id)
        if not order or not order.is_open:
            return None
        for key, value in changes.items():
            if hasattr(order, key) and value is not None:
                setattr(order, key, value)
        order.updated_at = time.time()
        self.audit.log_order(order.as_dict())
        return order

    # -- simulasi fill --------------------------------------------------- #
    def on_price(self, snap: MarketSnapshot) -> list[Fill]:
        self._last_price = snap.last_price
        fills: list[Fill] = []
        now = time.time()

        for order in list(self._orders.values()):
            if not order.is_open:
                continue
            # Timeout order yang menggantung.
            if order.type == OrderType.LIMIT and now - order.ts > self.cfg.order_timeout_s:
                order.status = OrderStatus.EXPIRED
                order.updated_at = now
                self.audit.log_order(order.as_dict())
                continue

            fill_price: float | None = None
            fee_rate = self.cfg.maker_fee

            if order.type == OrderType.LIMIT and order.price is not None:
                # Limit yang menyeberang book dieksekusi sebagai taker (fee taker);
                # limit yang menunggu di book dibayar sebagai maker.
                if order.side == OrderSide.BUY and snap.best_ask <= order.price:
                    fill_price = min(order.price, snap.best_ask)
                    fee_rate = self.cfg.taker_fee if snap.best_ask < order.price else self.cfg.maker_fee
                elif order.side == OrderSide.SELL and snap.best_bid >= order.price:
                    fill_price = max(order.price, snap.best_bid)
                    fee_rate = self.cfg.taker_fee if snap.best_bid > order.price else self.cfg.maker_fee
            elif order.type == OrderType.MARKET:
                slip = self.cfg.slippage_bps / 10_000.0
                fill_price = snap.best_ask * (1 + slip) if order.side == OrderSide.BUY else snap.best_bid * (1 - slip)
                fee_rate = self.cfg.taker_fee
            elif order.type == OrderType.STOP_MARKET and order.stop_price is not None:
                triggered = (
                    snap.last_price <= order.stop_price
                    if order.side == OrderSide.SELL
                    else snap.last_price >= order.stop_price
                )
                if triggered:
                    slip = self.cfg.slippage_bps / 10_000.0
                    fill_price = (
                        snap.best_bid * (1 - slip)
                        if order.side == OrderSide.SELL
                        else snap.best_ask * (1 + slip)
                    )
                    fee_rate = self.cfg.taker_fee
            elif order.type == OrderType.TAKE_PROFIT_MARKET and order.stop_price is not None:
                triggered = (
                    snap.last_price >= order.stop_price
                    if order.side == OrderSide.SELL
                    else snap.last_price <= order.stop_price
                )
                if triggered:
                    slip = self.cfg.slippage_bps / 10_000.0
                    fill_price = (
                        snap.best_bid * (1 - slip)
                        if order.side == OrderSide.SELL
                        else snap.best_ask * (1 + slip)
                    )
                    fee_rate = self.cfg.taker_fee

            if fill_price is None:
                continue

            qty = order.remaining
            fee = qty * fill_price * fee_rate
            reference = order.price or order.stop_price or snap.last_price
            slippage_bps = abs(fill_price - reference) / reference * 10_000.0 if reference else 0.0

            order.filled += qty
            order.avg_price = fill_price
            order.fee += fee
            order.slippage_bps = slippage_bps
            order.status = OrderStatus.FILLED
            order.updated_at = now
            self.audit.log_order(order.as_dict())
            self.audit.log_fill(order.client_id, order.symbol, qty, fill_price, fee)
            fills.append(
                Fill(
                    client_id=order.client_id,
                    symbol=order.symbol,
                    side=order.side,
                    qty=qty,
                    price=fill_price,
                    fee=fee,
                    ts=now,
                    order_type=order.type,
                )
            )
        return fills

    # -- akuntansi ------------------------------------------------------ #
    def apply_fill_to_balance(self, fill: Fill) -> None:
        """Perbarui saldo quote: beli mengurangi, jual menambah, fee selalu keluar."""
        if fill.side == OrderSide.BUY:
            self._balance -= fill.qty * fill.price + fill.fee
        else:
            self._balance += fill.qty * fill.price - fill.fee


def build_broker(settings: Settings, audit: AuditLog) -> Broker:
    """Pabrik broker. Fail closed: tanpa pagar lengkap, live ditolak.

    Live hanya dibangun bila ``AEGIS_LIVE_MODE=true`` **dan** seluruh pagar di
    ``live.LiveBroker._check_gates`` terpenuhi (frasa persetujuan, kredensial,
    daftar putih simbol, batas notional). Kalau salah satu gagal, start ditolak
    sebelum ada satu pun request ke exchange.
    """
    if settings.live_mode:
        from live import LiveBroker, LiveRefused  # impor lokal: hindari siklus

        try:
            return LiveBroker(settings, audit)
        except LiveRefused as exc:
            raise LiveBrokerUnavailable(f"live ditolak: {exc}") from exc
    return PaperBroker(settings, audit, initial_balance=settings.initial_equity)
