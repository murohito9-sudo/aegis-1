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
from data import CCXTFeed, DataQualityGate, MarketSnapshot, SyntheticFeed, timeframe_seconds
from evaluation import EvaluationEngine
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
        self.assertFalse(bool(sh.iloc[-1]))
        self.assertFalse(bool(sl.iloc[-1]))

    def test_indicator_columns_present(self) -> None:
        df = make_df()
        out = add_indicators(df, StrategyConfig())
        for col in ("ema_fast", "ema_slow", "rsi", "macd", "macd_signal", "atr",
                    "roc", "rel_volume", "atr_pct_rank"):
            self.assertIn(col, out.columns)
        self.assertFalse(out["ema_slow"].iloc[-1] != out["ema_slow"].iloc[-1])


# --------------------------------------------------------------------------- #
class TestSizing(TempProject):
    def test_risk_based_size(self) -> None:
        rm = RiskManager(RiskConfig(risk_per_trade=0.01, max_total_exposure=1.0,
                                    max_leverage=1.0, max_open_positions=1),
                         initial_equity=10_000, step_size=0.00001)
        res = rm.position_size(entry=100.0, stop=98.0)
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
        res = rm.position_size(entry=100.0, stop=99.9)
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
        rm.update_equity(9_600)
        self.assertTrue(rm.daily_loss_breached())

    def test_drawdown_limit(self) -> None:
        rm = RiskManager(RiskConfig(max_drawdown=0.10), initial_equity=10_000)
        rm.update_equity(9_500)
        self.assertFalse(rm.drawdown_breached())
        rm.update_equity(8_900)
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
        plan = rm.plan_management(pos, 102.0, atr_value=1.0)
        self.assertEqual(plan["action"], "partial_tp")
        self.assertAlmostEqual(plan["qty"], 5.0, places=4)
        rm.register_partial(pos, 5.0, 102.0, 0.05, 1.0)
        self.assertAlmostEqual(pos.size, 5.0, places=4)
        plan2 = rm.plan_management(pos, 104.0, atr_value=1.0)
        self.assertEqual(plan2["action"], "partial_tp")

    def test_trailing_stop_only_moves_in_favour(self) -> None:
        rm = RiskManager(RiskConfig(trailing_after_r=1.0, trailing_atr_mult=1.5,
                                    partial_take_profit_r=()),
                         initial_equity=10_000, step_size=0.00001)
        from strategy import Signal
        sig = Signal("BTC/USDT", 1.0, LONG, 0.9, 100.0, 95.0, 110.0, 2.0, "trending_up", 1.0)
        pos = rm.open_position(sig, 1.0, 100.0, 0.1)
        # Initial risk is 5 points (100 entry, 95 stop), so 1R is 105.
        plan = rm.plan_management(pos, 105.0, atr_value=1.0)
        self.assertEqual(plan["action"], "breakeven")
        self.assertAlmostEqual(plan["new_stop"], pos.entry, places=8)
        pos.stop = plan["new_stop"]
        pos.breakeven_done = True
        # At 106 the trade is above the 1R trailing activation threshold.
        plan2 = rm.plan_management(pos, 106.0, atr_value=1.0)
        self.assertEqual(plan2["action"], "trail")
        self.assertGreater(plan2["new_stop"], pos.stop)


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
        self.assertEqual(len(checks), 17)

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
        self.assertFalse(checks["12_kualitas_data"].ok)

    def test_pending_order_blocks(self) -> None:
        rm = RiskManager(RiskConfig(), initial_equity=10_000)
        checks = {c.name: c for c in rm.pre_trade_check(
            self._signal(), self.snapshot(make_df()), Mode.PAPER,
            gate_passed=True, pending_orders=1)}
        self.assertFalse(checks["13_tanpa_order_ganda"].ok)

    def test_duplicate_bar_blocks(self) -> None:
        rm = RiskManager(RiskConfig(), initial_equity=10_000)
        rm.last_traded_bar_ts = 1.0
        checks = {c.name: c for c in rm.pre_trade_check(
            self._signal(), self.snapshot(make_df()), Mode.PAPER,
            gate_passed=True, pending_orders=0)}
        self.assertFalse(checks["14_bukan_duplikat_bar"].ok)

    def test_cooldown_blocks(self) -> None:
        rm = RiskManager(RiskConfig(), initial_equity=10_000)
        rm.cooldown_until_bar_ts = 5.0
        checks = {c.name: c for c in rm.pre_trade_check(
            self._signal(), self.snapshot(make_df()), Mode.PAPER,
            gate_passed=True, pending_orders=0)}
        self.assertFalse(checks["15_cooldown_pasca_loss"].ok)

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


    def test_nan_open_fails(self) -> None:
        df = make_df(n=400)
        df.loc[df.index[-1], "open"] = np.nan
        result = DataQualityGate(self.settings).check(self.snapshot(df))
        self.assertFalse(result.passed)
        self.assertTrue(any(c.name == "angka_ohlcv_finite" and not c.ok for c in result.checks))

    def test_infinite_volume_fails(self) -> None:
        df = make_df(n=400)
        df.loc[df.index[-1], "volume"] = np.inf
        result = DataQualityGate(self.settings).check(self.snapshot(df))
        self.assertFalse(result.passed)
        self.assertTrue(any(c.name == "angka_ohlcv_finite" and not c.ok for c in result.checks))

    def test_negative_volume_fails(self) -> None:
        df = make_df(n=400)
        df.loc[df.index[-1], "volume"] = -1.0
        self.assertFalse(DataQualityGate(self.settings).check(self.snapshot(df)).passed)

    def test_open_outside_candle_range_fails(self) -> None:
        df = make_df(n=400)
        df.loc[df.index[-1], "open"] = df.loc[df.index[-1], "high"] * 1.01
        self.assertFalse(DataQualityGate(self.settings).check(self.snapshot(df)).passed)

    def test_future_bar_timestamp_fails(self) -> None:
        df = make_df(n=400)
        snap = self.snapshot(df)
        snap.ts = float(df["ts"].iloc[-1]) - 1.0
        result = DataQualityGate(self.settings).check(snap)
        self.assertFalse(result.passed)
        self.assertTrue(any(c.name == "kesegaran_data" and not c.ok for c in result.checks))


