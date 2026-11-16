"""AEGIS-1 — analitik kinerja dan laporan P&L.

Modul ini hanya memakai standard library, supaya bisa dipakai dari dashboard,
dari unit test, maupun dari skrip laporan tanpa menarik pandas.

Definisi angka (tidak ada yang disembunyikan):

* ``gross_pnl`` — jumlah PnL trade apa adanya. Nilai ini sudah dipotong fee
  keluar dan fee partial take profit, tetapi belum memotong fee masuk.
* ``net_pnl`` — ``gross_pnl`` dikurangi seluruh fee masuk. Ini angka yang
  paling dekat dengan uang yang benar-benar berpindah.
* ``expectancy_*`` — rata-rata per trade. Bukan janji, hanya statistik dari
  trade yang sudah terjadi.
* ``sharpe_trade`` / ``sortino_trade`` — rasio rata-rata terhadap simpangan
  baku return per trade (return = PnL / ekuitas awal). Sengaja TIDAK
  disetahunkan, karena jumlah trade per tahun berbeda-beda antar pasar.
* ``max_drawdown`` — dihitung dari kurva ekuitas (bila tersedia) atau dari
  akumulasi PnL berurutan.
"""

from __future__ import annotations

import csv
import io
import math
import statistics
from typing import Any, Callable, Iterable, Sequence

TRADE_COLUMNS = [
    "closed_at", "symbol", "side", "entry", "exit", "size",
    "pnl", "net_pnl", "r_multiple", "mae_r", "mfe_r",
    "fees", "entry_fee", "duration_s", "reason",
]

CSV_HEADERS = [
    "waktu_tutup", "pair", "side", "entry", "exit", "size",
    "pnl", "net_pnl", "r", "mae_r", "mfe_r", "fee", "fee_masuk",
    "durasi_detik", "alasan",
]

REASON_LABEL = {
    "stop_loss": "stop loss",
    "take_profit": "take profit",
    "trailing_stop": "trailing stop",
    "partial_tp": "partial take profit",
    "close_all": "tutup semua (operator)",
    "kill_switch": "kill switch",
    "manual": "manual",
    "end_of_data": "akhir data",
}


