"""Configuración, entorno y utilidades compartidas de la aplicación live."""

from __future__ import annotations

import logging
import math
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

PACKAGE_DIR = Path(__file__).resolve().parent


def _find_app_dir() -> Path:
    """Encuentra el directorio live que contiene el feed y su fichero .env."""
    for parent in (PACKAGE_DIR, *PACKAGE_DIR.parents):
        if (parent / "feed.py").is_file():
            return parent
        if (parent / "live" / "feed.py").is_file():
            return parent / "live"
    return PACKAGE_DIR.parent


APP_DIR = _find_app_dir()

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv(APP_DIR / ".env", override=False)
    load_dotenv(override=False)

from strategy.config import Config

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(threadName)s: %(message)s",
)
logger = logging.getLogger("bot1")
STOP_EVENT = threading.Event()

# Defaults operativos; live/.env puede sobrescribirlos.
DEFAULT_SYMBOL = "BTCUSDT"
DEFAULT_INTERVAL = "1m"
DEFAULT_FEED_DB = "data/feed.db"
DEFAULT_SIGNAL_DB = "data/signal.db"
DEFAULT_BACKTEST_REPORT_DIR = "bot/data/backtest_out"
DEFAULT_POLL_SECONDS = 30.0
DEFAULT_RUN_ONCE = False
DEFAULT_LIVE_ONLY = True
DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8080


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso() -> str:
    return utc_now().isoformat()


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def ts_ms(moment: pd.Timestamp) -> int:
    return int(moment.timestamp() * 1000)


def parse_live_since(value: str) -> int:
    """Convierte epoch en ms o una fecha ISO 8601 a epoch en ms."""
    text = value.strip()
    if text.lstrip("-").isdigit():
        return int(text)
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return ts_ms(moment)


def opt_float(value) -> float | None:
    """Convierte a float JSON-safe; None si no es finito."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def env_str(name: str, default: str) -> str:
    return os.getenv(name, default)


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None or raw == "" else int(raw)


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw is None or raw == "" else float(raw)


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def resolve_path(value: str) -> Path:
    """Resuelve rutas relativas contra el directorio live."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else (APP_DIR / path)


def touch_heartbeat() -> None:
    """Registra actividad para el healthcheck del contenedor (best-effort)."""
    path = os.getenv("BOT_HEARTBEAT")
    if not path:
        return
    try:
        Path(path).write_text(utc_iso(), encoding="utf-8")
    except OSError:
        logger.debug("No se pudo escribir el heartbeat en %s", path)