class TestCCXTQuoteProvenance(unittest.TestCase):
    class FakeExchange:
        def __init__(self, bid, ask):
            self.bid = bid
            self.ask = ask

        def fetch_ohlcv(self, symbol, timeframe, limit):
            now_ms = int(time.time() * 1000)
            return [[now_ms - 60_000, 99.0, 101.0, 98.0, 100.0, 10.0]]

        def fetch_ticker(self, symbol):
            return {"last": 100.0, "bid": self.bid, "ask": self.ask}

    def _feed(self, bid, ask):
        feed = CCXTFeed.__new__(CCXTFeed)
        feed.exchange_id = "fake"
        feed.symbol = "BTC/USDT"
        feed.timeframe = "1m"
        feed._ex = self.FakeExchange(bid, ask)
        return feed

    def test_missing_quotes_are_not_fabricated(self):
        snap = self._feed(None, None).snapshot(10)
        self.assertEqual(snap.best_bid, 0.0)
        self.assertEqual(snap.best_ask, 0.0)
        self.assertTrue(snap.source.endswith("quotes=missing"))

    def test_observed_quotes_are_preserved(self):
        snap = self._feed(99.9, 100.1).snapshot(10)
        self.assertEqual(snap.best_bid, 99.9)
        self.assertEqual(snap.best_ask, 100.1)
        self.assertTrue(snap.source.endswith("quotes=observed"))