# --------------------------------------------------------------------------- #
# Normalisasi
# --------------------------------------------------------------------------- #
def _f(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def normalize_trade(trade: Any) -> dict[str, Any]:
    """Terima ``ClosedTrade``, dict, atau sqlite Row; kembalikan dict rata."""
    if isinstance(trade, dict):
        src = trade
    elif hasattr(trade, "__dict__"):
        src = vars(trade)
    else:  # sqlite3.Row dan sejenisnya
        src = {k: trade[k] for k in trade.keys()}  # type: ignore[union-attr]

    def pick(*names: str, default: Any = None) -> Any:
        for name in names:
            if name in src and src[name] is not None:
                return src[name]
        return default

    entry_fee = _f(pick("entry_fee"))
    fees = _f(pick("fees", "total_fees"), entry_fee)
    pnl = _f(pick("pnl"))
    opened = _f(pick("opened_at"))
    closed = _f(pick("closed_at"))

    return {
        "symbol": str(pick("symbol", default="—")),
        "side": str(pick("side", default="—")),
        "entry": _f(pick("entry")),
        "exit": _f(pick("exit")),
        "size": _f(pick("size")),
        "pnl": pnl,
        "net_pnl": pnl - entry_fee,
        "r_multiple": _f(pick("r_multiple")),
        "mae_r": _f(pick("mae_r")),
        "mfe_r": _f(pick("mfe_r")),
        "fees": fees,
        "entry_fee": entry_fee,
        "opened_at": opened,
        "closed_at": closed,
        "duration_s": max(0.0, closed - opened) if opened and closed else 0.0,
        "reason": str(pick("reason", "close_reason", default="—")),
    }


def normalize_order(order: Any) -> dict[str, Any]:
    if isinstance(order, dict):
        src = order
    elif hasattr(order, "__dict__"):
        src = vars(order)
    else:
        src = {k: order[k] for k in order.keys()}  # type: ignore[union-attr]
    qty = _f(src.get("filled") or src.get("qty"))
    price = _f(src.get("avg_price") or src.get("price"))
    return {
        "ts": _f(src.get("ts")),
        "symbol": str(src.get("symbol") or "—"),
        "side": str(src.get("side") or "—"),
        "type": str(src.get("type") or "—"),
        "qty": qty,
        "price": price,
        "notional": qty * price,
        "status": str(src.get("status") or "—"),
        "fee": _f(src.get("fee")),
        "slippage_bps": _f(src.get("slippage_bps")),
        "signal_id": src.get("signal_id"),
    }


# --------------------------------------------------------------------------- #
# Metrik
# --------------------------------------------------------------------------- #
def _streaks(pnls: Sequence[float]) -> tuple[int, int, int, int]:
    best_win = best_loss = cur_win = cur_loss = 0
    for pnl in pnls:
        if pnl > 0:
            cur_win += 1
            cur_loss = 0
        elif pnl < 0:
            cur_loss += 1
            cur_win = 0
        else:
            cur_win = cur_loss = 0
        best_win = max(best_win, cur_win)
        best_loss = max(best_loss, cur_loss)
    return best_win, best_loss, cur_win, cur_loss


def _drawdown(curve: Sequence[float]) -> tuple[float, float]:
    """Kembalikan (drawdown_maks_fraksi, drawdown_maks_uang)."""
    peak = None
    worst_frac = 0.0
    worst_money = 0.0
    for value in curve:
        if peak is None or value > peak:
            peak = value
        if peak and peak > 0:
            dd_money = peak - value
            dd_frac = dd_money / peak
            if dd_frac > worst_frac:
                worst_frac = dd_frac
            if dd_money > worst_money:
                worst_money = dd_money
    return worst_frac, worst_money


def metrics(
    trades: Iterable[Any],
    initial_equity: float = 10_000.0,
    equity_curve: Sequence[float] | None = None,
) -> dict[str, Any]:
    rows = [normalize_trade(t) for t in trades]
    rows.sort(key=lambda r: r["closed_at"])
    n = len(rows)
    initial_equity = _f(initial_equity, 10_000.0) or 10_000.0

    if not n:
        return {
            "trades": 0, "wins": 0, "losses": 0, "breakeven": 0,
            "win_rate": 0.0, "gross_pnl": 0.0, "net_pnl": 0.0,
            "gross_profit": 0.0, "gross_loss": 0.0, "profit_factor": 0.0,
            "expectancy_money": 0.0, "expectancy_r": 0.0,
            "avg_win": 0.0, "avg_loss": 0.0, "payoff": 0.0,
            "largest_win": 0.0, "largest_loss": 0.0,
            "max_win_streak": 0, "max_loss_streak": 0,
            "current_streak": 0, "avg_duration_s": 0.0,
            "total_fees": 0.0, "sharpe_trade": 0.0, "sortino_trade": 0.0,
            "max_drawdown": 0.0, "max_drawdown_money": 0.0,
            "recovery_factor": 0.0, "avg_mae_r": 0.0, "avg_mfe_r": 0.0,
            "avg_r": 0.0, "return_pct": 0.0,
        }

    pnls = [r["pnl"] for r in rows]
    nets = [r["net_pnl"] for r in rows]
    rs = [r["r_multiple"] for r in rows]
    wins = [r for r in rows if r["net_pnl"] > 0]
    losses = [r for r in rows if r["net_pnl"] < 0]
    breakeven = n - len(wins) - len(losses)

    gross_profit = sum(r["net_pnl"] for r in wins)
    gross_loss = abs(sum(r["net_pnl"] for r in losses))
    net_pnl = sum(nets)
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = 99.0 if gross_profit > 0 else 0.0

    returns = [r["net_pnl"] / initial_equity for r in rows]
    mean_ret = statistics.fmean(returns)
    stdev = statistics.pstdev(returns) if n > 1 else 0.0
    downside = [x for x in returns if x < 0]
    down_dev = math.sqrt(sum(x * x for x in downside) / len(downside)) if downside else 0.0

    if equity_curve:
        curve = [_f(v) for v in equity_curve]
    else:
        curve = [initial_equity]
        running = initial_equity
        for value in nets:
            running += value
            curve.append(running)
    dd_frac, dd_money = _drawdown(curve)

    best_win, best_loss, cur_win, cur_loss = _streaks(nets)
    avg_win = gross_profit / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0

    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": breakeven,
        "win_rate": len(wins) / n,
        "gross_pnl": sum(pnls),
        "net_pnl": net_pnl,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": profit_factor,
        "expectancy_money": net_pnl / n,
        "expectancy_r": statistics.fmean(rs) if rs else 0.0,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff": (avg_win / avg_loss) if avg_loss > 0 else 0.0,
        "largest_win": max(nets),
        "largest_loss": min(nets),
        "max_win_streak": best_win,
        "max_loss_streak": best_loss,
        "current_streak": cur_win if cur_win else -cur_loss,
        "avg_duration_s": statistics.fmean([r["duration_s"] for r in rows]),
        "total_fees": sum(r["fees"] for r in rows),
        "sharpe_trade": (mean_ret / stdev) if stdev > 0 else 0.0,
        "sortino_trade": (mean_ret / down_dev) if down_dev > 0 else 0.0,
        "max_drawdown": dd_frac,
        "max_drawdown_money": dd_money,
        "recovery_factor": (net_pnl / dd_money) if dd_money > 0 else 0.0,
        "avg_mae_r": statistics.fmean([r["mae_r"] for r in rows]),
        "avg_mfe_r": statistics.fmean([r["mfe_r"] for r in rows]),
        "avg_r": statistics.fmean(rs) if rs else 0.0,
        "return_pct": net_pnl / initial_equity,
    }


