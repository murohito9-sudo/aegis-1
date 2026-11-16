"""AEGIS-1 — adapter eksekusi live (Binance lewat ccxt). Default MATI.

Ini satu-satunya modul di paket yang bisa mengirim order nyata. Karena itu ia
dirancang dengan pagar berlapis, dan semuanya harus dibuka secara sadar:

1. ``AEGIS_LIVE_MODE=true`` — tanpa ini, ``build_broker()`` menolak start.
2. ``AEGIS_LIVE_ACK`` harus berisi frasa persis :data:`ACK_PHRASE`.
3. ``EXCHANGE_API_KEY`` dan ``EXCHANGE_API_SECRET`` harus ada di environment.
4. ``AEGIS_LIVE_SYMBOLS`` wajib diisi — tidak ada default "semua pair".
5. ``AEGIS_LIVE_DRY_RUN`` default ``true``: order divalidasi dan dicatat,
   tetapi **tidak dikirim**. Kirim nyata hanya bila disetel ``false``.
6. ``AEGIS_LIVE_MAX_ORDER_NOTIONAL`` membatasi nilai satu order.
7. Order market hanya boleh bila ``AEGIS_LIVE_ALLOW_MARKET=true``.

Yang tidak dilakukan modul ini: menyimpan key ke disk/log, mencetak nilai key,
atau menarik dana. Gunakan API key Binance dengan izin **Spot Trading saja**
dan withdrawal dimatikan, plus IP whitelist.

Peringatan: modul ini belum diuji terhadap akun Binance sungguhan dari
lingkungan pengembangan (tidak ada kredensial dan tidak ada akses jaringan ke
Binance di sana). Uji dulu dengan ``dry_run=true``, lalu nominal kecil.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from config import Settings
from execution import (
    Fill,
    Order,
    OrderRejected,
    OrderSide,
    OrderStatus,
    OrderType,
)
from logger import AuditLog

ACK_PHRASE = "SAYA PAHAM RISIKONYA"

# ccxt order type untuk Binance spot/futures.
_CCXT_TYPE = {
    OrderType.LIMIT: "limit",
    OrderType.MARKET: "market",
    OrderType.STOP_MARKET: "stop_market",
    OrderType.TAKE_PROFIT_MARKET: "take_profit_market",
}

_CCXT_STATUS = {
    "open": OrderStatus.OPEN,
    "new": OrderStatus.NEW,
    "closed": OrderStatus.FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "cancelled": OrderStatus.CANCELED,
    "expired": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
}


class LiveRefused(RuntimeError):
    """Live diminta tetapi pagar keselamatan belum lengkap."""


@dataclass
class LiveStatus:
    dry_run: bool = True
    connected: bool = False
    orders_sent: int = 0
    orders_rejected: int = 0
    last_error: str | None = None
    last_reconcile_ts: float = 0.0
    reconciled_orders: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "connected": self.connected,
            "orders_sent": self.orders_sent,
            "orders_rejected": self.orders_rejected,
            "last_error": self.last_error,
            "last_reconcile_ts": self.last_reconcile_ts,
            "reconciled_orders": self.reconciled_orders,
        }


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _redact(value: str) -> str:
    if not value:
        return "<kosong>"
    return f"{value[:4]}…{value[-2:]} ({len(value)} karakter)"


class LiveBroker:
    """Broker live dengan pagar berlapis. Antarmuka sama dengan PaperBroker."""

    name = "live"

    def __init__(
        self,
        settings: Settings,
        audit: AuditLog,
        exchange: Any | None = None,
    ) -> None:
        self.settings = settings
        self.cfg = settings.execution
        self.live = settings.live
        self.audit = audit
        self.status = LiveStatus(dry_run=settings.live.dry_run)
        self._orders: dict[str, Order] = {}
        self._exchange_ids: dict[str, str] = {}
        self._markets: dict[str, dict[str, Any]] = {}
        self._balance_cache: float = settings.initial_equity
        self._last_price: float | None = None

        self._check_gates()
        self._ex = exchange if exchange is not None else self._build_exchange()
        self._load_markets()
        self.reconcile()

    # ------------------------------------------------------------------ #
    # Pagar
    # ------------------------------------------------------------------ #
    def _check_gates(self) -> None:
        if not self.live.enabled:
            raise LiveRefused("AEGIS_LIVE_MODE belum true")
        if self.live.ack.strip().upper() != ACK_PHRASE.upper():
            raise LiveRefused(
                f"AEGIS_LIVE_ACK harus berisi frasa: {ACK_PHRASE!r} (persis, tanpa tanda kutip)"
            )
        if not _env("EXCHANGE_API_KEY") or not _env("EXCHANGE_API_SECRET"):
            raise LiveRefused("EXCHANGE_API_KEY / EXCHANGE_API_SECRET belum diisi di environment")
        if not self.live.symbols:
            raise LiveRefused("AEGIS_LIVE_SYMBOLS wajib diisi, mis. BTC/USDT")
        if self.live.max_order_notional <= 0:
            raise LiveRefused("AEGIS_LIVE_MAX_ORDER_NOTIONAL harus lebih besar dari 0")

    def _build_exchange(self) -> Any:
        try:
            import ccxt  # import lokal: paket tetap jalan tanpa ccxt saat paper
        except ImportError as exc:  # pragma: no cover
            raise LiveRefused("ccxt belum terpasang (`pip install ccxt`)") from exc
        exchange_id = self.settings.exchange_id or "binance"
        if not hasattr(ccxt, exchange_id):
            raise LiveRefused(f"exchange tidak dikenal ccxt: {exchange_id}")
        # Kredensial dibaca di sini dan hanya dipakai oleh ccxt. Tidak disimpan
        # sebagai atribut yang bisa tercetak, dan tidak pernah masuk log.
        client = getattr(ccxt, exchange_id)(
            {
                "apiKey": _env("EXCHANGE_API_KEY"),
                "secret": _env("EXCHANGE_API_SECRET"),
                "enableRateLimit": True,
                "options": {"adjustForTimeDifference": True, "recvWindow": 10_000},
            }
        )
        self.audit.log_event(
            "warning", "live_start",
            f"adapter live aktif: {exchange_id} · key {_redact(_env('EXCHANGE_API_KEY'))} · "
            f"dry_run={self.live.dry_run} · simbol {', '.join(self.live.symbols)}",
        )
        return client

    def _load_markets(self) -> None:
        try:
            self._markets = dict(self._ex.load_markets())
            self.status.connected = True
        except Exception as exc:  # noqa: BLE001 - laporkan, jangan jatuhkan bot diam-diam
            self.status.last_error = f"load_markets gagal: {exc}"
            self.audit.log_event("error", "live_markets", self.status.last_error)
            raise LiveRefused(self.status.last_error) from exc

    def _market(self, symbol: str) -> dict[str, Any]:
        if symbol not in self._markets:
            raise OrderRejected(f"symbol tidak ada di exchange: {symbol}")
        if symbol not in self.live.symbols:
            raise OrderRejected(
                f"symbol {symbol} tidak ada di AEGIS_LIVE_SYMBOLS ({', '.join(self.live.symbols)})"
            )
        return self._markets[symbol]

    # ------------------------------------------------------------------ #
    # Presisi & validasi
    # ------------------------------------------------------------------ #
    def _precise(self, symbol: str, qty: float, price: float | None) -> tuple[float, float | None]:
        market = self._market(symbol)
        try:
            amount = float(self._ex.amount_to_precision(symbol, qty))
        except Exception as exc:  # noqa: BLE001
            raise OrderRejected(f"presisi qty gagal: {exc}") from exc
        if amount <= 0:
            raise OrderRejected("qty menjadi 0 setelah pembulatan presisi exchange")

        limits = market.get("limits") or {}
        min_amount = ((limits.get("amount") or {}).get("min")) or 0
        if min_amount and amount < float(min_amount):
            raise OrderRejected(f"qty {amount} < minimum exchange {min_amount}")

        precise_price: float | None = None
        if price is not None:
            try:
                precise_price = float(self._ex.price_to_precision(symbol, price))
            except Exception as exc:  # noqa: BLE001
                raise OrderRejected(f"presisi harga gagal: {exc}") from exc

        reference = precise_price or (self._last_price or 0.0)
        notional = amount * reference
        min_cost = ((limits.get("cost") or {}).get("min")) or 0
        if min_cost and notional and notional < float(min_cost):
            raise OrderRejected(f"notional {notional:.4f} < minimum exchange {min_cost}")
        if notional > self.live.max_order_notional:
            raise OrderRejected(
                f"notional {notional:.2f} melebihi AEGIS_LIVE_MAX_ORDER_NOTIONAL "
                f"{self.live.max_order_notional:.2f}"
            )
        return amount, precise_price

    # ------------------------------------------------------------------ #
    # Antarmuka broker
    # ------------------------------------------------------------------ #
    def balance(self) -> float:
        if self.status.dry_run:
            return self._balance_cache
        try:
            book = self._ex.fetch_balance()
            quote = self.settings.quote_currency
            free = (book.get("free") or {}).get(quote)
            if free is not None:
                self._balance_cache = float(free)
        except Exception as exc:  # noqa: BLE001
            self.status.last_error = f"fetch_balance gagal: {exc}"
            self.audit.log_event("error", "live_balance", self.status.last_error)
        return self._balance_cache

    def place(self, order: Order) -> Order:
        if order.type == OrderType.MARKET and not self.live.allow_market:
            raise OrderRejected(
                "order market diblokir; setel AEGIS_LIVE_ALLOW_MARKET=true bila memang perlu"
            )
        amount, price = self._precise(order.symbol, order.qty, order.price)
        order.qty = amount
        if price is not None:
            order.price = price

        params: dict[str, Any] = {"newClientOrderId": order.client_id}
        if order.type in (OrderType.STOP_MARKET, OrderType.TAKE_PROFIT_MARKET):
            params["stopPrice"] = order.stop_price
            params["reduceOnly"] = True
        if order.meta.get("reduce_only"):
            params["reduceOnly"] = True

        payload = {
            "symbol": order.symbol,
            "type": _CCXT_TYPE[order.type],
            "side": "buy" if order.side == OrderSide.BUY else "sell",
            "amount": amount,
            "price": price,
            "params": {k: v for k, v in params.items() if v is not None},
        }
        self._orders[order.client_id] = order

        if self.status.dry_run:
            order.status = OrderStatus.NEW
            self.audit.log_event(
                "info", "live_dry_run",
                f"DRY-RUN: {order.side.value} {order.type.value} {order.symbol} "
                f"qty {amount} @ {price} (tidak dikirim)",
                {"payload": payload},
            )
            return order

        try:
            response = self._ex.create_order(**payload)
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            # Order dengan clientOrderId sama sudah ada: jangan kirim ulang.
            if "duplicate" in message.lower() or "-2010" in message:
                self.audit.log_event(
                    "warning", "live_duplicate",
                    f"order {order.client_id} sudah ada di exchange; tidak dikirim ulang",
                )
                order.status = OrderStatus.OPEN
                return order
            self.status.orders_rejected += 1
            self.status.last_error = message
            self.audit.log_event(
                "error", "live_rejected", f"order ditolak exchange: {message}",
                {"payload": payload},
            )
            raise OrderRejected(f"exchange menolak order: {message}") from exc

        self.status.orders_sent += 1
        self._apply_response(order, response)
        self.audit.log_event(
            "warning", "live_order",
            f"ORDER NYATA: {order.side.value} {order.type.value} {order.symbol} "
            f"qty {amount} @ {price} · id {response.get('id')} · status {order.status.value}",
            {"payload": payload, "response_id": response.get("id")},
        )
        return order

    def _apply_response(self, order: Order, response: dict[str, Any]) -> None:
        order.exchange_id = response.get("id")
        if order.exchange_id:
            self._exchange_ids[order.client_id] = str(order.exchange_id)
        order.status = _CCXT_STATUS.get(str(response.get("status") or "").lower(), order.status)
        filled = response.get("filled")
        if filled is not None:
            order.filled = float(filled)
        average = response.get("average") or response.get("price")
        if average is not None:
            order.avg_price = float(average)
        fee = (response.get("fee") or {}).get("cost") if isinstance(response.get("fee"), dict) else None
        if fee is not None:
            order.fee = float(fee)
        order.updated_at = time.time()

    def cancel(self, client_id: str) -> bool:
        order = self._orders.get(client_id)
        if order is None:
            return False
        if self.status.dry_run:
            order.status = OrderStatus.CANCELED
            self.audit.log_event("info", "live_dry_run", f"DRY-RUN: cancel {client_id}")
            return True
        try:
            self._ex.cancel_order(self._exchange_ids.get(client_id, client_id), order.symbol)
            order.status = OrderStatus.CANCELED
            order.updated_at = time.time()
            self.audit.log_event("warning", "live_cancel", f"order {client_id} dibatalkan di exchange")
            return True
        except Exception as exc:  # noqa: BLE001
            self.status.last_error = f"cancel gagal: {exc}"
            self.audit.log_event("error", "live_cancel", f"cancel {client_id} gagal: {exc}")
            return False

    def modify(self, client_id: str, **changes: object) -> Order | None:
        """Binance spot tidak punya amend: batalkan lalu kirim ulang."""
        order = self._orders.get(client_id)
        if order is None:
            return None
        if not self.cancel(client_id):
            return None
        replacement = Order(
            symbol=order.symbol,
            side=order.side,
            type=order.type,
            qty=float(changes.get("qty", order.qty)),
            price=changes.get("price", order.price),  # type: ignore[arg-type]
            stop_price=changes.get("stop_price", order.stop_price),  # type: ignore[arg-type]
            signal_id=order.signal_id,
            meta=dict(order.meta),
        )
        return self.place(replacement)

    def open_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.is_open]

    def all_orders(self) -> list[Order]:
        return list(self._orders.values())

    def on_price(self, snap: Any) -> list[Fill]:
        """Ambil status order terbuka dan ubah fill menjadi objek ``Fill``."""
        self._last_price = float(snap.last_price)
        if self.status.dry_run:
            return []
        fills: list[Fill] = []
        for order in list(self._orders.values()):
            if not order.is_open or order.exchange_id is None:
                continue
            try:
                response = self._ex.fetch_order(order.exchange_id, order.symbol)
            except Exception as exc:  # noqa: BLE001
                self.status.last_error = f"fetch_order gagal: {exc}"
                continue
            previous = order.filled
            self._apply_response(order, response)
            delta = order.filled - previous
            if delta > 0:
                price = order.avg_price or self._last_price or 0.0
                fills.append(
                    Fill(
                        client_id=order.client_id,
                        symbol=order.symbol,
                        side=order.side,
                        qty=delta,
                        price=float(price),
                        fee=float(order.fee or 0.0),
                        ts=time.time(),
                        order_type=order.type,
                    )
                )
        return fills

    # ------------------------------------------------------------------ #
    # Keselamatan
    # ------------------------------------------------------------------ #
    def cancel_all(self) -> int:
        canceled = 0
        for order in list(self.open_orders()):
            if self.cancel(order.client_id):
                canceled += 1
        return canceled

    def flatten(self, symbol: str, qty: float, is_long: bool) -> bool:
        """Tutup posisi di exchange dengan order market reduceOnly."""
        side = OrderSide.SELL if is_long else OrderSide.BUY
        order = Order(
            symbol=symbol, side=side, type=OrderType.MARKET, qty=qty,
            meta={"reduce_only": True, "kind": "flatten"},
        )
        if not self.live.allow_market:
            self.audit.log_event(
                "error", "live_flatten",
                "flatten butuh order market, tetapi AEGIS_LIVE_ALLOW_MARKET=false",
            )
            return False
        try:
            self.place(order)
            return True
        except OrderRejected as exc:
            self.audit.log_event("error", "live_flatten", f"flatten gagal: {exc}")
            return False

    def reconcile(self) -> dict[str, Any]:
        """Cocokkan order terbuka dan saldo dengan keadaan di exchange."""
        summary: dict[str, Any] = {"open_orders": 0, "balance": None, "errors": []}
        if self.status.dry_run:
            self.audit.log_event("info", "live_reconcile", "dry-run: rekonsiliasi dilewati")
            return summary
        try:
            open_orders = self._ex.fetch_open_orders()
            summary["open_orders"] = len(open_orders)
            self.status.reconciled_orders = len(open_orders)
            for item in open_orders:
                client_id = (item.get("info") or {}).get("clientOrderId") or item.get("clientOrderId")
                if client_id and client_id in self._orders:
                    self._orders[client_id].exchange_id = str(item.get("id"))
                    self._exchange_ids[client_id] = str(item.get("id"))
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"fetch_open_orders: {exc}")
        try:
            summary["balance"] = self.balance()
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"balance: {exc}")
        self.status.last_reconcile_ts = time.time()
        self.audit.log_event("info", "live_reconcile", "rekonsiliasi selesai", summary)
        return summary
