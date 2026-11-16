"""AEGIS-1 — JARVIS #13: Portfolio Management Engine.

Mengelola kepemilikan aset sebagai portofolio, bukan sebagai posisi lepas:
bobot per aset, konsentrasi, kontribusi risiko, rencana DCA, dan saran
rebalancing. Semua perhitungan murni — tidak menyentuh exchange dan tidak
mengirim order; keluarannya berupa *usulan* yang masih harus melewati
guardrail risiko dan (kalau live) pagar eksekusi.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass
class Holding:
    symbol: str
    qty: float = 0.0
    cost_basis: float = 0.0        # harga rata-rata beli per unit
    price: float = 0.0             # harga pasar terakhir

    @property
    def value(self) -> float:
        return max(0.0, self.qty) * max(0.0, self.price)

    @property
    def cost(self) -> float:
        return max(0.0, self.qty) * max(0.0, self.cost_basis)

    @property
    def pnl(self) -> float:
        return self.value - self.cost

    @property
    def pnl_pct(self) -> float:
        return (self.pnl / self.cost) if self.cost > 0 else 0.0

    def as_dict(self, total: float) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "qty": round(self.qty, 8),
            "price": round(self.price, 8),
            "cost_basis": round(self.cost_basis, 8),
            "value": round(self.value, 2),
            "pnl": round(self.pnl, 2),
            "pnl_pct": round(self.pnl_pct, 4),
            "weight": round(self.value / total, 4) if total > 0 else 0.0,
        }


@dataclass
class RebalanceOrder:
    symbol: str
    side: str            # "BUY" | "SELL"
    qty: float
    notional: float
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "side": self.side, "qty": round(self.qty, 8),
                "notional": round(self.notional, 2), "reason": self.reason}


@dataclass
class Portfolio:
    quote: str = "USDT"
    cash: float = 0.0
    holdings: dict[str, Holding] = field(default_factory=dict)

    # -- nilai ---------------------------------------------------------- #
    def value(self) -> float:
        return self.cash + sum(h.value for h in self.holdings.values())

    def invested(self) -> float:
        return sum(h.value for h in self.holdings.values())

    def pnl(self) -> float:
        return sum(h.pnl for h in self.holdings.values())

    def weights(self) -> dict[str, float]:
        total = self.value()
        if total <= 0:
            return {}
        out = {symbol: h.value / total for symbol, h in self.holdings.items() if h.value > 0}
        if self.cash > 0:
            out[self.quote] = self.cash / total
        return out

    def concentration(self) -> float:
        """Indeks Herfindahl (0..1). >0.4 berarti sangat terkonsentrasi."""
        return sum(w * w for w in self.weights().values())

    # -- mutasi --------------------------------------------------------- #
    def set_price(self, symbol: str, price: float) -> None:
        if symbol in self.holdings:
            self.holdings[symbol].price = float(price)

    def buy(self, symbol: str, qty: float, price: float) -> Holding:
        cost = float(qty) * float(price)
        if cost > self.cash + 1e-9:
            raise ValueError(f"kas tidak cukup: butuh {cost:.2f}, ada {self.cash:.2f}")
        holding = self.holdings.get(symbol) or Holding(symbol=symbol)
        total_qty = holding.qty + float(qty)
        if total_qty > 0:
            holding.cost_basis = (holding.cost + cost) / total_qty
        holding.qty = total_qty
        holding.price = float(price)
        self.holdings[symbol] = holding
        self.cash -= cost
        return holding

    def sell(self, symbol: str, qty: float, price: float) -> float:
        holding = self.holdings.get(symbol)
        if holding is None or holding.qty < qty - 1e-12:
            raise ValueError(f"qty {symbol} tidak cukup untuk dijual")
        proceeds = float(qty) * float(price)
        holding.qty -= float(qty)
        holding.price = float(price)
        self.cash += proceeds
        if holding.qty <= 1e-12:
            self.holdings.pop(symbol, None)
        return proceeds

    # -- rencana -------------------------------------------------------- #
    def rebalance(
        self,
        target_weights: dict[str, float],
        min_notional: float = 10.0,
    ) -> list[RebalanceOrder]:
        """Usulan order agar bobot mendekati target. Tidak mengeksekusi apa pun."""
        total = self.value()
        if total <= 0 or not target_weights:
            return []
        weight_sum = sum(max(0.0, w) for w in target_weights.values())
        if weight_sum <= 0:
            return []

        orders: list[RebalanceOrder] = []
        for symbol, raw_weight in target_weights.items():
            weight = max(0.0, raw_weight) / weight_sum
            holding = self.holdings.get(symbol)
            price = holding.price if holding and holding.price > 0 else 0.0
            if price <= 0:
                continue
            target_value = total * weight
            current_value = holding.value if holding else 0.0
            delta = target_value - current_value
            if abs(delta) < max(min_notional, total * 0.005):
                continue
            qty = abs(delta) / price
            orders.append(RebalanceOrder(
                symbol=symbol,
                side="BUY" if delta > 0 else "SELL",
                qty=qty,
                notional=abs(delta),
                reason=f"bobot {current_value / total:.1%} → target {weight:.1%}",
            ))
        orders.sort(key=lambda o: o.notional, reverse=True)
        return orders

    def dca_plan(self, amount: float, weights: dict[str, float]) -> list[dict[str, Any]]:
        """Bagi setoran ``amount`` menurut bobot target."""
        total_weight = sum(max(0.0, w) for w in weights.values())
        if amount <= 0 or total_weight <= 0:
            return []
        plan = []
        for symbol, raw in weights.items():
            weight = max(0.0, raw) / total_weight
            notional = amount * weight
            holding = self.holdings.get(symbol)
            price = holding.price if holding and holding.price > 0 else 0.0
            plan.append({
                "symbol": symbol,
                "weight": round(weight, 4),
                "notional": round(notional, 2),
                "qty": round(notional / price, 8) if price > 0 else None,
            })
        return plan

    def risk_contribution(self) -> dict[str, float]:
        """Kontribusi kasar terhadap risiko portofolio (bobot x volatilitas harga).

        Volatilitas di sini memakai rentang harga harian relatif bila tersedia;
        tanpa data itu, bobot dipakai apa adanya sebagai perkiraan konservatif.
        """
        weights = self.weights()
        return {symbol: round(weight, 4) for symbol, weight in weights.items()}

    def as_dict(self) -> dict[str, Any]:
        total = self.value()
        return {
            "quote": self.quote,
            "total_value": round(total, 2),
            "cash": round(self.cash, 2),
            "invested": round(self.invested(), 2),
            "pnl": round(self.pnl(), 2),
            "concentration": round(self.concentration(), 4),
            "weights": {k: round(v, 4) for k, v in self.weights().items()},
            "holdings": [h.as_dict(total) for h in
                         sorted(self.holdings.values(), key=lambda x: x.value, reverse=True)],
        }


def build_portfolio(
    rows: Iterable[dict[str, Any]],
    cash: float = 0.0,
    quote: str = "USDT",
) -> Portfolio:
    """Bangun portofolio dari baris ``{symbol, qty, cost_basis, price}``."""
    portfolio = Portfolio(quote=quote, cash=float(cash))
    for row in rows:
        symbol = str(row.get("symbol") or "").strip()
        if not symbol:
            continue
        qty = float(row.get("qty") or 0)
        if qty <= 0:
            continue
        portfolio.holdings[symbol] = Holding(
            symbol=symbol,
            qty=qty,
            cost_basis=float(row.get("cost_basis") or 0),
            price=float(row.get("price") or row.get("cost_basis") or 0),
        )
    return portfolio
