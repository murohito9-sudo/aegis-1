"""AEGIS-1 — JARVIS #05: Market Microstructure Engine.

Membaca order book dan trade flow, lalu meringkasnya jadi angka yang bisa
dipakai lapisan lain:

* ``spread_bps`` — biaya masuk-keluar paling dasar.
* ``depth_1pct`` — nilai notional yang tersedia dalam 1% dari mid, di dua sisi.
* ``imbalance`` — rasio tekanan beli terhadap jual di dalam jendela itu.
* ``trade_flow`` — VWAP dan agresivitas beli/jual dari daftar trade.

Semua fungsi inti murni (menerima list/dict), sehingga bisa diuji tanpa
jaringan. ``MicrostructureFeed`` adalah pembungkus ccxt yang opsional: kalau
exchange tidak memberi order book, ia jatuh ke estimasi dari candle dan
menandainya jelas sebagai estimasi, bukan data nyata.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence


@dataclass
class BookMetrics:
    spread_bps: float = 0.0
    mid: float = 0.0
    bid_depth: float = 0.0        # notional dalam jendela depth_pct
    ask_depth: float = 0.0
    imbalance: float = 0.5        # 0 = semua jual, 1 = semua beli
    depth_pct: float = 0.01
    levels: int = 0
    source: str = "book"

    @property
    def total_depth(self) -> float:
        return self.bid_depth + self.ask_depth

    def as_dict(self) -> dict[str, Any]:
        return {
            "spread_bps": round(self.spread_bps, 3),
            "mid": round(self.mid, 8),
            "bid_depth": round(self.bid_depth, 2),
            "ask_depth": round(self.ask_depth, 2),
            "imbalance": round(self.imbalance, 4),
            "depth_pct": self.depth_pct,
            "levels": self.levels,
            "source": self.source,
        }


@dataclass
class TradeFlow:
    vwap: float = 0.0
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    trades: int = 0
    aggressive_ratio: float = 0.5   # volume beli / total volume
    source: str = "trades"

    def as_dict(self) -> dict[str, Any]:
        return {
            "vwap": round(self.vwap, 8),
            "buy_volume": round(self.buy_volume, 6),
            "sell_volume": round(self.sell_volume, 6),
            "trades": self.trades,
            "aggressive_ratio": round(self.aggressive_ratio, 4),
            "source": self.source,
        }


def analyze_book(
    bids: Sequence[Sequence[float]],
    asks: Sequence[Sequence[float]],
    depth_pct: float = 0.01,
) -> BookMetrics:
    """Hitung spread, kedalaman, dan imbalance dari order book mentah.

    ``bids``/``asks`` berisi ``[harga, qty]``, seperti keluaran ccxt.
    """
    bids = [b for b in bids if len(b) >= 2 and float(b[0]) > 0]
    asks = [a for a in asks if len(a) >= 2 and float(a[0]) > 0]
    if not bids or not asks:
        return BookMetrics(depth_pct=depth_pct, source="kosong")

    best_bid = max(float(b[0]) for b in bids)
    best_ask = min(float(a[0]) for a in asks)
    if best_ask <= best_bid:
        return BookMetrics(depth_pct=depth_pct, source="book-tidak-valid")

    mid = (best_bid + best_ask) / 2
    spread_bps = (best_ask - best_bid) / mid * 10_000
    lower = mid * (1 - depth_pct)
    upper = mid * (1 + depth_pct)

    bid_depth = sum(float(p) * float(q) for p, q in ((b[0], b[1]) for b in bids) if float(p) >= lower)
    ask_depth = sum(float(p) * float(q) for p, q in ((a[0], a[1]) for a in asks) if float(p) <= upper)
    total = bid_depth + ask_depth
    imbalance = (bid_depth / total) if total > 0 else 0.5

    return BookMetrics(
        spread_bps=spread_bps,
        mid=mid,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
        imbalance=imbalance,
        depth_pct=depth_pct,
        levels=len(bids) + len(asks),
        source="book",
    )


def analyze_trades(trades: Iterable[Sequence[Any]]) -> TradeFlow:
    """Ringkas trade flow dari ``[ts, price, qty, side?]`` atau dict ccxt."""
    buy_vol = sell_vol = notional = volume = 0.0
    count = 0
    for item in trades:
        if isinstance(item, dict):
            price = float(item.get("price") or 0)
            qty = float(item.get("amount") or item.get("qty") or 0)
            side = str(item.get("side") or "").lower()
        else:
            if len(item) < 3:
                continue
            price = float(item[1] or 0)
            qty = float(item[2] or 0)
            side = str(item[3]).lower() if len(item) > 3 else ""
        if price <= 0 or qty <= 0:
            continue
        count += 1
        notional += price * qty
        volume += qty
        if side == "buy":
            buy_vol += qty
        elif side == "sell":
            sell_vol += qty
        else:
            # Tanpa label sisi, pakai aturan tick: qty besar dianggap agresif beli.
            buy_vol += qty / 2
            sell_vol += qty / 2
    total = buy_vol + sell_vol
    return TradeFlow(
        vwap=(notional / volume) if volume > 0 else 0.0,
        buy_volume=buy_vol,
        sell_volume=sell_vol,
        trades=count,
        aggressive_ratio=(buy_vol / total) if total > 0 else 0.5,
    )


def estimate_from_candles(candles: Any, depth_pct: float = 0.01) -> BookMetrics:
    """Perkiraan kasar bila exchange tidak memberi order book.

    Ditandai ``source="estimasi-candle"`` supaya tidak pernah dikira data nyata.
    Kedalaman diperkirakan dari volume candle terakhir.
    """
    if candles is None or len(candles) == 0:
        return BookMetrics(depth_pct=depth_pct, source="estimasi-candle")
    last = candles.iloc[-1]
    close = float(last["close"])
    volume = float(last["volume"])
    notional = close * volume
    up = float(last["close"]) >= float(last["open"])
    split = 0.55 if up else 0.45
    return BookMetrics(
        spread_bps=1.0,
        mid=close,
        bid_depth=notional * split,
        ask_depth=notional * (1 - split),
        imbalance=split,
        depth_pct=depth_pct,
        levels=0,
        source="estimasi-candle",
    )


@dataclass
class MicrostructureFeed:
    """Pembungkus exchange (ccxt). Exchange diinjeksi agar mudah diuji."""

    exchange: Any | None = None
    symbol: str = "BTC/USDT"
    depth_limit: int = 20
    depth_pct: float = 0.01
    errors: list[str] = field(default_factory=list)

    def book(self) -> BookMetrics:
        if self.exchange is None:
            return BookMetrics(depth_pct=self.depth_pct, source="tanpa-exchange")
        try:
            raw = self.exchange.fetch_order_book(self.symbol, self.depth_limit)
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"fetch_order_book: {exc}")
            return BookMetrics(depth_pct=self.depth_pct, source="gagal-orderbook")
        return analyze_book(raw.get("bids") or [], raw.get("asks") or [], self.depth_pct)

    def trades(self, limit: int = 100) -> TradeFlow:
        if self.exchange is None:
            return TradeFlow(source="tanpa-exchange")
        try:
            raw = self.exchange.fetch_trades(self.symbol, limit=limit)
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"fetch_trades: {exc}")
            return TradeFlow(source="gagal-trades")
        return analyze_trades(raw)