def group(
    rows: Sequence[dict[str, Any]],
    key: Callable[[dict[str, Any]], str],
) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(key(row), []).append(row)
    out = []
    for name in sorted(buckets):
        items = buckets[name]
        wins = [i for i in items if i["net_pnl"] > 0]
        losses = [i for i in items if i["net_pnl"] < 0]
        gp = sum(i["net_pnl"] for i in wins)
        gl = abs(sum(i["net_pnl"] for i in losses))
        out.append({
            "name": name,
            "trades": len(items),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(items) if items else 0.0,
            "net_pnl": sum(i["net_pnl"] for i in items),
            "profit_factor": (gp / gl) if gl > 0 else (99.0 if gp > 0 else 0.0),
            "avg_r": statistics.fmean([i["r_multiple"] for i in items]),
        })
    return out


def _hour_key(row: dict[str, Any]) -> str:
    ts = row["closed_at"]
    if not ts:
        return "—"
    import time as _time

    return f"{_time.gmtime(ts).tm_hour:02d}:00"


def build_report(
    trades: Iterable[Any],
    orders: Iterable[Any] = (),
    initial_equity: float = 10_000.0,
    equity_curve: Sequence[float] | None = None,
    meta: dict[str, Any] | None = None,
    trade_limit: int = 200,
    order_limit: int = 200,
) -> dict[str, Any]:
    rows = [normalize_trade(t) for t in trades]
    rows.sort(key=lambda r: r["closed_at"], reverse=True)
    all_rows = rows
    order_rows = [normalize_order(o) for o in orders]
    order_rows.sort(key=lambda r: r["ts"], reverse=True)

    return {
        "meta": dict(meta or {}),
        "metrics": metrics(all_rows, initial_equity, equity_curve),
        "trades": all_rows[:trade_limit],
        "orders": order_rows[:order_limit],
        "by_symbol": group(all_rows, lambda r: r["symbol"]),
        "by_side": group(all_rows, lambda r: r["side"]),
        "by_hour": group(all_rows, _hour_key),
        "by_reason": group(all_rows, lambda r: r["reason"]),
        "order_fees": {
            "total": sum(o["fee"] for o in order_rows),
            "avg_slippage_bps": (
                statistics.fmean([o["slippage_bps"] for o in order_rows]) if order_rows else 0.0
            ),
        },
    }


# --------------------------------------------------------------------------- #
# Ekspor
# --------------------------------------------------------------------------- #
def _money(value: float) -> str:
    return f"{value:,.2f}"


