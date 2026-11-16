"""AEGIS-1 — unit test.

Fokus pada hal yang bisa menghabiskan uang bila salah:

* position sizing dan pembulatan ke bawah,
* seluruh batas risiko (rugi harian, exposure, drawdown, slot),
* checklist pra-trade menolak saat kondisi tidak aman,
* logika sinyal: tidak ada sinyal dari konfirmasi tunggal,
* deduplikasi order dan penolakan mode live,
* gate kualitas data.

Jalankan: ``python tests.py`` (atau ``python -m pytest tests.py -q``).
"""

from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from config import Mode, RiskConfig, Settings, StrategyConfig
from data import DataQualityGate, MarketSnapshot, SyntheticFeed, timeframe_seconds
from execution import (
    LiveBrokerUnavailable,
    Order,
    OrderRejected,
    OrderSide,
    OrderType,
    PaperBroker,
    build_broker,
)
from indicators import add_indicators, atr, ema, rsi, swing_points
from logger import AuditLog
from risk import RiskManager, round_down_to_step
from strategy import LONG, SHORT, evaluate


def make_df(n: int = 320, trend: float = 0.0008, seed: int = 3, noise: float = 0.002) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 60_000 * np.cumprod(1 + rng.normal(trend, noise, n))
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, noise * 0.5, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, noise * 0.5, n)))
    volume = np.abs(rng.normal(100, 25, n)) + 20
    ts = 1_700_000_000 + 900 * np.arange(n)
    return pd.DataFrame({"ts": ts, "open": open_, "high": high, "low": low,
                         "close": close, "volume": volume})