# Campo de Config -> variable de entorno y lector. Los defaults vienen de
# Config para mantener idéntico el comportamiento del backtest.
_CONFIG_ENV = {
    "ema_fast": ("STRATEGY_EMA_FAST", env_int),
    "ema_slow": ("STRATEGY_EMA_SLOW", env_int),
    "timeframe": ("STRATEGY_TIMEFRAME", env_str),
    "allow_long": ("STRATEGY_ALLOW_LONG", env_bool),
    "allow_short": ("STRATEGY_ALLOW_SHORT", env_bool),
    "confirm_bars": ("STRATEGY_CONFIRM_BARS", env_int),
    "entry_adx_enabled": ("STRATEGY_ENTRY_ADX", env_bool),
    "entry_adx_len": ("STRATEGY_ENTRY_ADX_LEN", env_int),
    "entry_adx_min": ("STRATEGY_ENTRY_ADX_MIN", env_float),
    "entry_adx_timeframe": ("STRATEGY_ENTRY_ADX_TIMEFRAME", env_str),
    "entry_atr_enabled": ("STRATEGY_ENTRY_ATR", env_bool),
    "entry_atr_len": ("STRATEGY_ENTRY_ATR_LEN", env_int),
    "entry_atr_lookback": ("STRATEGY_ENTRY_ATR_LOOKBACK", env_int),
    "entry_atr_min_ratio": ("STRATEGY_ENTRY_ATR_MIN_RATIO", env_float),
    "entry_atr_max_ratio": ("STRATEGY_ENTRY_ATR_MAX_RATIO", env_float),
    "entry_price_ema_filter_enabled": ("STRATEGY_ENTRY_PRICE_EMA_FILTER", env_bool),
    "entry_mfi_risk_scaling_enabled": ("STRATEGY_ENTRY_MFI_RISK_SCALING", env_bool),
    "entry_mfi_len": ("STRATEGY_ENTRY_MFI_LEN", env_int),
    "entry_mfi_risk_base": ("STRATEGY_ENTRY_MFI_RISK_BASE", env_float),
    "entry_mfi_risk_spread_scale": ("STRATEGY_ENTRY_MFI_RISK_SPREAD_SCALE", env_float),
    "entry_mfi_risk_min": ("STRATEGY_ENTRY_MFI_RISK_MIN", env_float),
    "entry_mfi_risk_max": ("STRATEGY_ENTRY_MFI_RISK_MAX", env_float),
    "entry_er_risk_scaling_enabled": ("STRATEGY_ENTRY_ER_RISK_SCALING", env_bool),
    "entry_er_len": ("STRATEGY_ENTRY_ER_LEN", env_int),
    "entry_er_risk_base": ("STRATEGY_ENTRY_ER_RISK_BASE", env_float),
    "entry_er_risk_slope": ("STRATEGY_ENTRY_ER_RISK_SLOPE", env_float),
    "entry_er_risk_min": ("STRATEGY_ENTRY_ER_RISK_MIN", env_float),
    "entry_er_risk_max": ("STRATEGY_ENTRY_ER_RISK_MAX", env_float),
    "initial_capital": ("STRATEGY_INITIAL_CAPITAL", env_float),
    "commission_bps": ("STRATEGY_COMMISSION_BPS", env_float),
    "slippage_bps": ("STRATEGY_SLIPPAGE_BPS", env_float),
    "risk_pct": ("STRATEGY_RISK_PCT", env_float),
    "max_leverage": ("STRATEGY_MAX_LEVERAGE", env_float),
    "stop_loss_pct": ("STRATEGY_STOP_LOSS_PCT", env_float),
    "partial_tp_enabled": ("STRATEGY_PARTIAL_TP", env_bool),
    "partial_tp_r": ("STRATEGY_PARTIAL_TP_R", env_float),
    "partial_tp_pct": ("STRATEGY_PARTIAL_TP_PCT", env_float),
    "partial_tp2_r": ("STRATEGY_PARTIAL_TP2_R", env_float),
    "partial_tp2_pct": ("STRATEGY_PARTIAL_TP2_PCT", env_float),
    "partial_tp3_r": ("STRATEGY_PARTIAL_TP3_R", env_float),
    "partial_tp3_pct": ("STRATEGY_PARTIAL_TP3_PCT", env_float),
    "partial_tp4_r": ("STRATEGY_PARTIAL_TP4_R", env_float),
    "partial_tp4_pct": ("STRATEGY_PARTIAL_TP4_PCT", env_float),
    "partial_tp5_r": ("STRATEGY_PARTIAL_TP5_R", env_float),
    "partial_tp5_pct": ("STRATEGY_PARTIAL_TP5_PCT", env_float),
    "partial_tp6_r": ("STRATEGY_PARTIAL_TP6_R", env_float),
    "partial_tp6_pct": ("STRATEGY_PARTIAL_TP6_PCT", env_float),
    "partial_tp7_r": ("STRATEGY_PARTIAL_TP7_R", env_float),
    "partial_tp7_pct": ("STRATEGY_PARTIAL_TP7_PCT", env_float),
    "tp3_ema_slow_penetration_pct": (
        "STRATEGY_TP3_EMA_SLOW_PENETRATION_PCT", env_float
    ),
    "move_stop_to_breakeven": ("STRATEGY_MOVE_STOP_TO_BREAKEVEN", env_bool),
    "adx_exit_enabled": ("STRATEGY_ADX_EXIT", env_bool),
    "adx_timeframe": ("STRATEGY_ADX_TIMEFRAME", env_str),
    "adx_dilen": ("STRATEGY_ADX_DILEN", env_int),
    "adx_adxlen": ("STRATEGY_ADX_ADXLEN", env_int),
    "adx_key_level": ("STRATEGY_ADX_KEY_LEVEL", env_float),
    "adx_exit_pct": ("STRATEGY_ADX_EXIT_PCT", env_float),
    "adx_exit_range": ("STRATEGY_ADX_EXIT_RANGE", env_int),
    "ema_weakening_exit_enabled": ("STRATEGY_EMA_WEAKENING_EXIT", env_bool),
    "ema_weakening_timeframe": ("STRATEGY_EMA_WEAKENING_TIMEFRAME", env_str),
    "ema_weakening_fast": ("STRATEGY_EMA_WEAKENING_FAST", env_int),
    "ema_weakening_slow": ("STRATEGY_EMA_WEAKENING_SLOW", env_int),
    "ema_weakening_gap_pct": ("STRATEGY_EMA_WEAKENING_GAP_PCT", env_float),
    "ema_weakening_exit_pct": ("STRATEGY_EMA_WEAKENING_EXIT_PCT", env_float),
    "rsi_divergence_exit_enabled": ("STRATEGY_RSI_DIVERGENCE_EXIT", env_bool),
    "rsi_divergence_exit_pct": ("STRATEGY_RSI_DIVERGENCE_EXIT_PCT", env_float),
    "rsi_divergence_timeframe": ("STRATEGY_RSI_DIVERGENCE_TIMEFRAME", env_str),
    "rsi_divergence_len": ("STRATEGY_RSI_DIVERGENCE_LEN", env_int),
    "rsi_divergence_pivot": ("STRATEGY_RSI_DIVERGENCE_PIVOT", env_int),
    "rsi_divergence_lookback": ("STRATEGY_RSI_DIVERGENCE_LOOKBACK", env_int),
    "ema55_touch_exit_enabled": ("STRATEGY_EMA55_TOUCH_EXIT", env_bool),
    "ema55_touch_tolerance_pct": ("STRATEGY_EMA55_TOUCH_TOLERANCE_PCT", env_float),
    "ema55_touch_lookback_bars": ("STRATEGY_EMA55_TOUCH_LOOKBACK_BARS", env_int),
    "ema55_touch_min_touches": ("STRATEGY_EMA55_TOUCH_MIN_TOUCHES", env_int),
    "ema55_touch_consecutive": ("STRATEGY_EMA55_TOUCH_CONSECUTIVE", env_int),
    "ema55_touch_breakout_buffer_pct": (
        "STRATEGY_EMA55_TOUCH_BREAKOUT_BUFFER_PCT", env_float
    ),
    "ema55_touch_exit_loss_pct": ("STRATEGY_EMA55_TOUCH_EXIT_LOSS_PCT", env_float),
    "ema55_touch_short_exit_loss_pct": (
        "STRATEGY_EMA55_TOUCH_SHORT_EXIT_LOSS_PCT", env_float
    ),
    "volume_exit_enabled": ("STRATEGY_VOLUME_EXIT", env_bool),
    "volume_exit_bars": ("STRATEGY_VOLUME_EXIT_BARS", env_int),
    "volume_exit_pct": ("STRATEGY_VOLUME_EXIT_PCT", env_float),
    "volume_exit_min_drop_pct": ("STRATEGY_VOLUME_EXIT_MIN_DROP_PCT", env_float),
}


def build_strategy_config() -> Config:
    defaults = Config()
    values = {
        field: read(variable, getattr(defaults, field))
        for field, (variable, read) in _CONFIG_ENV.items()
    }
    return Config(**values)
