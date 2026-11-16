"""AEGIS-1 — Adaptive Execution & Guarded Investment System.

Configuration layer.

Aturan keras yang berlaku di modul ini:

* Tidak ada secret yang dibaca ke dalam atribut yang bisa dicetak/di-log.
  Kredensial hanya dicek keberadaannya (``credential_presence``), nilainya
  tidak pernah dibaca ke variabel yang bisa bocor.
* ``LIVE_MODE`` ada sebagai flag konfigurasi, tetapi eksekusi live tidak
  diimplementasikan di paket ini. ``execution.build_broker()`` menolak
  (fail closed) bila flag itu true. Lihat README bagian "Batas paket".
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"


class Mode(str, Enum):
    """Mode operasi bot. Ditampilkan apa adanya di dashboard."""

    PAPER = "PAPER"
    SAFE = "SAFE"
    PAUSED = "PAUSED"
    KILL = "KILL_SWITCH"


class Regime(str, Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGING = "ranging"
    HIGH_VOLATILITY = "high_volatility"
    LOW_LIQUIDITY = "low_liquidity"
    UNKNOWN = "unknown"


def _env(key: str, default: str = "") -> str:
    return os.environ.get(f"AEGIS_{key}", default).strip()


def _env_float(key: str, default: float) -> float:
    raw = _env(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = _env(key).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "y"}


@dataclass(frozen=True)
class StrategyConfig:
    """Parameter strategi multi-konfirmasi."""

    ema_fast: int = 50
    ema_slow: int = 200
    rsi_period: int = 14
    rsi_overbought: float = 78.0          # ekstrem, bukan 70
    rsi_oversold: float = 22.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    atr_period: int = 14
    roc_period: int = 10
    volume_lookback: int = 20
    swing_lookback: int = 5
    min_bars: int = 260                   # cukup untuk EMA200 + buffer
    min_confluence: float = 0.70          # skor 0..1, di bawah ini tidak entry
    atr_percentile_window: int = 100
    high_vol_percentile: float = 0.90     # ATR di atas persentil ini = volatil
    low_liquidity_ratio: float = 0.55     # rel-vol di bawah ini = sepi
    # --- lapisan analisis tambahan ---
    mtf_enabled: bool = True              # konfirmasi timeframe lebih tinggi
    mtf_factor: int = 4                   # 15m -> 1h, 1h -> 4h
    mtf_fast: int = 20
    mtf_slow: int = 50
    divergence_enabled: bool = True       # divergensi RSI sebagai veto
    divergence_lookback: int = 60
    pattern_enabled: bool = True          # engulfing / hammer / shooting star
    squeeze_enabled: bool = True          # bonus saat breakout dari kompresi


@dataclass(frozen=True)
class AdaptiveRisk:
    """Penyesuaian risiko per trade berdasarkan kualitas sinyal dan kondisi.

    Semua pengali dikalikan lalu dijepit di ``[min_mult, max_mult]``. Tidak ada
    jalur yang boleh melewati batas keras ``max_total_exposure`` atau
    ``max_drawdown`` — pengali ini hanya memperkecil atau menormalkan.
    """

    enabled: bool = True
    min_mult: float = 0.25
    max_mult: float = 1.50
    quality_floor: float = 0.60           # skor di bawah ini tidak menambah risiko
    quality_ceiling: float = 0.90
    vol_penalty: float = 0.35             # ATR persentil 1.0 -> risiko -35%
    dd_throttle_start: float = 0.50       # mulai kurangi saat DD 50% dari batas
    dd_throttle_max: float = 0.50         # pengurangan maksimum dari drawdown
    expectancy_lookback: int = 20
    negative_expectancy_mult: float = 0.50


@dataclass(frozen=True)
class RiskConfig:
    """Guardrail risiko. Semua angka fraksi dari equity kecuali disebut lain."""

    risk_per_trade: float = 0.01
    max_open_positions: int = 3
    max_total_exposure: float = 0.20
    max_daily_loss: float = 0.03
    max_drawdown: float = 0.10
    min_reward_risk: float = 2.0
    atr_stop_mult: float = 1.8
    structure_buffer_atr: float = 0.25
    partial_take_profit_r: tuple[float, ...] = (1.0, 2.0)
    partial_take_profit_fraction: float = 0.5
    trailing_after_r: float = 1.0
    trailing_atr_mult: float = 1.5
    max_leverage: float = 1.0
    max_spread_bps: float = 8.0
    min_relative_volume: float = 1.0
    cooldown_bars_after_loss: int = 3      # anti revenge-trading
    max_orders_per_bar: int = 1            # anti duplicate order
    # --- manajemen risiko tambahan ---
    directional_exposure_cap: float = 0.12  # maks notional satu arah / ekuitas
    max_bars_in_trade: int = 0              # 0 = tanpa batas; time stop
    breakeven_after_r: float = 1.0          # stop dipindah ke entry setelah R ini
    adaptive: AdaptiveRisk = field(default_factory=AdaptiveRisk)


@dataclass(frozen=True)
class ExecutionConfig:
    """Parameter eksekusi & biaya."""

    maker_fee: float = 0.0002
    taker_fee: float = 0.0005
    slippage_bps: float = 1.5
    order_timeout_s: float = 20.0
    prefer_limit_orders: bool = True
    allow_market_orders: bool = True       # hanya untuk SL darurat
    tick_size: float = 0.01
    step_size: float = 0.00001
    min_qty: float = 0.00001
    min_notional: float = 5.0
    contract_size: float = 1.0
    # --- metadata instrumen (penting untuk forex/saham) ---
    pip_size: float = 0.0001            # 1 pip EURUSD = 0.0001
    price_decimals: int = 2
    spread_bps: float = 1.0             # dipakai bila data tidak membawa bid/ask
    use_data_spread: bool = True        # pakai bid/ask dari data bila tersedia


@dataclass(frozen=True)
class SessionConfig:
    """Filter waktu & batas aktivitas harian (anti-overtrading).

    ``windows`` berisi pasangan menit UTC ``(mulai, selesai)``, selesai
    eksklusif. Jendela yang melewati tengah malam (mulai > selesai) dibaca
    sebagai dua rentang yang menyambung.
    """

    enabled: bool = False
    windows: tuple[tuple[int, int], ...] = ()
    skip_weekend: bool = False
    max_trades_per_day: int = 0          # 0 = tanpa batas

    def is_open(self, ts: float | None = None) -> bool:
        stamp = time.time() if ts is None else float(ts)
        tm = time.gmtime(stamp)
        if self.skip_weekend and tm.tm_wday >= 5:
            return False
        if not self.enabled or not self.windows:
            return True
        minute = tm.tm_hour * 60 + tm.tm_min
        for start, end in self.windows:
            if start <= end:
                if start <= minute < end:
                    return True
            elif minute >= start or minute < end:
                return True
        return False

    def describe(self) -> str:
        if not self.enabled and not self.windows and not self.skip_weekend:
            return "24/7"
        parts: list[str] = []
        for start, end in self.windows:
            parts.append(f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}")
        text = ", ".join(parts) if parts else "setiap jam"
        if self.skip_weekend:
            text += " (Senin-Jumat)"
        if self.max_trades_per_day:
            text += f", maks {self.max_trades_per_day} trade/hari"
        return text


def parse_windows(raw: str) -> tuple[tuple[int, int], ...]:
    """``"07:00-16:00,13:30-20:00"`` -> ``((420, 960), (810, 1200))``."""
    windows: list[tuple[int, int]] = []
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk or "-" not in chunk:
            continue
        left, right = chunk.split("-", 1)
        try:
            sh, sm = (int(x) for x in left.strip().split(":"))
            eh, em = (int(x) for x in right.strip().split(":"))
        except (ValueError, TypeError):
            continue
        start = max(0, min(24 * 60, sh * 60 + sm))
        end = max(0, min(24 * 60, eh * 60 + em))
        if start == end:
            continue
        windows.append((start, end))
    return tuple(windows)


@dataclass(frozen=True)
class Settings:
    """Konfigurasi runtime lengkap."""

    symbol: str = "BTC/USDT"
    timeframe: str = "15m"
    market: str = "crypto"
    exchange_id: str = "binance"
    initial_equity: float = 10_000.0
    quote_currency: str = "USDT"
    mode: Mode = Mode.PAPER
    live_mode: bool = False
    db_path: Path = DEFAULT_DATA_DIR / "aegis1.sqlite3"
    jsonl_path: Path = DEFAULT_DATA_DIR / "aegis1.jsonl"
    kill_switch_file: Path = DEFAULT_DATA_DIR / "KILL_SWITCH"
    loop_interval_s: float = 5.0
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8787
    dashboard_enabled: bool = True
    paper_seed: int = 7
    offline: bool = False
    replay: bool = False
    replay_bars: int = 1
    volume_fallback: float | None = None   # isi volume bila CSV tidak punya kolom volume
    session: SessionConfig = field(default_factory=SessionConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)


def _load_dotenv() -> None:
    """Muat ``.env`` bila python-dotenv tersedia. Tidak wajib."""
    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:
        return
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=False)


def load_settings() -> Settings:
    """Muat konfigurasi dari environment (prefix ``AEGIS_``) + default aman."""

    _load_dotenv()
    live_requested = _env_bool("LIVE_MODE", False)
    strategy = StrategyConfig(
        ema_fast=_env_int("EMA_FAST", StrategyConfig.ema_fast),
        ema_slow=_env_int("EMA_SLOW", StrategyConfig.ema_slow),
        min_confluence=_env_float("MIN_CONFLUENCE", StrategyConfig.min_confluence),
        min_bars=_env_int("MIN_BARS", StrategyConfig.min_bars),
        mtf_enabled=_env_bool("MTF_ENABLED", StrategyConfig.mtf_enabled),
        mtf_factor=max(2, _env_int("MTF_FACTOR", StrategyConfig.mtf_factor)),
        divergence_enabled=_env_bool("DIVERGENCE_ENABLED", StrategyConfig.divergence_enabled),
        pattern_enabled=_env_bool("PATTERN_ENABLED", StrategyConfig.pattern_enabled),
        squeeze_enabled=_env_bool("SQUEEZE_ENABLED", StrategyConfig.squeeze_enabled),
    )
    risk = RiskConfig(
        risk_per_trade=_env_float("RISK_PER_TRADE", RiskConfig.risk_per_trade),
        max_open_positions=_env_int("MAX_OPEN_POSITIONS", RiskConfig.max_open_positions),
        max_total_exposure=_env_float("MAX_TOTAL_EXPOSURE", RiskConfig.max_total_exposure),
        max_daily_loss=_env_float("MAX_DAILY_LOSS", RiskConfig.max_daily_loss),
        max_drawdown=_env_float("MAX_DRAWDOWN", RiskConfig.max_drawdown),
        min_reward_risk=_env_float("MIN_REWARD_RISK", RiskConfig.min_reward_risk),
        max_leverage=_env_float("MAX_LEVERAGE", RiskConfig.max_leverage),
        max_spread_bps=_env_float("MAX_SPREAD_BPS", RiskConfig.max_spread_bps),
        directional_exposure_cap=_env_float(
            "DIRECTIONAL_CAP", RiskConfig.directional_exposure_cap),
        max_bars_in_trade=_env_int("MAX_BARS_IN_TRADE", RiskConfig.max_bars_in_trade),
        breakeven_after_r=_env_float("BREAKEVEN_AFTER_R", RiskConfig.breakeven_after_r),
        adaptive=AdaptiveRisk(
            enabled=_env_bool("ADAPTIVE_RISK", True),
            min_mult=_env_float("RISK_MIN_MULT", AdaptiveRisk.min_mult),
            max_mult=_env_float("RISK_MAX_MULT", AdaptiveRisk.max_mult),
            vol_penalty=_env_float("RISK_VOL_PENALTY", AdaptiveRisk.vol_penalty),
        ),
    )
    execution = ExecutionConfig(
        slippage_bps=_env_float("SLIPPAGE_BPS", ExecutionConfig.slippage_bps),
        step_size=_env_float("STEP_SIZE", ExecutionConfig.step_size),
        min_notional=_env_float("MIN_NOTIONAL", ExecutionConfig.min_notional),
        min_qty=_env_float("MIN_QTY", ExecutionConfig.min_qty),
        tick_size=_env_float("TICK_SIZE", ExecutionConfig.tick_size),
        pip_size=_env_float("PIP_SIZE", ExecutionConfig.pip_size),
        price_decimals=_env_int("PRICE_DECIMALS", ExecutionConfig.price_decimals),
        spread_bps=_env_float("SPREAD_BPS", ExecutionConfig.spread_bps),
        use_data_spread=_env_bool("USE_DATA_SPREAD", ExecutionConfig.use_data_spread),
        maker_fee=_env_float("MAKER_FEE", ExecutionConfig.maker_fee),
        taker_fee=_env_float("TAKER_FEE", ExecutionConfig.taker_fee),
    )
    windows = parse_windows(_env("SESSIONS"))
    session = SessionConfig(
        enabled=bool(windows) or _env_bool("SESSION_FILTER", False),
        windows=windows,
        skip_weekend=_env_bool("SKIP_WEEKEND", False),
        max_trades_per_day=_env_int("MAX_TRADES_PER_DAY", 0),
    )
    raw_volume = _env("VOLUME_FALLBACK")
    volume_fallback: float | None = None
    if raw_volume:
        try:
            volume_fallback = float(raw_volume)
        except ValueError:
            volume_fallback = None
    # Mode default selalu non-live. LIVE harus diminta eksplisit lewat env.
    mode = Mode.PAPER
    return Settings(
        symbol=_env("SYMBOL", Settings.symbol),
        timeframe=_env("TIMEFRAME", Settings.timeframe),
        market=_env("MARKET", Settings.market),
        exchange_id=_env("EXCHANGE", Settings.exchange_id),
        initial_equity=_env_float("INITIAL_EQUITY", Settings.initial_equity),
        quote_currency=_env("QUOTE_CURRENCY", Settings.quote_currency),
        mode=mode,
        live_mode=live_requested,
        db_path=Path(_env("DB_PATH", str(Settings.db_path))),
        jsonl_path=Path(_env("JSONL_PATH", str(Settings.jsonl_path))),
        kill_switch_file=Path(_env("KILL_SWITCH_FILE", str(Settings.kill_switch_file))),
        loop_interval_s=_env_float("LOOP_INTERVAL_S", Settings.loop_interval_s),
        dashboard_host=_env("DASHBOARD_HOST", Settings.dashboard_host),
        dashboard_port=_env_int("DASHBOARD_PORT", Settings.dashboard_port),
        dashboard_enabled=_env_bool("DASHBOARD_ENABLED", True),
        offline=_env_bool("OFFLINE", False),
        replay=_env_bool("REPLAY", False),
        replay_bars=max(1, _env_int("REPLAY_BARS", 1)),
        volume_fallback=volume_fallback,
        session=session,
        strategy=strategy,
        risk=risk,
        execution=execution,
    )


CREDENTIAL_VARS = (
    "EXCHANGE_API_KEY",
    "EXCHANGE_API_SECRET",
    "EXCHANGE_API_PASSPHRASE",
)


def credential_presence() -> dict[str, bool]:
    """Laporkan variabel kredensial mana yang terisi — tanpa pernah membaca nilainya.

    Fungsi ini sengaja hanya mengembalikan boolean. Nilai key, secret, dan
    passphrase tidak pernah dimuat ke memori proses ini dan tidak pernah
    ditulis ke log, database, atau dashboard.
    """

    return {name: bool(os.environ.get(name, "").strip()) for name in CREDENTIAL_VARS}