class TempProject(unittest.TestCase):
    """Basis: direktori sementara untuk database audit."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.settings = Settings(
            db_path=self.tmp / "test.sqlite3",
            jsonl_path=self.tmp / "test.jsonl",
            kill_switch_file=self.tmp / "KILL",
            initial_equity=10_000.0,
            offline=True,
        )
        self.audit = AuditLog(self.settings.db_path, self.settings.jsonl_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def snapshot(self, df: pd.DataFrame, spread_bps: float = 2.0) -> MarketSnapshot:
        price = float(df["close"].iloc[-1])
        half = price * spread_bps / 20_000.0
        return MarketSnapshot(
            symbol="BTC/USDT", timeframe="15m", candles=df,
            best_bid=price - half, best_ask=price + half,
            last_price=price, ts=float(df["ts"].iloc[-1]) + 1.0, source="test",
        )


# --------------------------------------------------------------------------- #
class TestIndicators(unittest.TestCase):
    def test_ema_converges_and_rsi_bounds(self) -> None:
        df = make_df()
        e = ema(df["close"], 50)
        self.assertAlmostEqual(float(e.iloc[-1]), float(df["close"].tail(50).mean()), delta=400)
        r = rsi(df["close"], 14).dropna()
        self.assertTrue(((r >= 0) & (r <= 100)).all())

    def test_atr_positive(self) -> None:
        df = make_df()
        self.assertTrue((atr(df, 14).dropna() > 0).all())

    def test_swings_are_confirmed_not_current_bar(self) -> None:
        df = make_df()
        sh, sl = swing_points(df, 5)
        # Bar terakhir tidak boleh ditandai swing: butuh konfirmasi kanan.
        self.assertFalse(bool(sh.iloc[-1]))
        self.assertFalse(bool(sl.iloc[-1]))

    def test_indicator_columns_present(self) -> None:
        df = make_df()
        out = add_indicators(df, StrategyConfig())
        for col in ("ema_fast", "ema_slow", "rsi", "macd", "macd_signal", "atr",
                    "roc", "rel_volume", "atr_pct_rank"):
            self.assertIn(col, out.columns)
        self.assertFalse(out["ema_slow"].iloc[-1] != out["ema_slow"].iloc[-1])  # bukan NaN


# --------------------------------------------------------------------------- #
class TestSizing(TempProject):
    def test_risk_based_size(self) -> None:
        # Slot tunggal + exposure penuh supaya batas tidak mendominasi perhitungan.
        rm = RiskManager(RiskConfig(risk_per_trade=0.01, max_total_exposure=1.0,
                                    max_leverage=1.0, max_open_positions=1),
                         initial_equity=10_000, step_size=0.00001)
        res = rm.position_size(entry=100.0, stop=98.0)  # risiko 2 per unit, 1% = 100
        self.assertTrue(res.valid)
        self.assertAlmostEqual(res.qty, 50.0, places=4)
        self.assertAlmostEqual(res.qty * 2.0, 100.0, places=2)

    def test_round_down_never_rounds_up(self) -> None:
        self.assertEqual(round_down_to_step(1.23456789, 0.001), 1.234)
        self.assertEqual(round_down_to_step(0.000019, 0.00001), 0.00001)
        self.assertLessEqual(round_down_to_step(9.999999, 0.1), 9.999999)

    def test_capped_by_exposure(self) -> None:
        rm = RiskManager(RiskConfig(risk_per_trade=0.5, max_total_exposure=0.2,
                                    max_leverage=1.0, max_open_positions=3),
                         initial_equity=10_000, step_size=0.00001)
        res = rm.position_size(entry=100.0, stop=99.9)  # tanpa cap akan sangat besar
        self.assertTrue(res.valid)
        self.assertLessEqual(res.notional, 10_000 * 0.2 / 3 + 1e-6)
        self.assertIn(res.capped_by, ("exposure/leverage", "slot_posisi"))

    def test_rejects_zero_stop_distance(self) -> None:
        rm = RiskManager(RiskConfig(), initial_equity=10_000)
        self.assertFalse(rm.position_size(entry=100.0, stop=100.0).valid)

    def test_min_notional_rejection(self) -> None:
        rm = RiskManager(RiskConfig(risk_per_trade=0.00001), initial_equity=100,
                         step_size=0.00001, min_notional=5.0)
        res = rm.position_size(entry=100.0, stop=99.0)
        self.assertFalse(res.valid)


# --------------------------------------------------------------------------- #
class TestRiskLimits(TempProject):
    def test_daily_loss_limit(self) -> None:
        rm = RiskManager(RiskConfig(max_daily_loss=0.03), initial_equity=10_000)
        self.assertFalse(rm.daily_loss_breached())
        rm.update_equity(9_600)   # -4%
        self.assertTrue(rm.daily_loss_breached())

    def test_drawdown_limit(self) -> None:
        rm = RiskManager(RiskConfig(max_drawdown=0.10), initial_equity=10_000)
        rm.update_equity(9_500)   # -5%
        self.assertFalse(rm.drawdown_breached())
        rm.update_equity(8_900)   # -11%
        self.assertTrue(rm.drawdown_breached())

    def test_mark_to_market_counts_unrealized(self) -> None:
        rm = RiskManager(RiskConfig(max_daily_loss=0.03), initial_equity=10_000)
        rm.update_mark(9_500)
        self.assertAlmostEqual(rm.daily_loss(), 0.05, places=4)
        self.assertTrue(rm.daily_loss_breached())

    def test_day_roll_resets_daily_loss(self) -> None:
        rm = RiskManager(RiskConfig(max_daily_loss=0.03), initial_equity=10_000)
        rm.update_equity(9_500)
        rm.roll_day_if_needed(time.time() + 86400)
        self.assertFalse(rm.daily_loss_breached())

    def test_max_positions_and_exposure(self) -> None:
        rm = RiskManager(RiskConfig(max_open_positions=3), initial_equity=10_000)
        from strategy import Signal
        sig = Signal("BTC/USDT", 1.0, LONG, 0.9, 100.0, 98.0, 104.0, 2.0, "trending_up", 1.0)
        for _ in range(3):
            rm.open_position(sig, 1.0, 100.0, 0.1)
        snap = self.snapshot(make_df())
        checks = {c.name: c for c in rm.pre_trade_check(
            sig, snap, Mode.PAPER, gate_passed=True, pending_orders=0)}
        self.assertFalse(checks["8_slot_posisi"].ok)

    def test_partial_take_profit_plan(self) -> None:
        rm = RiskManager(RiskConfig(partial_take_profit_r=(1.0, 2.0),
                                    trailing_after_r=1.0, trailing_atr_mult=1.5),
                         initial_equity=10_000, step_size=0.00001)
        from strategy import Signal
        sig = Signal("BTC/USDT", 1.0, LONG, 0.9, 100.0, 98.0, 104.0, 2.0, "trending_up", 1.0)
        pos = rm.open_position(sig, 10.0, 100.0, 0.1)
        plan = rm.plan_management(pos, 102.0, atr_value=1.0)  # +1R
        self.assertEqual(plan["action"], "partial_tp")
        self.assertAlmostEqual(plan["qty"], 5.0, places=4)
        rm.register_partial(pos, 5.0, 102.0, 0.05, 1.0)
        self.assertAlmostEqual(pos.size, 5.0, places=4)
        plan2 = rm.plan_management(pos, 104.0, atr_value=1.0)  # +2R
        self.assertEqual(plan2["action"], "partial_tp")

    def test_trailing_stop_only_moves_in_favour(self) -> None:
        rm = RiskManager(RiskConfig(trailing_after_r=1.0, trailing_atr_mult=1.5,
                                    partial_take_profit_r=()),
                         initial_equity=10_000, step_size=0.00001)
        from strategy import Signal
        sig = Signal("BTC/USDT", 1.0, LONG, 0.9, 100.0, 98.0, 110.0, 2.0, "trending_up", 1.0)
        pos = rm.open_position(sig, 1.0, 100.0, 0.1)
        plan = rm.plan_management(pos, 103.0, atr_value=1.0)
        self.assertEqual(plan["action"], "trail")
        self.assertGreater(plan["new_stop"], pos.stop)


# --------------------------------------------------------------------------- #
class TestPreTradeChecklist(TempProject):
    def _signal(self):
        from strategy import Signal
        return Signal("BTC/USDT", 1.0, LONG, 0.85, 100.0, 98.0, 104.0, 2.0, "trending_up", 1.0)

    def test_all_pass_in_good_conditions(self) -> None:
        rm = RiskManager(RiskConfig(), initial_equity=10_000)
        snap = self.snapshot(make_df())
        checks = rm.pre_trade_check(self._signal(), snap, Mode.PAPER,
                                    gate_passed=True, pending_orders=0)
        failed = [c for c in checks if not c.ok]
        self.assertEqual(failed, [], [f"{c.name}: {c.detail}" for c in failed])
        self.assertEqual(len(checks), 16)

    def test_kill_switch_blocks(self) -> None:
        rm = RiskManager(RiskConfig(), initial_equity=10_000)
        snap = self.snapshot(make_df())
        checks = {c.name: c for c in rm.pre_trade_check(
            self._signal(), snap, Mode.KILL, gate_passed=True, pending_orders=0)}
        self.assertFalse(checks["6_kill_switch"].ok)

    def test_paused_blocks(self) -> None:
        rm = RiskManager(RiskConfig(), initial_equity=10_000)
        checks = {c.name: c for c in rm.pre_trade_check(
            self._signal(), self.snapshot(make_df()), Mode.PAUSED,
            gate_passed=True, pending_orders=0)}
        self.assertFalse(checks["6_kill_switch"].ok)

    def test_wide_spread_blocks(self) -> None:
        rm = RiskManager(RiskConfig(max_spread_bps=5.0), initial_equity=10_000)
        checks = {c.name: c for c in rm.pre_trade_check(
            self._signal(), self.snapshot(make_df(), spread_bps=40.0), Mode.PAPER,
            gate_passed=True, pending_orders=0)}
        self.assertFalse(checks["7_spread_likuiditas"].ok)

    def test_failed_gate_blocks(self) -> None:
        rm = RiskManager(RiskConfig(), initial_equity=10_000)
        checks = {c.name: c for c in rm.pre_trade_check(
            self._signal(), self.snapshot(make_df()), Mode.PAPER,
            gate_passed=False, pending_orders=0)}
        self.assertFalse(checks["11_kualitas_data"].ok)

    def test_pending_order_blocks(self) -> None:
        rm = RiskManager(RiskConfig(), initial_equity=10_000)
        checks = {c.name: c for c in rm.pre_trade_check(
            self._signal(), self.snapshot(make_df()), Mode.PAPER,
            gate_passed=True, pending_orders=1)}
        self.assertFalse(checks["12_tanpa_order_ganda"].ok)

    def test_duplicate_bar_blocks(self) -> None:
        rm = RiskManager(RiskConfig(), initial_equity=10_000)
        rm.last_traded_bar_ts = 1.0
        checks = {c.name: c for c in rm.pre_trade_check(
            self._signal(), self.snapshot(make_df()), Mode.PAPER,
            gate_passed=True, pending_orders=0)}
        self.assertFalse(checks["13_bukan_duplikat_bar"].ok)

    def test_cooldown_blocks(self) -> None:
        rm = RiskManager(RiskConfig(), initial_equity=10_000)
        rm.cooldown_until_bar_ts = 5.0
        checks = {c.name: c for c in rm.pre_trade_check(
            self._signal(), self.snapshot(make_df()), Mode.PAPER,
            gate_passed=True, pending_orders=0)}
        self.assertFalse(checks["14_cooldown_pasca_loss"].ok)

    def test_bad_reward_risk_blocks(self) -> None:
        rm = RiskManager(RiskConfig(min_reward_risk=2.0), initial_equity=10_000)
        sig = self._signal()
        sig.reward_risk = 1.2
        checks = {c.name: c for c in rm.pre_trade_check(
            sig, self.snapshot(make_df()), Mode.PAPER, gate_passed=True, pending_orders=0)}
        self.assertFalse(checks["3_reward_risk"].ok)


# --------------------------------------------------------------------------- #
class TestStrategy(unittest.TestCase):
    def test_insufficient_data_gives_no_signal(self) -> None:
        sig = evaluate(make_df(n=120), StrategyConfig(), RiskConfig())
        self.assertFalse(sig.actionable)
        self.assertTrue(any("data kurang" in v for v in sig.vetoes))

    def test_uptrend_can_produce_long_or_veto_but_never_short(self) -> None:
        sig = evaluate(make_df(n=400, trend=0.0015, noise=0.0015), StrategyConfig(), RiskConfig())
        self.assertNotEqual(sig.side, SHORT)

    def test_downtrend_never_long_without_confirmation(self) -> None:
        sig = evaluate(make_df(n=400, trend=-0.0015, noise=0.0015, seed=11),
                       StrategyConfig(), RiskConfig())
        self.assertNotEqual(sig.side, LONG)

    def test_signal_always_has_explanation(self) -> None:
        for trend in (-0.001, 0.0, 0.001):
            sig = evaluate(make_df(n=400, trend=trend), StrategyConfig(), RiskConfig())
            self.assertTrue(sig.reasons or sig.vetoes)

    def test_extreme_rsi_vetoes_chasing(self) -> None:
        # Tren sangat kuat membuat RSI ekstrem: bot harus menolak mengejar.
        sig = evaluate(make_df(n=400, trend=0.0015, noise=0.0015, seed=1),
                       StrategyConfig(), RiskConfig())
        self.assertFalse(sig.actionable)
        self.assertTrue(any("RSI" in v for v in sig.vetoes))

    def test_actionable_signal_respects_reward_risk_and_stops(self) -> None:
        found = False
        for trend in (0.0004, 0.0006, 0.0008):
            for seed in range(1, 9):
                sig = evaluate(make_df(n=400, trend=trend, noise=0.0025, seed=seed),
                               StrategyConfig(), RiskConfig())
                if not sig.actionable:
                    continue
                found = True
                self.assertGreaterEqual(sig.reward_risk, 2.0 - 1e-9)
                self.assertGreater(sig.score, 0.0)
                if sig.side == LONG:
                    self.assertLess(sig.stop, sig.entry)
                    self.assertGreater(sig.take_profit, sig.entry)
                else:
                    self.assertGreater(sig.stop, sig.entry)
                    self.assertLess(sig.take_profit, sig.entry)
        self.assertTrue(found, "tidak ada sinyal actionable pada data uji tren naik")

    def test_flat_data_never_actionable(self) -> None:
        df = make_df(n=400, trend=0.0, noise=0.00001, seed=5)
        sig = evaluate(df, StrategyConfig(), RiskConfig())
        self.assertFalse(sig.actionable)


# --------------------------------------------------------------------------- #
class TestDataGate(TempProject):
    def test_good_data_passes(self) -> None:
        gate = DataQualityGate(self.settings)
        df = make_df(n=400)
        snap = self.snapshot(df)
        snap.ts = float(df["ts"].iloc[-1]) + 1.0
        self.assertTrue(gate.check(snap).passed)

    def test_too_few_bars_fails(self) -> None:
        gate = DataQualityGate(self.settings)
        snap = self.snapshot(make_df(n=100))
        self.assertFalse(gate.check(snap).passed)

    def test_stale_data_fails(self) -> None:
        gate = DataQualityGate(self.settings)
        df = make_df(n=400)
        snap = self.snapshot(df)
        snap.ts = float(df["ts"].iloc[-1]) + timeframe_seconds("15m") * 10
        result = gate.check(snap)
        self.assertFalse(result.passed)
        self.assertTrue(any(c.name == "kesegaran_data" and not c.ok for c in result.checks))

    def test_wide_spread_fails(self) -> None:
        gate = DataQualityGate(self.settings)
        df = make_df(n=400)
        snap = self.snapshot(df, spread_bps=60.0)
        self.assertFalse(gate.check(snap).passed)

    def test_duplicate_timestamps_fail(self) -> None:
        gate = DataQualityGate(self.settings)
        df = make_df(n=400)
        df.loc[df.index[-2], "ts"] = df["ts"].iloc[-3]
        snap = self.snapshot(df)
        self.assertFalse(gate.check(snap).passed)


# --------------------------------------------------------------------------- #
class TestExecution(TempProject):
    def _broker(self) -> PaperBroker:
        return PaperBroker(self.settings, self.audit, initial_balance=10_000.0)

    def test_limit_fill_charges_fee_and_slippage(self) -> None:
        broker = self._broker()
        snap = self.snapshot(make_df())
        order = broker.place(Order(symbol="BTC/USDT", side=OrderSide.BUY,
                                   type=OrderType.LIMIT, qty=0.01, price=snap.best_ask))
        fills = broker.on_price(snap)
        self.assertEqual(len(fills), 1)
        self.assertGreater(fills[0].fee, 0.0)
        self.assertEqual(order.status.value, "FILLED")

    def test_duplicate_signal_is_rejected(self) -> None:
        broker = self._broker()
        snap = self.snapshot(make_df())
        broker.place(Order(symbol="BTC/USDT", side=OrderSide.BUY, type=OrderType.LIMIT,
                           qty=0.01, price=snap.best_ask, signal_id="sig-1"))
        with self.assertRaises(OrderRejected):
            broker.place(Order(symbol="BTC/USDT", side=OrderSide.BUY, type=OrderType.LIMIT,
                               qty=0.01, price=snap.best_ask, signal_id="sig-1"))

    def test_stop_order_triggers_on_price(self) -> None:
        broker = self._broker()
        df = make_df()
        snap = self.snapshot(df)
        stop_price = snap.last_price * 0.98
        broker.place(Order(symbol="BTC/USDT", side=OrderSide.SELL,
                           type=OrderType.STOP_MARKET, qty=0.01, stop_price=stop_price))
        # Harga belum menyentuh stop.
        self.assertEqual(broker.on_price(snap), [])
        # Harga jatuh melewati stop.
        crash = MarketSnapshot(symbol=snap.symbol, timeframe=snap.timeframe, candles=df,
                               best_bid=stop_price * 0.99, best_ask=stop_price * 0.991,
                               last_price=stop_price * 0.99, ts=snap.ts + 60, source="test")
        fills = broker.on_price(crash)
        self.assertEqual(len(fills), 1)
        self.assertLess(fills[0].price, stop_price)

    def test_min_qty_rejected(self) -> None:
        broker = self._broker()
        snap = self.snapshot(make_df())
        with self.assertRaises(OrderRejected):
            broker.place(Order(symbol="BTC/USDT", side=OrderSide.BUY,
                               type=OrderType.LIMIT, qty=1e-12, price=snap.best_ask))

    def test_limit_order_expires(self) -> None:
        broker = self._broker()
        df = make_df()
        snap = self.snapshot(df)
        far = snap.last_price * 0.5
        order = broker.place(Order(symbol="BTC/USDT", side=OrderSide.BUY,
                                   type=OrderType.LIMIT, qty=0.01, price=far))
        order.ts -= (self.settings.execution.order_timeout_s + 1)
        broker.on_price(snap)
        self.assertEqual(order.status.value, "EXPIRED")


class TestLiveRefusal(TempProject):
    def test_live_mode_is_refused(self) -> None:
        live = replace(self.settings, live_mode=True)
        with self.assertRaises(LiveBrokerUnavailable):
            build_broker(live, self.audit)

    def test_default_build_is_paper(self) -> None:
        broker = build_broker(self.settings, self.audit)
        self.assertEqual(broker.name, "paper")


# --------------------------------------------------------------------------- #
class TestDashboardContract(TempProject):
    def test_confirm_phrases_exist_for_all_actions(self) -> None:
        from dashboard import CONFIRM_PHRASES
        self.assertEqual(set(CONFIRM_PHRASES), {"pause", "resume", "close_all", "kill"})
        for phrase in CONFIRM_PHRASES.values():
            self.assertGreater(len(phrase), 5)

    def test_index_html_has_no_secret_storage(self) -> None:
        html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("localStorage.", html)
        self.assertNotIn("sessionStorage.", html)
        self.assertIn("AEGIS-1 Trading Console", html)
        self.assertIn("__AEGIS_CSRF__", html)


class TestFeedAndLogging(TempProject):
    def test_synthetic_feed_is_deterministic(self) -> None:
        a = SyntheticFeed(seed=42).snapshot(300)
        b = SyntheticFeed(seed=42).snapshot(300)
        self.assertAlmostEqual(float(a.candles["close"].iloc[-2]),
                               float(b.candles["close"].iloc[-2]), places=6)

    def test_audit_roundtrip(self) -> None:
        self.audit.log_event("info", "test", "halo", {"a": 1})
        self.audit.log_order({"client_id": "x1", "symbol": "BTC/USDT", "side": "BUY",
                              "type": "LIMIT", "qty": 1.0, "price": 100.0, "status": "OPEN"})
        self.audit.log_equity(10_000, 10_000, 0, 0, 0)
        self.assertTrue(self.audit.recent_events(5))
        self.assertEqual(self.audit.recent_orders(5)[0]["client_id"], "x1")
        self.assertTrue(self.audit.equity_curve(5))
        self.assertTrue(self.settings.jsonl_path.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
