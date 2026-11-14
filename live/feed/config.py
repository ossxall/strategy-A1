"""Configuración del colector y utilidades de tiempo."""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(
        Path(__file__).resolve().parents[2] / ".env",
        override=False,
    )
except ImportError:
    pass

REST_BASE_URL = "https://fapi.binance.com"
WS_BASE_URL = "wss://fstream.binance.com/ws"

SYMBOL = os.getenv("FEED_SYMBOL", "BTCUSDT").upper()
INTERVAL = os.getenv("FEED_INTERVAL", "1m")
DB_PATH = os.getenv("FEED_DB_PATH", "data/feed.db")
START_DATE = os.getenv("FEED_START_DATE", "2026-11-01")

REST_TIMEOUT_CONNECT = float(os.getenv("FEED_REST_TIMEOUT_CONNECT", "5"))
REST_TIMEOUT_READ = float(os.getenv("FEED_REST_TIMEOUT_READ", "20"))
REST_PAGE_LIMIT = min(1500, max(1, int(os.getenv("FEED_REST_PAGE_LIMIT", "1500"))))
REST_REPAIR_INTERVAL = max(5, int(os.getenv("FEED_REST_REPAIR_INTERVAL", "60")))
GAP_SCAN_MINUTES = max(10, int(os.getenv("FEED_GAP_SCAN_MINUTES", "1440")))

WS_PING_INTERVAL = max(5, int(os.getenv("FEED_WS_PING_INTERVAL", "20")))
WS_PING_TIMEOUT = max(1, int(os.getenv("FEED_WS_PING_TIMEOUT", "10")))
WS_WATCHDOG_SECONDS = max(15, int(os.getenv("FEED_WS_WATCHDOG_SECONDS", "90")))
MAX_BACKOFF_SECONDS = max(5, int(os.getenv("FEED_MAX_BACKOFF_SECONDS", "60")))
# 0 (por defecto) = reintentar indefinidamente.
WS_MAX_RECOVERY_SECONDS = max(0, int(os.getenv("FEED_WS_MAX_RECOVERY_SECONDS", "0")))
WS_STABLE_SECONDS = 60

LOG_LEVEL = os.getenv("FEED_LOG_LEVEL", "INFO").upper()
STATUS_HOST = os.getenv("FEED_STATUS_HOST", "127.0.0.1")
STATUS_PORT = int(os.getenv("FEED_STATUS_PORT", "8787"))
STATUS_DB_PROBE_INTERVAL = max(
    1.0, float(os.getenv("FEED_STATUS_DB_PROBE_INTERVAL", "5"))
)
STATUS_DB_TIMEOUT = max(
    0.05, min(2.0, float(os.getenv("FEED_STATUS_DB_TIMEOUT", "0.2")))
)
STATUS_REQUEST_TIMEOUT = max(
    0.1, min(5.0, float(os.getenv("FEED_STATUS_REQUEST_TIMEOUT", "1")))
)
STATUS_MAX_CLIENTS = max(1, min(64, int(os.getenv("FEED_STATUS_MAX_CLIENTS", "8"))))

if not 1 <= STATUS_PORT <= 65535:
    raise ValueError("FEED_STATUS_PORT debe estar entre 1 y 65535")


def utc_now():
    return datetime.now(timezone.utc)


def utc_iso(dt=None):
    dt = dt or utc_now()
    return dt.astimezone(timezone.utc).isoformat()


def interval_milliseconds(interval):
    """Convierte intervalos Binance habituales a milisegundos."""
    unit = interval[-1]
    amount = int(interval[:-1])
    units = {"m": 60000, "h": 3600000, "d": 86400000, "w": 604800000}
    if unit not in units or amount <= 0:
        raise ValueError(f"Intervalo no soportado: {interval}")
    return amount * units[unit]


def parse_start_date(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


INTERVAL_MS = interval_milliseconds(INTERVAL)
START_TIME_MS = parse_start_date(START_DATE)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(threadName)s: %(message)s",
)
logger = logging.getLogger("binance_collector")
