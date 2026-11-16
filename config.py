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
    )
    execution = ExecutionConfig(
        slippage_bps=_env_float("SLIPPAGE_BPS", ExecutionConfig.slippage_bps),
        step_size=_env_float("STEP_SIZE", ExecutionConfig.step_size),
        min_notional=_env_float("MIN_NOTIONAL", ExecutionConfig.min_notional),
    )
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