class TestEvaluationEngine(unittest.TestCase):
    def test_confluence_score_is_not_treated_as_probability(self) -> None:
        engine = EvaluationEngine()
        engine.record(score=0.9, side="LONG", r_multiple=1.0, ts=1.0)
        report = engine.report().as_dict()
        self.assertIsNone(report["brier"])
        self.assertEqual(report["probability_samples"], 0)
        self.assertEqual(report["calibration"], [])
        self.assertTrue(report["score_performance"])

    def test_explicit_probability_calculates_brier(self) -> None:
        engine = EvaluationEngine()
        engine.record(score=0.8, probability=0.75, side="LONG", r_multiple=1.0, ts=1.0)
        engine.record(score=0.6, probability=0.25, side="SHORT", r_multiple=-1.0, ts=2.0)
        report = engine.report().as_dict()
        self.assertAlmostEqual(report["brier"], 0.0625)
        self.assertEqual(report["probability_samples"], 2)
        self.assertEqual(len(report["calibration"]), 2)

    def test_missing_score_is_preserved_as_missing(self) -> None:
        engine = EvaluationEngine()
        self.assertEqual(engine.load([{"ts": 1, "r_multiple": 1.0}]), 1)
        self.assertEqual(engine.report().as_dict()["score_performance"], [])

    def test_non_finite_probability_is_rejected(self) -> None:
        engine = EvaluationEngine()
        with self.assertRaises(ValueError):
            engine.record(score=0.5, probability=float("nan"), side="LONG", r_multiple=1, ts=1)


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
        self.assertEqual(broker.on_price(snap), [])
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


    def test_research_endpoint_requires_csrf_and_returns_read_only_report(self) -> None:
        from fastapi.testclient import TestClient
        from dashboard import Hub, create_app

        class StubEngine:
            async def state(self):
                return {}

            def history(self):
                return {"orders": [], "trades": [], "signals": []}

            def report(self):
                return {}

            def report_markdown(self):
                return ""

            def report_csv(self):
                return ""

            def briefing(self):
                return {}

            def health(self):
                return {"ok": True, "tick_count": 7, "issues": []}

            def portfolio(self):
                return {}

            def research(self):
                return {"available": False, "samples": 2, "required_samples": 30}

            async def execute_action(self, action):
                return {"action": action}

        with TestClient(create_app(StubEngine(), Hub())) as client:
            page = client.get("/")
            self.assertEqual(page.status_code, 200)
            token = page.text.split('name="aegis-csrf" content="', 1)[1].split('"', 1)[0]
            denied = client.get("/api/research")
            self.assertEqual(denied.status_code, 403)
            response = client.get("/api/research", headers={"X-AEGIS-CSRF": token})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["samples"], 2)
            health = client.get("/api/health", headers={"X-AEGIS-CSRF": token})
            self.assertTrue(health.json()["ok"])

    def test_frontend_navigation_and_responsive_contract(self) -> None:
        from html.parser import HTMLParser

        html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")

        class IdCollector(HTMLParser):
            def __init__(self):
                super().__init__()
                self.ids = []

            def handle_starttag(self, tag, attrs):
                self.ids.extend(value for key, value in attrs if key == "id" and value)

        parser = IdCollector()
        parser.feed(html)
        duplicates = sorted({value for value in parser.ids if parser.ids.count(value) > 1})
        self.assertEqual(duplicates, [])
        for panel in ("ringkasan", "strategi", "research", "monitoring"):
            self.assertIn(f'data-panel="{panel}"', html)
            self.assertIn(f'id="panel-{panel}"', html)
        for legacy_id in ("chart", "pos-body", "hist-body", "report", "btn-kill", "m-next", "k-equity", "rep-kpis"):
            self.assertIn(f'id="{legacy_id}"', html)
        self.assertIn('@media(max-width:1050px)', html)
        self.assertIn('@media(max-width:620px)', html)
        self.assertIn('fetch("/api/health"', html)
        self.assertIn('fetch("/api/research"', html)


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