def _pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def _dur(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m"
    return f"{seconds // 86400}d {seconds % 86400 // 3600}h"


def to_markdown(report: dict[str, Any]) -> str:
    m = report["metrics"]
    meta = report.get("meta") or {}
    out: list[str] = []
    out.append("# Laporan kinerja AEGIS-1\n")
    if meta:
        out.append("| Kunci | Nilai |")
        out.append("| --- | --- |")
        for key in ("symbol", "timeframe", "mode", "sumber_data", "dibuat", "replay"):
            if key in meta:
                out.append(f"| {key} | {meta[key]} |")
        out.append("")
    out.append("## Ringkasan")
    out.append("")
    out.append("| Metrik | Nilai |")
    out.append("| --- | --- |")
    rows = [
        ("Jumlah trade", str(m["trades"])),
        ("Menang / kalah / seri", f"{m['wins']} / {m['losses']} / {m['breakeven']}"),
        ("Win rate", _pct(m["win_rate"])),
        ("PnL bersih", _money(m["net_pnl"])),
        ("PnL kotor (sebelum fee masuk)", _money(m["gross_pnl"])),
        ("Profit factor", f"{m['profit_factor']:.2f}"),
        ("Expectancy per trade", _money(m["expectancy_money"])),
        ("Expectancy per trade (R)", f"{m['expectancy_r']:+.2f}R"),
        ("Rata-rata menang / kalah", f"{_money(m['avg_win'])} / {_money(m['avg_loss'])}"),
        ("Payoff ratio", f"{m['payoff']:.2f}"),
        ("Trade menang terbesar", _money(m["largest_win"])),
        ("Trade kalah terbesar", _money(m["largest_loss"])),
        ("Streak menang / kalah terpanjang", f"{m['max_win_streak']} / {m['max_loss_streak']}"),
        ("Sharpe per trade (tidak disetahunkan)", f"{m['sharpe_trade']:.2f}"),
        ("Sortino per trade", f"{m['sortino_trade']:.2f}"),
        ("Drawdown maksimum", f"{_pct(m['max_drawdown'])} ({_money(m['max_drawdown_money'])})"),
        ("Recovery factor", f"{m['recovery_factor']:.2f}"),
        ("Rata-rata MAE / MFE", f"{m['avg_mae_r']:+.2f}R / {m['avg_mfe_r']:+.2f}R"),
        ("Total fee", _money(m["total_fees"])),
        ("Rata-rata durasi trade", _dur(m["avg_duration_s"])),
        ("Return terhadap ekuitas awal", _pct(m["return_pct"])),
    ]
    for label, value in rows:
        out.append(f"| {label} | {value} |")
    out.append("")

    for title, key, first in (
        ("Per simbol", "by_symbol", "Pair"),
        ("Per arah", "by_side", "Arah"),
        ("Per jam tutup (UTC)", "by_hour", "Jam"),
        ("Per alasan tutup", "by_reason", "Alasan"),
    ):
        items = report.get(key) or []
        if not items:
            continue
        out.append(f"## {title}")
        out.append("")
        out.append(f"| {first} | Trade | Menang | Kalah | Win rate | PnL bersih | PF | Rata-rata R |")
        out.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for item in items:
            out.append(
                f"| {item['name']} | {item['trades']} | {item['wins']} | {item['losses']} | "
                f"{_pct(item['win_rate'])} | {_money(item['net_pnl'])} | "
                f"{item['profit_factor']:.2f} | {item['avg_r']:+.2f} |"
            )
        out.append("")

    trades = report.get("trades") or []
    if trades:
        out.append("## Daftar trade (terbaru lebih dulu)")
        out.append("")
        out.append("| Tutup | Pair | Side | Entry | Exit | Size | PnL | Net | R | MAE | MFE | Fee | Durasi | Alasan |")
        out.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for t in trades:
            out.append(
                f"| {_clock(t['closed_at'])} | {t['symbol']} | {t['side']} | {t['entry']:.5f} | "
                f"{t['exit']:.5f} | {t['size']:.5f} | {_money(t['pnl'])} | {_money(t['net_pnl'])} | "
                f"{t['r_multiple']:+.2f} | {t['mae_r']:+.2f} | {t['mfe_r']:+.2f} | "
                f"{_money(t['fees'])} | {_dur(t['duration_s'])} | {REASON_LABEL.get(t['reason'], t['reason'])} |"
            )
        out.append("")

    orders = report.get("orders") or []
    if orders:
        out.append("## Daftar order")
        out.append("")
        out.append("| Waktu | Pair | Side | Tipe | Qty | Harga | Notional | Status | Fee | Slip |")
        out.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for o in orders:
            out.append(
                f"| {_clock(o['ts'])} | {o['symbol']} | {o['side']} | {o['type']} | "
                f"{o['qty']:.6f} | {o['price']:.5f} | {_money(o['notional'])} | {o['status']} | "
                f"{_money(o['fee'])} | {o['slippage_bps']:.2f} bp |"
            )
        out.append("")

    out.append("## Catatan metodologi")
    out.append("")
    out.append("- Angka dihitung dari trade yang sudah ditutup dan order yang tercatat di audit log.")
    out.append("- PnL bersih = PnL tercatat dikurangi fee masuk.")
    out.append("- Sharpe dan Sortino dihitung per trade dari return terhadap ekuitas awal, "
               "tidak disetahunkan.")
    out.append("- MAE (maximum adverse excursion) dan MFE (maximum favourable excursion) "
               "diukur dalam R selama posisi terbuka.")
    out.append("- Bukan nasihat investasi; hasil simulasi tidak menjamin hasil nyata.")
    return "\n".join(out) + "\n"


def _clock(ts: float) -> str:
    if not ts:
        return "—"
    import time as _time

    return _time.strftime("%Y-%m-%d %H:%M", _time.gmtime(ts))


def to_csv(report: dict[str, Any]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADERS)
    for t in report.get("trades") or []:
        writer.writerow([
            _clock(t["closed_at"]), t["symbol"], t["side"],
            f"{t['entry']:.8f}", f"{t['exit']:.8f}", f"{t['size']:.8f}",
            f"{t['pnl']:.4f}", f"{t['net_pnl']:.4f}", f"{t['r_multiple']:.4f}",
            f"{t['mae_r']:.4f}", f"{t['mfe_r']:.4f}",
            f"{t['fees']:.4f}", f"{t['entry_fee']:.4f}",
            int(t["duration_s"]), t["reason"],
        ])
    return buf.getvalue()
