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
    """Encuentra el directorio live que contiene los perfiles de configuración."""
    for parent in (PACKAGE_DIR, *PACKAGE_DIR.parents):
        if (parent / "feed.py").is_file():
            return parent
        if (parent / "live" / "feed.py").is_file():
            return parent / "live"
    return PACKAGE_DIR.parent


APP_DIR = _find_app_dir()

from strategy.config import Config

logging.basicConfig(
    level=getattr(logging, os.getenv("BOT_LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(threadName)s: %(message)s",
)
logger = logging.getLogger("bot1")
STOP_EVENT = threading.Event()

# Defaults operativos; el perfil activo puede sobrescribirlos con claves BOT_.
DEFAULT_SYMBOL = "BTCUSDT"
DEFAULT_INTERVAL = "1m"
DEFAULT_MODE = "live"
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
    return os.getenv(_bot_env_name(name), default)


def env_int(name: str, default: int) -> int:
    raw = os.getenv(_bot_env_name(name))
    return default if raw is None or raw == "" else int(raw)


def env_float(name: str, default: float) -> float:
    raw = os.getenv(_bot_env_name(name))
    return default if raw is None or raw == "" else float(raw)


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(_bot_env_name(name))
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _bot_env_name(name: str) -> str:
    """Añade el prefijo del bot sin duplicarlo en claves ya prefijadas."""
    return name if name.startswith("BOT_") else f"BOT_{name}"


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


def build_strategy_config() -> Config:
    """Usa exclusivamente los valores hardcodeados de strategy.Config."""
    return Config()