class TestForexAndReplay(TempProject):
    """Fondasi forex: replay historis, metadata instrumen, spread dari data."""

    def _csv(self, rows: int = 40, with_volume: bool = True, with_spread: bool = False) -> Path:
        import csv as _csv

        path = self.tmp / "eurusd.csv"
        start = 1_700_000_000
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = _csv.writer(fh)
            header = ["ts", "open", "high", "low", "close"]
            if with_volume:
                header.append("volume")
            if with_spread:
                header.append("spread")
            writer.writerow(header)
            price = 1.0850
            for i in range(rows):
                price += 0.00012 * ((i % 7) - 3)
                row = [start + i * 3600, price, price + 0.0004, price - 0.0004, price]
                if with_volume:
                    row.append(1000 + i)
                if with_spread:
                    row.append(0.00008)
                writer.writerow(row)
        return path

    def test_replay_serves_bar_clock_not_wall_clock(self) -> None:
        from data import ReplayFeed

        path = self._csv()
        feed = ReplayFeed(path, "EUR/USD", "1h", start_bars=10)
        snap = feed.snapshot(300)
        self.assertEqual(len(snap.candles), 10)
        self.assertAlmostEqual(snap.ts, float(snap.candles["ts"].iloc[-1]), places=3)
        self.assertLess(snap.ts, time.time() - 3600)
        self.assertIn("replay:", snap.source)

    def test_replay_advance_moves_one_bar_and_stops_at_end(self) -> None:
        from data import ReplayFeed

        path = self._csv(rows=6)
        feed = ReplayFeed(path, "EUR/USD", "1h", start_bars=2)
        first = feed.snapshot(300).ts
        self.assertTrue(feed.advance())
        self.assertGreater(feed.snapshot(300).ts, first)
        for _ in range(10):
            feed.advance()
        self.assertTrue(feed.done)
        self.assertEqual(feed.progress["bar"], feed.progress["total"])

    def test_replay_gate_passes_on_old_data(self) -> None:
        from data import ReplayFeed

        path = self._csv(rows=400)
        feed = ReplayFeed(path, "EUR/USD", "1h", start_bars=400)
        snap = feed.snapshot(400)
        settings = Settings(
            db_path=self.tmp / "r.sqlite3",
            jsonl_path=self.tmp / "r.jsonl",
            symbol="EUR/USD",
            timeframe="1h",
            initial_equity=10_000.0,
            offline=True,
        )
        result = DataQualityGate(settings).check(snap)
        freshness = [c for c in result.checks if c.name == "kesegaran_data"][0]
        self.assertTrue(freshness.ok, freshness.detail)

    def test_volume_fallback_allows_csv_without_volume(self) -> None:
        from data import CSVFeed

        path = self._csv(with_volume=False)
        with self.assertRaises(ValueError):
            CSVFeed(path, "EUR/USD", "1h")
        feed = CSVFeed(path, "EUR/USD", "1h", volume_fallback=1.0)
        snap = feed.snapshot(30)
        self.assertTrue((snap.candles["volume"] > 0).all())

    def test_spread_comes_from_data_when_available(self) -> None:
        from data import CSVFeed

        path = self._csv(with_spread=True)
        feed = CSVFeed(path, "EUR/USD", "1h", spread_bps=50.0)
        snap = feed.snapshot(30)
        self.assertLess(snap.spread_bps, 2.0)
        self.assertGreater(snap.spread_bps, 0.1)

    def test_session_windows_and_weekend(self) -> None:
        from config import SessionConfig, parse_windows

        windows = parse_windows("07:00-16:00")
        session = SessionConfig(enabled=True, windows=windows)
        monday_10 = 1_700_000_000 - (1_700_000_000 % 86400) + 10 * 3600
        self.assertTrue(session.is_open(monday_10))
        self.assertFalse(session.is_open(monday_10 + 8 * 3600))
        weekend = SessionConfig(skip_weekend=True)
        saturday = monday_10 + 5 * 86400
        self.assertFalse(weekend.is_open(saturday))

    def test_session_overnight_window(self) -> None:
        from config import SessionConfig, parse_windows

        session = SessionConfig(enabled=True, windows=parse_windows("22:00-02:00"))
        base = 1_700_000_000 - (1_700_000_000 % 86400)
        self.assertTrue(session.is_open(base + 23 * 3600))
        self.assertTrue(session.is_open(base + 1 * 3600))
        self.assertFalse(session.is_open(base + 12 * 3600))


