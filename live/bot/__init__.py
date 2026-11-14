"""Aplicación de señales en vivo para la estrategia."""

from __future__ import annotations

import sys
from pathlib import Path

# En Docker, la estrategia se monta como paquete hermano bajo /app/legacy;
# en el checkout, está en la raíz del repositorio.
_PACKAGE_DIR = Path(__file__).resolve().parent
for _parent in (_PACKAGE_DIR, *_PACKAGE_DIR.parents):
    if (_parent / "strategy").is_dir():
        if str(_parent) not in sys.path:
            sys.path.insert(0, str(_parent))
        break

from .app import HTTP_SERVER_AVAILABLE, SignalBot, main, request_stop
from .config import (
    build_strategy_config,
    env_bool,
    env_float,
    env_int,
    env_str,
    parse_live_since,
    ts_ms,
)
from .events import build_events, build_events_and_result, events_to_trades
from .series import build_series_frames, resample, series_frame
from .storage import FeedReader, SignalStore

__all__ = [
    "HTTP_SERVER_AVAILABLE",
    "FeedReader",
    "SignalBot",
    "SignalStore",
    "build_events",
    "build_events_and_result",
    "build_series_frames",
    "build_strategy_config",
    "env_bool",
    "env_float",
    "env_int",
    "env_str",
    "events_to_trades",
    "main",
    "parse_live_since",
    "request_stop",
    "resample",
    "series_frame",
    "ts_ms",
]