class TestAnalytics(unittest.TestCase):
    def _trade(self, pnl: float, r: float, **kw: object) -> dict[str, object]:
        row = {
            "symbol": "BTC/USDT", "side": "LONG", "entry": 100.0, "exit": 100.0 + pnl,
            "size": 1.0, "pnl": pnl, "r_multiple": r, "opened_at": 1000.0,
            "closed_at": 2000.0, "reason": "take_profit", "mae_r": -0.2, "mfe_r": r,
            "fees": 0.5, "entry_fee": 0.2,
        }
        row.update(kw)
        return row

    def test_metrics_math(self) -> None:
        import analytics

        trades = [
            self._trade(10.0, 2.0),
            self._trade(-5.0, -1.0, reason="stop_loss"),
            self._trade(20.0, 4.0),
            self._trade(-5.0, -1.0, reason="stop_loss"),
        ]
        m = analytics.metrics(trades, initial_equity=1000.0)
        self.assertEqual(m["trades"], 4)
        self.assertEqual(m["wins"], 2)
        self.assertEqual(m["losses"], 2)
        self.assertAlmostEqual(m["win_rate"], 0.5)
        self.assertAlmostEqual(m["gross_pnl"], 20.0)
        self.assertAlmostEqual(m["net_pnl"], 20.0 - 4 * 0.2)
        self.assertAlmostEqual(m["profit_factor"], (30 - 0.4) / (10 + 0.4), places=6)
        self.assertAlmostEqual(m["expectancy_money"], (20.0 - 0.8) / 4)
        self.assertAlmostEqual(m["expectancy_r"], 1.0)
        self.assertAlmostEqual(m["payoff"], ((30 - 0.4) / 2) / ((10 + 0.4) / 2))
        self.assertEqual(m["max_win_streak"], 1)
        self.assertEqual(m["max_loss_streak"], 1)
        self.assertGreater(m["max_drawdown"], 0.0)

    def test_empty_metrics_are_safe(self) -> None:
        import analytics

        m = analytics.metrics([])
        self.assertEqual(m["trades"], 0)
        self.assertEqual(m["profit_factor"], 0.0)
        self.assertEqual(m["sharpe_trade"], 0.0)

    def test_report_groups_and_exports(self) -> None:
        import analytics

        trades = [self._trade(10.0, 2.0), self._trade(-4.0, -1.0, symbol="ETH/USDT")]
        orders = [{"ts": 1500.0, "symbol": "BTC/USDT", "side": "BUY", "type": "LIMIT",
                   "qty": 0.5, "avg_price": 100.0, "status": "FILLED", "fee": 0.1,
                   "slippage_bps": 1.2}]
        report = analytics.build_report(trades, orders, initial_equity=1000.0,
                                        meta={"symbol": "BTC/USDT"})
        self.assertEqual(len(report["by_symbol"]), 2)
        self.assertTrue(report["by_reason"])
        md = analytics.to_markdown(report)
        self.assertIn("# Laporan kinerja AEGIS-1", md)
        self.assertIn("Profit factor", md)
        self.assertIn("Daftar trade", md)
        csv_text = analytics.to_csv(report)
        self.assertTrue(csv_text.startswith("waktu_tutup"))
        self.assertEqual(len(csv_text.strip().splitlines()), 3)

    def test_trade_cap_and_excursion_tracking(self) -> None:
        from strategy import Signal

        risk = RiskManager(RiskConfig(risk_per_trade=0.01), 10_000.0,
                           step_size=0.00001, min_qty=0.00001, min_notional=1.0)
        signal = Signal(symbol="BTC/USDT", bar_ts=1.0, side="LONG", score=0.8,
                        entry=100.0, stop=95.0, take_profit=115.0, reward_risk=3.0,
                        regime="trending_up", atr=1.0)
        pos = risk.open_position(signal, qty=1.0, fill_price=100.0, fee=0.1)
        self.assertEqual(risk.trades_today, 1)
        pos.update_excursion(97.0)
        pos.update_excursion(106.0)
        self.assertAlmostEqual(pos.mae_r, -0.6, places=6)
        self.assertAlmostEqual(pos.mfe_r, 1.2, places=6)
        trade = risk.close_position(pos, 106.0, fee=0.2, reason="take_profit")
        self.assertAlmostEqual(trade.mae_r, -0.6, places=6)
        self.assertAlmostEqual(trade.mfe_r, 1.2, places=6)
        self.assertAlmostEqual(trade.fees, 0.3, places=6)
        self.assertAlmostEqual(trade.entry_fee, 0.1, places=6)


class TestIntelligence(unittest.TestCase):
    """Lapisan analisis: MTF, divergensi, pola, squeeze — tanpa look-ahead."""

    def _bars(self, n=40, step=60, start=None, price=100.0):
        if start is None:
            start = (1_700_000_000 // 240) * 240
        rows = []
        for i in range(n):
            rows.append({"ts": start + i * step, "open": price, "high": price + 1,
                         "low": price - 1, "close": price, "volume": 10.0})
            price += 0.5
        return pd.DataFrame(rows)

    def test_resample_drops_incomplete_bucket(self) -> None:
        from intelligence import resample_closed

        df = self._bars(n=10)
        htf = resample_closed(df, base_tf_seconds=60, factor=4)
        self.assertEqual(len(htf), 2)

    def test_resample_never_uses_last_partial_bar(self) -> None:
        from intelligence import resample_closed

        df = self._bars(n=8)
        htf = resample_closed(df, base_tf_seconds=60, factor=4)
        self.assertEqual(len(htf), 2)

    def test_htf_bias_direction(self) -> None:
        from intelligence import htf_bias

        up = htf_bias(self._bars(n=400), base_tf_seconds=60, factor=4, fast=10, slow=20)
        self.assertEqual(up.trend, "up")

    def _divergence_frame(self, kind: str) -> pd.DataFrame:
        n = 40
        idx = np.arange(n, dtype=float)
        bump = lambda center, height: height * np.exp(-((idx - center) / 2.0) ** 2)
        if kind == "bearish":
            high = 100.0 + bump(10, 15.0) + bump(30, 20.0)
            low = high - 5.0
            rsi = 50.0 + bump(10, 30.0) + bump(30, 20.0)
        else:
            low = 100.0 - bump(10, 20.0) - bump(30, 25.0)
            high = low + 5.0
            rsi = 50.0 - bump(10, 30.0) - bump(30, 20.0)
        return pd.DataFrame({"ts": idx, "open": high, "high": high, "low": low,
                             "close": high, "volume": [1.0] * n, "rsi": rsi})

    def test_rsi_divergence_bearish(self) -> None:
        from intelligence import rsi_divergence
        self.assertEqual(rsi_divergence(self._divergence_frame("bearish")), "bearish")

    def test_rsi_divergence_bullish(self) -> None:
        from intelligence import rsi_divergence
        self.assertEqual(rsi_divergence(self._divergence_frame("bullish")), "bullish")

    def test_candle_pattern_engulfing(self) -> None:
        from intelligence import candle_pattern
        df = pd.DataFrame([
            {"ts": 1, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1.0},
            {"ts": 2, "open": 101.0, "high": 101.2, "low": 98.5, "close": 99.0, "volume": 1.0},
            {"ts": 3, "open": 98.6, "high": 102.0, "low": 98.4, "close": 101.5, "volume": 1.0},
        ])
        self.assertEqual(candle_pattern(df), "bullish_engulfing")

    def test_squeeze_detects_compression(self) -> None:
        from intelligence import squeeze_state
        wide = pd.DataFrame({"close": [100 + 0.01 * i * i for i in range(120)]})
        squeezed = pd.DataFrame({"close": [100 + (i % 10) * 5 for i in range(80)] + [100 + i * 0.001 for i in range(40)]})
        self.assertTrue(squeeze_state(squeezed)[0])
        self.assertFalse(squeeze_state(wide)[0])

    def test_weights_sum_to_one_per_regime(self) -> None:
        from strategy import WEIGHTS, WEIGHTS_BY_REGIME
        self.assertAlmostEqual(sum(WEIGHTS.values()), 1.0, places=6)
        for regime, weights in WEIGHTS_BY_REGIME.items():
            self.assertAlmostEqual(sum(weights.values()), 1.0, places=6, msg=regime)

    def test_signal_exposes_analysis_and_no_lookahead(self) -> None:
        from strategy import evaluate
        df = make_df(n=400, trend=0.0006, noise=0.0025, seed=5)
        closed = df.iloc[:-1].reset_index(drop=True)
        sig = evaluate(closed, StrategyConfig(), RiskConfig(), "BTC/USDT", "15m")
        self.assertAlmostEqual(sig.bar_ts, float(closed["ts"].iloc[-1]), places=3)
        self.assertIn(sig.htf_trend, ("up", "down", "flat"))
        self.assertGreaterEqual(sig.atr_pct_rank, 0.0)
        self.assertLessEqual(sig.atr_pct_rank, 1.0)


class TestAdaptiveRisk(TempProject):
    def test_multiplier_stays_inside_bounds(self) -> None:
        rm = RiskManager(RiskConfig(), initial_equity=10_000.0)
        for score in (0.0, 0.5, 0.7, 1.0):
            for rank in (0.0, 0.5, 1.0):
                mult, notes = rm.adaptive_multiplier(score, rank)
                self.assertGreaterEqual(mult, rm.cfg.adaptive.min_mult - 1e-9)
                self.assertLessEqual(mult, rm.cfg.adaptive.max_mult + 1e-9)
                self.assertTrue(notes)

    def test_negative_expectancy_reduces_risk(self) -> None:
        from risk import ClosedTrade
        rm = RiskManager(RiskConfig(), initial_equity=10_000.0)
        base, _ = rm.adaptive_multiplier(0.85, 0.4)
        rm.closed = [ClosedTrade("BTC/USDT", "LONG", 100.0, 98.0, 1.0, -2.0, -1.0,
                                 1.0, 2.0, "stop_loss") for _ in range(5)]
        worse, _ = rm.adaptive_multiplier(0.85, 0.4)
        self.assertLess(worse, base)

    def test_sizing_scales_with_multiplier_when_risk_binds(self) -> None:
        rm = RiskManager(RiskConfig(risk_per_trade=0.01, max_total_exposure=0.9, max_open_positions=1),
                         initial_equity=10_000.0, step_size=0.00001, min_qty=0.00001, min_notional=1.0)
        full = rm.position_size(entry=1000.0, stop=950.0)
        half = rm.position_size(entry=1000.0, stop=950.0, risk_multiplier=0.5)
        self.assertAlmostEqual(full.qty, 2.0, places=6)
        self.assertAlmostEqual(half.qty, 1.0, places=6)

    def test_directional_exposure_cap_blocks_second_position(self) -> None:
        from strategy import Signal
        cfg = RiskConfig(directional_exposure_cap=0.10, max_total_exposure=0.9,
                         max_open_positions=3)
        rm = RiskManager(cfg, initial_equity=10_000.0, step_size=0.00001,
                         min_qty=0.00001, min_notional=1.0)
        sig = Signal("BTC/USDT", 1.0, "LONG", 0.9, 100.0, 95.0, 110.0, 2.0, "trending_up", 1.0)
        rm.open_position(sig, qty=15.0, fill_price=100.0, fee=0.0)
        snap = self.snapshot(make_df())
        snap = MarketSnapshot(symbol="BTC/USDT", timeframe="15m", candles=snap.candles,
                              best_bid=99.9, best_ask=100.1, last_price=100.0,
                              ts=snap.ts, source="test")
        checks = {c.name: c for c in rm.pre_trade_check(
            sig, snap, Mode.PAPER, gate_passed=True, pending_orders=0)}
        self.assertFalse(checks["11_exposure_arah"].ok)

    def test_time_stop_closes_stale_trade(self) -> None:
        import time as _time
        from strategy import Signal
        rm = RiskManager(RiskConfig(max_bars_in_trade=10), initial_equity=10_000.0,
                         step_size=0.00001, min_qty=0.00001, min_notional=1.0)
        sig = Signal("BTC/USDT", 1.0, "LONG", 0.9, 100.0, 95.0, 120.0, 4.0, "trending_up", 1.0)
        pos = rm.open_position(sig, qty=1.0, fill_price=100.0, fee=0.0)
        pos.opened_at = _time.time() - 12 * 900
        plan = rm.plan_management(pos, 100.5, atr_value=1.0, tf_seconds=900)
        self.assertEqual(plan["action"], "time_stop")

    def test_adaptive_risk_can_be_disabled(self) -> None:
        from config import AdaptiveRisk
        rm = RiskManager(RiskConfig(adaptive=AdaptiveRisk(enabled=False)), initial_equity=10_000.0)
        mult, notes = rm.adaptive_multiplier(0.2, 1.0)
        self.assertEqual(mult, 1.0)
        self.assertTrue(notes)


# Remaining tests continue unchanged from baseline.



class TestOfflineResearch(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = [{"ts": float(i), "r_multiple": (0.5 if i % 3 else -1.0)}
                     for i in range(20)]

    def test_walk_forward_is_chronological_and_out_of_sample(self) -> None:
        from research import walk_forward
        report = walk_forward(self.rows, train_size=8, test_size=4, step_size=4)
        self.assertEqual(report["window_count"], 3)
        first = report["windows"][0]
        self.assertLess(first["train_end"], first["test_start"])
        self.assertEqual(first["test"]["samples"], 4)
        self.assertEqual(report["test_summary"]["samples"], 12)

    def test_walk_forward_rejects_duplicate_timestamps(self) -> None:
        from research import walk_forward
        rows = self.rows + [{"ts": 0.0, "r_multiple": 1.0}]
        with self.assertRaises(ValueError):
            walk_forward(rows, train_size=4, test_size=2)

    def test_walk_forward_handles_insufficient_history(self) -> None:
        from research import walk_forward
        report = walk_forward(self.rows[:4], train_size=4, test_size=2)
        self.assertEqual(report["window_count"], 0)
        self.assertEqual(report["test_summary"]["samples"], 0)

    def test_monte_carlo_is_reproducible_and_bounded(self) -> None:
        from research import monte_carlo_risk
        a = monte_carlo_risk(self.rows, simulations=200, seed=7)
        b = monte_carlo_risk(self.rows, simulations=200, seed=7)
        self.assertEqual(a, b)
        self.assertGreaterEqual(a["max_drawdown"]["p95"], 0.0)
        self.assertLessEqual(a["probability_below_start"], 1.0)

    def test_monte_carlo_rejects_unsafe_parameters(self) -> None:
        from research import monte_carlo_risk
        with self.assertRaises(ValueError):
            monte_carlo_risk(self.rows, simulations=10)
        with self.assertRaises(ValueError):
            monte_carlo_risk(self.rows, simulations=100, risk_fraction=0.10)

    def test_research_rejects_non_finite_results(self) -> None:
        from research import walk_forward
        rows = [{"ts": 1.0, "r_multiple": float("nan")}]
        with self.assertRaises(ValueError):
            walk_forward(rows, train_size=2, test_size=1)

    def test_overlapping_walk_forward_windows_do_not_double_count_test_rows(self) -> None:
        from research import walk_forward
        report = walk_forward(self.rows, train_size=6, test_size=4, step_size=2)
        unique_test = {row["ts"] for window in report["windows"]
                       for row in self.rows
                       if window["test_start"] <= row["ts"] <= window["test_end"]}
        self.assertEqual(report["test_summary"]["samples"], len(unique_test))

    def test_profit_factor_is_json_safe_when_no_losses(self) -> None:
        from research import walk_forward
        rows = [{"ts": float(i), "r_multiple": 1.0} for i in range(6)]
        report = walk_forward(rows, train_size=3, test_size=3)
        self.assertIsNone(report["test_summary"]["profit_factor_r"])


class TestPortfolioRiskAnalysis(unittest.TestCase):
    def test_correlation_matrix_detects_perfect_relationships(self) -> None:
        from portfolio import correlation_matrix
        matrix = correlation_matrix({"A": [1, 2, 3], "B": [2, 4, 6], "C": [3, 2, 1]})
        self.assertAlmostEqual(matrix["A"]["B"], 1.0)
        self.assertAlmostEqual(matrix["A"]["C"], -1.0)
        self.assertIsNone(matrix["A"].get("missing"))

    def test_correlation_rejects_misaligned_or_non_finite_data(self) -> None:
        from portfolio import correlation_matrix
        with self.assertRaises(ValueError):
            correlation_matrix({"A": [1, 2], "B": [1]})
        with self.assertRaises(ValueError):
            correlation_matrix({"A": [1, float("inf")]})

    def test_stress_test_aggregates_weighted_shocks(self) -> None:
        from portfolio import stress_test
        report = stress_test(
            {"BTC": 0.6, "ETH": 0.3, "USDT": 0.1},
            {"risk_off": {"BTC": -0.2, "ETH": -0.4}},
        )
        self.assertAlmostEqual(report["risk_off"]["portfolio_return"], -0.24)
        self.assertAlmostEqual(report["risk_off"]["portfolio_return_pct"], -24.0)

    def test_stress_test_rejects_invalid_weights_and_shocks(self) -> None:
        from portfolio import stress_test
        with self.assertRaises(ValueError):
            stress_test({"BTC": 1.2}, {"crash": {"BTC": -0.5}})
        with self.assertRaises(ValueError):
            stress_test({"BTC": 0.5}, {"impossible": {"BTC": -1.2}})


if __name__ == "__main__":
    unittest.main(verbosity=2)
