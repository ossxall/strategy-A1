#!/usr/bin/env python3
"""
Bot 1 — Estrategia EMA Cross en vivo (eventos en signal.db)
===========================================================

Lee la data OHLCV almacenada por `feed.py` en `data/feed.db` (1m por defecto) y
**replica exactamente el backtest** definido en `strategy/strategy.py`:

- Remuestrea a `STRATEGY_TIMEFRAME`, calcula señales y salidas con
  `EMACrossStrategy` y ejecuta el `Backtester` sobre las velas cerradas.
- Por cada acción que tomaría el backtest (apertura, reversal, cierre por
  EMA_CROSS, ADX_REVERSAL, EMA_WEAKENING, RSI_DIVERGENCE, STOP_LOSS, BREAKEVEN,
  PARTIAL_TP, EMA_SLOW_TOUCH, CONFIRM_MOVE_AGAINST)
  guarda un evento en `signal.db`.

Reglas de diseño
----------------
- El bot **no crea órdenes**. Solo publica los eventos con `executed = 0`
  (false); un consumidor externo decide si los ejecuta y actualiza la marca.
- Cada evento es idempotente: se deduplica por
  (symbol, interval, timeframe, event, signal, reason, bar_time).
- Las decisiones se conocen al cierre de la vela y se ejecutan en la apertura
  siguiente (igual que el backtest); por eso se guardan `bar_time` (vela de la
  señal) y `signal_time` (momento de ejecución). Las salidas por stop/breakeven/
  partial/toque a la EMA slow/movimiento adverso desde la confirmación se
  disparan dentro de la vela, por lo que `bar_time == signal_time`.
- `force_close_at_end` se desactiva: en vivo una posición abierta no debe
  cerrarse artificialmente al final de los datos disponibles.

Orden de los eventos (para el consumidor)
-----------------------------------------
Cada posición genera un `ENTRY` y uno o más `EXIT` (partial TP, weakening,
breakeven, stop, reversal o cierre completo). Para reproducir el backtest,
ordena por `(signal_time, bar_time, event)` con `EXIT` antes que `ENTRY`:

- `ENTRY`: `signal_time = bar_time + timeframe` (se ejecuta en la apertura
  siguiente a la vela de la señal).
- `EXIT` por EMA_CROSS / ADX / EMA_WEAKENING / RSI_DIVERGENCE: también
  `signal_time = bar_time + timeframe` (se decide al cierre y se ejecuta en la
  apertura siguiente).
- `EXIT` por STOP_LOSS / BREAKEVEN / PARTIAL_TP / EMA_SLOW_TOUCH /
  CONFIRM_MOVE_AGAINST: ocurre dentro de la vela, por eso `signal_time ==
  bar_time`. Si coincide con una apertura, la apertura va primero (bar_time
  menor).

El `payload` de cada `EXIT` incluye `action` (`CLOSE`/`PARTIAL_CLOSE`),
`close_pct` (porcentaje de la posición *restante* a cerrar, igual que el
backtest), `exit_price`, `pnl_net` y `r_multiple`.

Uso
---
    python bot.py                 # bucle continuo (polling de feed.db)
    python bot.py --once          # procesa lo disponible y termina
    python bot.py --help

Modo live-only
--------------
Con `--live-only` (o `LIVE_ONLY=true`) el bot ignora el histórico y publica
solo las señales cuya vela (`bar_time`) es igual o posterior al arranque; así
no vuelca de golpe todas las señales pasadas al ponerse en marcha. La marca de
arranque se guarda en `signal.db` (`bot_state.live_since_ms`) para no perder
señales entre reinicios. Para reanudar desde una fecha concreta:
`--live-since 2026-09-01` (o epoch en ms).

Endpoint HTTP de debug
----------------------
Expone el último cálculo de series de cada timeframe en JSON (ver `server.py`).
Se configura con `--http-host` / `HTTP_HOST` (por defecto 127.0.0.1) y
`--http-port` / `HTTP_PORT` (por defecto 8080; `0` lo desactiva).

La configuración se toma de `.env` (mismas variables que `feed.py` más las
de la estrategia con prefijo `STRATEGY_`).
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import math
import os
import signal
import sqlite3
import sys
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Estrategia compartida (strategy/strategy.py) y módulos locales (server.py)
# ---------------------------------------------------------------------------
_BASE_DIR = Path(__file__).resolve().parent          # .../bin/legacy/bot1
_LEGACY_DIR = _BASE_DIR.parent                       # .../bin/legacy
for _path in (_LEGACY_DIR, _BASE_DIR):               # bot1/ queda primero
    sys.path.insert(0, str(_path))

from server import SeriesSnapshot, start_server
from strategy.strategy import (
    Backtester,
    Config,
    EMACrossStrategy,
)
from strategy.utils import resample_ohlcv, source_minutes

try:
    from dotenv import load_dotenv

    # Prioriza el .env del propio bot y admite el del directorio actual.
    load_dotenv(_BASE_DIR / ".env", override=False)
    load_dotenv(override=False)
except ImportError:
    pass


# ===========================================================================
# Utilidades y configuración
# ===========================================================================

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(threadName)s: %(message)s",
)
logger = logging.getLogger("bot1")

STOP_EVENT = threading.Event()


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
    """Resuelve rutas relativas contra el directorio del bot (bot1/)."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else (_BASE_DIR / path)


def touch_heartbeat() -> None:
    """Registra actividad para el healthcheck del contenedor (best-effort).

    Solo actúa si `BOT_HEARTBEAT` está definido; si no, es un no-op.
    """
    path = os.getenv("BOT_HEARTBEAT")
    if not path:
        return
    try:
        Path(path).write_text(utc_iso(), encoding="utf-8")
    except OSError:
        logger.debug("No se pudo escribir el heartbeat en %s", path)


# Campo de `Config` -> (variable de entorno, lector). Los defaults salen de
# `Config()`, así el bot ejecuta la misma estrategia que el backtest.
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
    "entry_dmi_risk_scaling_enabled": ("STRATEGY_ENTRY_DMI_RISK_SCALING", env_bool),
    "entry_dmi_risk_base": ("STRATEGY_ENTRY_DMI_RISK_BASE", env_float),
    "entry_dmi_risk_spread_scale": ("STRATEGY_ENTRY_DMI_RISK_SPREAD_SCALE", env_float),
    "entry_dmi_risk_min": ("STRATEGY_ENTRY_DMI_RISK_MIN", env_float),
    "entry_dmi_risk_max": ("STRATEGY_ENTRY_DMI_RISK_MAX", env_float),
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
    # Gestión de posición: afecta el sizing y las salidas (stop/partial).
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
    "move_stop_to_breakeven": ("STRATEGY_MOVE_STOP_TO_BREAKEVEN", env_bool),
    # Cierre por toque a la EMA slow (misma lógica que el backtest).
    "close_on_ema_slow_touch": ("STRATEGY_CLOSE_ON_EMA_SLOW_TOUCH", env_bool),
    "close_on_ema_slow_touch_pct": ("STRATEGY_CLOSE_ON_EMA_SLOW_TOUCH_PCT", env_float),
    "close_on_ema_slow_touch_after": ("STRATEGY_CLOSE_ON_EMA_SLOW_TOUCH_AFTER", env_bool),
    "close_on_ema_slow_touch_exit_pct": ("STRATEGY_CLOSE_ON_EMA_SLOW_TOUCH_EXIT_PCT", env_float),
    # Salidas: las usa `prepare_exits`.
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
    # Salida total por divergencia RSI en contra de la posición.
    "rsi_divergence_exit_enabled": ("STRATEGY_RSI_DIVERGENCE_EXIT", env_bool),
    "rsi_divergence_timeframe": ("STRATEGY_RSI_DIVERGENCE_TIMEFRAME", env_str),
    "rsi_divergence_len": ("STRATEGY_RSI_DIVERGENCE_LEN", env_int),
    "rsi_divergence_pivot": ("STRATEGY_RSI_DIVERGENCE_PIVOT", env_int),
    "rsi_divergence_lookback": ("STRATEGY_RSI_DIVERGENCE_LOOKBACK", env_int),
    "candle_pattern_exit_enabled": ("STRATEGY_CANDLE_PATTERN_EXIT", env_bool),
    "candle_pattern_exit_pct": ("STRATEGY_CANDLE_PATTERN_EXIT_PCT", env_float),
    "candle_pattern_exit_confirm_bars": ("STRATEGY_CANDLE_PATTERN_EXIT_CONFIRM_BARS", env_int),
    "candle_pattern_exit_loss_pct": ("STRATEGY_CANDLE_PATTERN_EXIT_LOSS_PCT", env_float),
    "candle_pattern_exit_long_loss_pct": ("STRATEGY_CANDLE_PATTERN_EXIT_LONG_LOSS_PCT", env_float),
    "candle_pattern_exit_short_loss_pct": ("STRATEGY_CANDLE_PATTERN_EXIT_SHORT_LOSS_PCT", env_float),
    "candle_pattern_exit_signed_loss_pct": ("STRATEGY_CANDLE_PATTERN_EXIT_SIGNED_LOSS_PCT", env_float),
    "ema55_touch_exit_enabled": ("STRATEGY_EMA55_TOUCH_EXIT", env_bool),
    "ema55_touch_tolerance_pct": ("STRATEGY_EMA55_TOUCH_TOLERANCE_PCT", env_float),
    "ema55_touch_lookback_bars": ("STRATEGY_EMA55_TOUCH_LOOKBACK_BARS", env_int),
    "ema55_touch_min_touches": ("STRATEGY_EMA55_TOUCH_MIN_TOUCHES", env_int),
    "ema55_touch_consecutive": ("STRATEGY_EMA55_TOUCH_CONSECUTIVE", env_int),
    "ema55_touch_breakout_buffer_pct": ("STRATEGY_EMA55_TOUCH_BREAKOUT_BUFFER_PCT", env_float),
    "ema55_touch_exit_loss_pct": ("STRATEGY_EMA55_TOUCH_EXIT_LOSS_PCT", env_float),
    "ema55_touch_short_exit_loss_pct": ("STRATEGY_EMA55_TOUCH_SHORT_EXIT_LOSS_PCT", env_float),
    "timestamp_is_close": ("STRATEGY_TIMESTAMP_IS_CLOSE", env_bool),
    # Entrada parcial + confirmación (scale-in)
    "entry_partial_enabled": ("STRATEGY_ENTRY_PARTIAL", env_bool),
    "entry_partial_pct": ("STRATEGY_ENTRY_PARTIAL_PCT", env_float),
    "entry_confirm_mode": ("STRATEGY_ENTRY_CONFIRM_MODE", env_str),
    "entry_confirm_bars": ("STRATEGY_ENTRY_CONFIRM_BARS", env_int),
    "entry_confirm_close_in_dir": ("STRATEGY_ENTRY_CONFIRM_CLOSE_IN_DIR", env_bool),
    "entry_confirm_retest_tol_pct": ("STRATEGY_ENTRY_CONFIRM_RETEST_TOL_PCT", env_float),
    "entry_confirm_max_move_pct": ("STRATEGY_ENTRY_CONFIRM_MAX_MOVE_PCT", env_float),
    "entry_confirm_move_exit_enabled": ("STRATEGY_ENTRY_CONFIRM_MOVE_EXIT", env_bool),
    "entry_confirm_move_exit_pct": ("STRATEGY_ENTRY_CONFIRM_MOVE_EXIT_PCT", env_float),
    "entry_confirm_move_exit_portion_pct": ("STRATEGY_ENTRY_CONFIRM_MOVE_EXIT_PORTION_PCT", env_float),
}


def build_strategy_config() -> Config:
    defaults = Config()
    return Config(
        **{
            field: read(var, getattr(defaults, field))
            for field, (var, read) in _CONFIG_ENV.items()
        }
    )


# ===========================================================================
# signal.db — almacén de eventos
# ===========================================================================

SIGNALS_DDL = """
CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    interval    TEXT    NOT NULL,
    timeframe   TEXT    NOT NULL,
    bar_time    INTEGER NOT NULL,
    signal_time INTEGER NOT NULL,
    event       TEXT    NOT NULL,
    signal      TEXT    NOT NULL,
    side        TEXT,
    reason      TEXT    NOT NULL,
    price       REAL,
    ema_fast    REAL,
    ema_slow    REAL,
    adx_entry   REAL,
    executed    INTEGER NOT NULL DEFAULT 0 CHECK (executed IN (0, 1)),
    created_at  TEXT    NOT NULL,
    payload     TEXT    NOT NULL DEFAULT '{}'
)
"""

SIGNALS_INDEXES = (
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_signals_event
    ON signals(symbol, interval, timeframe, event, signal, reason, bar_time)
    """,
    "CREATE INDEX IF NOT EXISTS idx_signals_bar_time ON signals(bar_time)",
    "CREATE INDEX IF NOT EXISTS idx_signals_signal_time ON signals(signal_time)",
    "CREATE INDEX IF NOT EXISTS idx_signals_executed ON signals(executed)",
)

BOT_STATE_DDL = """
CREATE TABLE IF NOT EXISTS bot_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

INSERT_SQL = """
INSERT OR IGNORE INTO signals (
    symbol, interval, timeframe, bar_time, signal_time, event, signal,
    side, reason, price, ema_fast, ema_slow, adx_entry, executed,
    created_at, payload
) VALUES (
    :symbol, :interval, :timeframe, :bar_time, :signal_time, :event, :signal,
    :side, :reason, :price, :ema_fast, :ema_slow, :adx_entry, :executed,
    :created_at, :payload
)
"""


class SignalStore:
    """Gestiona `signal.db` y la inserción idempotente de eventos."""

    def __init__(self, path: Path):
        self.path = path
        self.conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute(SIGNALS_DDL)
        self.conn.execute(BOT_STATE_DDL)
        for ddl in SIGNALS_INDEXES:
            self.conn.execute(ddl)
        self.conn.commit()
        logger.info("signal.db listo en %s", self.path)

    def insert(self, events: list[dict]) -> int:
        if not events or self.conn is None:
            return 0
        cursor = self.conn.executemany(INSERT_SQL, events)
        self.conn.commit()
        return max(cursor.rowcount, 0)

    def get_state(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM bot_state WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO bot_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None


# ===========================================================================
# feed.db — lectura de OHLCV
# ===========================================================================

class FeedReader:
    """Lee velas ya cerradas desde `feed.db` (solo lecturas)."""

    QUERY = """
        SELECT open_time, open, high, low, close, volume
        FROM candles
        WHERE symbol = ? AND interval = ? AND is_closed = 1
        ORDER BY open_time
    """

    def __init__(self, path: Path, symbol: str, interval: str):
        self.path = path
        self.symbol = symbol
        self.interval = interval
        self.conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(
                f"No existe feed.db en {self.path}. Inicia feed.py primero."
            )
        self.conn = sqlite3.connect(self.path, timeout=30)
        # Garantiza que el bot nunca escriba en feed.db.
        self.conn.execute("PRAGMA query_only=ON")

    def load_closed(self) -> pd.DataFrame:
        """Devuelve todas las velas cerradas indexadas por timestamp UTC."""
        if self.conn is None:
            self.connect()
        frame = pd.read_sql_query(
            self.QUERY, self.conn, params=(self.symbol, self.interval)
        )
        if frame.empty:
            return frame
        frame["timestamp"] = pd.to_datetime(frame.pop("open_time"), unit="ms", utc=True)
        return (
            frame.dropna(subset=["open", "high", "low", "close"])
            .drop_duplicates("timestamp", keep="last")
            .sort_values("timestamp")
            .set_index("timestamp")[["open", "high", "low", "close", "volume"]]
        )

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None


# ===========================================================================
# Series (para el endpoint) y eventos
# ===========================================================================

def resample(raw: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Remuestrea OHLCV a `timeframe` (copia si el origen ya es >= timeframe)."""
    if raw.empty or pd.Timedelta(minutes=source_minutes(raw)) >= pd.Timedelta(timeframe):
        return raw.copy()
    return resample_ohlcv(raw, timeframe)


def series_frame(
    strategy: EMACrossStrategy,
    raw: pd.DataFrame,
    bars: pd.DataFrame,
    name: str,
    computed: tuple | None = None,
) -> pd.DataFrame:
    """OHLCV de `bars` + las series de la estrategia (señales y salidas).

    `computed` incluye las señales, salidas previas y las categorías TA-Lib
    (reversión, bidireccionales y doji) si ya se calcularon para `bars`.
    """
    frame = bars.copy()
    try:
        if computed is None:
            signals = strategy.generate_signals(bars, raw)
            exits = (
                *strategy.prepare_exits(raw, bars),
                *strategy.prepare_candle_exits(bars),
            )
        else:
            signals, *exits = computed
        if len(exits) == 3:  # Compatibilidad con snapshots/callers antiguos.
            adx_exit, weak_exit, rsi_div_exit = exits
            candle_bull_exit = candle_bear_exit = None
            signed_bull_exit = signed_bear_exit = doji_exit = None
        else:
            (adx_exit, weak_exit, rsi_div_exit, candle_bull_exit, candle_bear_exit,
             signed_bull_exit, signed_bear_exit, doji_exit) = exits
    except Exception:
        logger.exception("No se pudieron calcular las series de %s", name)
        return frame
    for col in signals.columns:
        if col not in frame.columns:
            frame[col] = signals[col].to_numpy()
    for col, arr in (
        ("adx_exit", adx_exit),
        ("weak_exit", weak_exit),
        ("rsi_div_exit", rsi_div_exit),
        ("candle_bull_exit", candle_bull_exit),
        ("candle_bear_exit", candle_bear_exit),
        ("signed_bull_exit", signed_bull_exit),
        ("signed_bear_exit", signed_bear_exit),
        ("doji_exit", doji_exit),
    ):
        if arr is not None and len(arr) == len(frame):
            frame[col] = np.asarray(arr)
    if candle_bull_exit is not None and candle_bear_exit is not None \
            and signed_bull_exit is not None and signed_bear_exit is not None:
        frame["candle_exit"] = (
            (np.asarray(candle_bull_exit, bool) | np.asarray(signed_bull_exit, bool)).astype(np.int8)
            - (np.asarray(candle_bear_exit, bool) | np.asarray(signed_bear_exit, bool)).astype(np.int8)
        )
    return frame


def build_series_frames(
    strategy: EMACrossStrategy,
    raw: pd.DataFrame,
    bars: pd.DataFrame,
    computed: tuple,
    cfg: Config,
    interval: str,
) -> dict[str, tuple[list[str], pd.DataFrame]]:
    """Arma `{timeframe: (roles, DataFrame)}` para el endpoint de series.

    Todos los timeframes llevan las mismas series (OHLCV, EMAs, ADX, cruces,
    target, salidas), calculadas con la misma estrategia.
    """
    frames: dict[str, tuple[list[str], pd.DataFrame]] = {}

    def add(role: str, timeframe: str, make) -> None:
        if timeframe not in frames:
            frames[timeframe] = ([], make())
        frames[timeframe][0].append(role)

    add("strategy", cfg.timeframe,
        lambda: series_frame(strategy, raw, bars, cfg.timeframe, computed))
    add("raw", interval, lambda: series_frame(strategy, raw, raw, interval))
    adx_tfs = cfg.adx_timeframes if cfg.adx_exit_enabled else ()
    weak_tfs = cfg.ema_weakening_timeframes if cfg.ema_weakening_exit_enabled else ()
    for role, timeframe in (
        *(("adx_exit", tf) for tf in adx_tfs),
        *(("ema_weakening", tf) for tf in weak_tfs),
    ):
        if timeframe:
            add(role, timeframe,
                lambda tf=timeframe: series_frame(strategy, raw, resample(raw, tf), tf))
    return frames


def events_to_trades(events: list[dict]) -> list[dict]:
    """Eventos ENTRY/EXIT -> trades planos para graficar (tiempos en epoch ms).

    Se ordenan como el backtest: por tiempo de ejecución, EXIT antes que ENTRY.
    """
    trades = [
        {
            "event": e["event"], "side": e["side"], "reason": e["reason"],
            "time": e["signal_time"], "price": e["price"], **json.loads(e["payload"]),
        }
        for e in events
    ]
    return sorted(trades, key=lambda t: (t["time"], t["event"] != "EXIT"))


def _reason_is_intrabar(reason: str) -> bool:
    """Stop, breakeven, take-profit parcial y salidas por precio se disparan dentro de la vela."""
    return reason.startswith(("STOP_LOSS", "PARTIAL_TP", "EMA_SLOW_TOUCH", "CONFIRM_MOVE_AGAINST")) \
        or reason == "BREAKEVEN"


def build_events(
    bars: pd.DataFrame,
    signals: pd.DataFrame,
    adx_exit: np.ndarray | None,
    weak_exit: np.ndarray | None,
    rsi_div_exit: np.ndarray | None,
    cfg: Config,
    symbol: str,
    interval: str,
    candle_bull_exit: np.ndarray | None = None,
    candle_bear_exit: np.ndarray | None = None,
    signed_bull_exit: np.ndarray | None = None,
    signed_bear_exit: np.ndarray | None = None,
    doji_exit: np.ndarray | None = None,
) -> list[dict]:
    """Ejecuta el backtest y traduce cada acción real a un evento.

    Reutiliza el motor `Backtester` de `strategy/strategy.py` sobre las velas
    cerradas, de modo que el bot publica exactamente las acciones que tomaría
    el backtest: aperturas (incluidas reversals), cierres por EMA_CROSS,
    ADX_REVERSAL, EMA_WEAKENING, RSI_DIVERGENCE, EMA55_REPEATED_TOUCH_WEAKNESS(_SHORT)
    y patrones TA-Lib confirmados por doji, ruptura del extremo y pérdida abierta; además de STOP_LOSS,
    BREAKEVEN, PARTIAL_TP y EMA_SLOW_TOUCH. La posición abierta al final no se
    cierra (`force_close_at_end=False`): en vivo sigue vigente.
    """
    if len(bars) < 2:
        return []

    engine = Backtester(
        bars, replace(cfg, force_close_at_end=False), signals, adx_exit, weak_exit,
        rsi_div_exit, candle_bull_exit=candle_bull_exit, candle_bear_exit=candle_bear_exit,
        signed_bull_exit=signed_bull_exit, signed_bear_exit=signed_bear_exit,
        doji_exit=doji_exit,
    )
    # `Backtester.run` imprime progreso; lo silenciamos para no ensuciar el log.
    with contextlib.redirect_stdout(io.StringIO()):
        engine.run()

    tf_ms = int(pd.Timedelta(cfg.timeframe).total_seconds() * 1000)
    created_at = utc_iso()

    # Indicadores por barra {bar_ms: (ema_fast, ema_slow, adx_entry)}.
    indicators = dict(
        zip(
            bars.index.as_unit("ms").asi8.tolist(),  # epoch ms, sea cual sea la unidad
            zip(*(signals[c].to_numpy(float) for c in ("ema_fast", "ema_slow", "adx_entry"))),
        )
    )

    def make_event(event, side, reason, bar_time, signal_time, price, payload) -> dict:
        fast, slow, adx = indicators.get(bar_time, (np.nan,) * 3)
        return {
            "symbol": symbol,
            "interval": interval,
            "timeframe": cfg.timeframe,
            "bar_time": bar_time,
            "signal_time": signal_time,
            "event": event,
            "signal": side,
            "side": side,
            "reason": reason,
            "price": opt_float(price),
            "ema_fast": opt_float(fast),
            "ema_slow": opt_float(slow),
            "adx_entry": opt_float(adx),
            "executed": 0,
            "created_at": created_at,
            "payload": json.dumps(payload),
        }

    open_pos = engine.position
    entries: dict[int, tuple] = {}              # position_id -> (side, time, price)
    legs: dict[int, list] = {}                  # position_id -> tramos de cierre
    entry_meta: dict[int, float] = {}           # position_id -> % del objetivo en 1ª entrada
    for trade in engine.trades:
        entries.setdefault(trade.position_id, (trade.side, trade.entry_time, trade.entry_price))
        legs.setdefault(trade.position_id, []).append(trade)
    if open_pos is not None:
        entries.setdefault(
            open_pos.position_id,
            ("LONG" if open_pos.side == 1 else "SHORT", open_pos.entry_time, open_pos.entry_price),
        )
    for opened in engine.opened:
        entries.setdefault(
            op_id := op["position_id"],  # noqa: F821
        ) if False else None
    # El precio/tiempo de entrada real (con precio medio ponderado tras los adds)
    # y el % del objetivo entrado en la primera fase salen del propio motor.
    for op in getattr(engine, "opened", []):
        pid = op["position_id"]
        entries[pid] = (
            "LONG" if op["side"] == 1 else "SHORT", op["time"],
            # Si hubo adds, la entrada media está en los tramos de cierre; si no,
            # se usa el precio de la primera entrada.
            entries.get(pid, (None, None, op["price"]))[2] or op["price"],
        )
        entry_pct = {pid: op["entry_pct"]}

    events: list[dict] = []

    # --- Aperturas: una por posición (cerrada o todavía abierta) ---
    for pid in sorted(entries):
        side, entry_time, entry_price = entries[pid]
        signal_time = ts_ms(entry_time)
        events.append(
            make_event(
                "ENTRY", side, "EMA_CROSS", signal_time - tf_ms, signal_time,
                entry_price,
                {
                    "action": "OPEN",
                    "position_id": pid,
                    "entry_pct": float(entry_pct.get(pid, 100.0)),
                },
            )
        )

    # --- Añadidos de entrada parcial (scale-in): completan la posición ---
    for add in getattr(engine, "adds", []):
        pid = add["position_id"]
        side = "LONG" if add["side"] == 1 else "SHORT"
        signal_time = ts_ms(add["time"])
        target = add.get("target_quantity") or 0.0
        add_pct = 100.0 * add["quantity"] / target if target else 100.0
        events.append(
            make_event(
                "ENTRY_ADD", side, "ENTRY_ADD", signal_time - tf_ms, signal_time,
                add["price"],
                {
                    "action": "ADD",
                    "position_id": pid,
                    "add_pct": add_pct,
                    "add_price": opt_float(add["price"]),
                },
            )
        )

    # --- Cierres: un evento por cada tramo ejecutado por el backtest ---
    for position_id in sorted(legs):
        # Cantidad realmente llenada: suma de tramos cerrados + lo que sigue
        # abierto (con `target_quantity` puede haber un add aún no ejecutado).
        if open_pos is not None and open_pos.position_id == position_id:
            filled = sum(leg.quantity for leg in legs[position_id]) + open_pos.quantity
            remaining_total = open_pos.target_quantity or open_pos.initial_quantity
            if remaining_total <= 0:
                remaining_total = filled
        else:
            filled = sum(leg.quantity for leg in legs[position_id])
            remaining_total = filled

        for leg in legs[position_id]:
            signal_time = ts_ms(leg.exit_time)
            bar_time = signal_time if _reason_is_intrabar(leg.reason) else signal_time - tf_ms
            # El backtest cierra un porcentaje de la posición *restante*; es el
            # que debe aplicar el consumidor sobre su posición.
            close_pct = 100.0 * leg.quantity / remaining_total if remaining_total else 100.0
            r_multiple = leg.pnl_net / leg.position_risk if leg.position_risk else None
            events.append(
                make_event(
                    "EXIT", leg.side, leg.reason, bar_time, signal_time, leg.exit_price,
                    {
                        "action": "CLOSE" if close_pct >= 99.999 else "PARTIAL_CLOSE",
                        "position_id": position_id,
                        "close_pct": close_pct,
                        "exit_price": opt_float(leg.exit_price),
                        "pnl_net": opt_float(leg.pnl_net),
                        "r_multiple": opt_float(r_multiple),
                    },
                )
            )
            remaining_total -= leg.quantity

    return events


# ===========================================================================
# Bot
# ===========================================================================

class SignalBot:
    def __init__(
        self,
        cfg: Config,
        feed: FeedReader,
        store: SignalStore,
        symbol: str,
        interval: str,
        min_bars: int | None = None,
        live_only: bool = False,
        live_since_ms: int | None = None,
        snapshot: SeriesSnapshot | None = None,
    ):
        self.cfg = cfg
        self.feed = feed
        self.store = store
        self.symbol = symbol
        self.interval = interval
        self.strategy = EMACrossStrategy(cfg)
        self.min_bars = min_bars or (cfg.ema_slow + 2)
        self.live_only = live_only
        self.live_since_ms = live_since_ms
        self.snapshot = snapshot
        self._live_resolved = False

    def _resolve_live_since(self) -> None:
        """Fija la marca desde la que se publican señales (modo live-only).

        La marca se guarda en `signal.db` (`bot_state`) para no perder señales
        entre reinicios.
        """
        if self._live_resolved:
            return
        self._live_resolved = True
        if not self.live_only:
            return

        if self.live_since_ms is None:
            stored = self.store.get_state("live_since_ms")
            if stored is not None:
                self.live_since_ms = int(stored)
                logger.info(
                    "Modo live-only: reanudando desde %s.", ms_to_iso(self.live_since_ms)
                )
                return
            self.live_since_ms = ts_ms(utc_now())

        self.store.set_state("live_since_ms", str(self.live_since_ms))
        logger.info(
            "Modo live-only: solo señales desde %s (se ignora el histórico).",
            ms_to_iso(self.live_since_ms),
        )

    def _publish_snapshot(self, raw, bars, computed, events) -> None:
        """Actualiza el endpoint de series (nunca debe romper el ciclo)."""
        if self.snapshot is None:
            return
        try:
            frames = build_series_frames(
                self.strategy, raw, bars, computed, self.cfg, self.interval
            )
            self.snapshot.publish(frames, self.cfg.timeframe, events_to_trades(events))
        except Exception:
            logger.exception("No se pudo actualizar el snapshot de series")

    def process_once(self) -> int:
        """Ejecuta un ciclo: lee, calcula y publica eventos nuevos."""
        self._resolve_live_since()
        raw = self.feed.load_closed()
        if raw.empty:
            logger.debug("feed.db sin velas cerradas todavía.")
            return 0

        bars = resample(raw, self.cfg.timeframe)
        if len(bars) < self.min_bars:
            logger.info(
                "Insuficientes velas %s (%d < %d); esperando más datos.",
                self.cfg.timeframe, len(bars), self.min_bars,
            )
            return 0

        signals = self.strategy.generate_signals(bars, raw)
        adx_exit, weak_exit, rsi_div_exit = self.strategy.prepare_exits(raw, bars)
        (candle_bull_exit, candle_bear_exit, signed_bull_exit,
         signed_bear_exit, doji_exit) = self.strategy.prepare_candle_exits(bars)
        events = build_events(
            bars, signals, adx_exit, weak_exit, rsi_div_exit,
            self.cfg, self.symbol, self.interval,
            candle_bull_exit=candle_bull_exit, candle_bear_exit=candle_bear_exit,
            signed_bull_exit=signed_bull_exit, signed_bear_exit=signed_bear_exit,
            doji_exit=doji_exit,
        )
        # El endpoint muestra todos los trades del backtest (sin filtro live-only).
        self._publish_snapshot(
            raw, bars, (signals, adx_exit, weak_exit, rsi_div_exit, candle_bull_exit,
                        candle_bear_exit, signed_bull_exit, signed_bear_exit, doji_exit), events
        )
        if self.live_since_ms is not None:
            # live-only: descarta las señales anteriores al arranque.
            events = [e for e in events if e["bar_time"] >= self.live_since_ms]
        inserted = self.store.insert(events)
        if inserted:
            logger.info(
                "Publicados %d eventos nuevos (de %d acciones del backtest).",
                inserted, len(events),
            )
        return inserted

    def run(self, once: bool = False, poll_seconds: float = 30.0) -> None:
        while not STOP_EVENT.is_set():
            try:
                self.process_once()
            except FileNotFoundError as exc:
                logger.warning("%s", exc)
            except Exception:
                logger.exception("Error durante el ciclo del bot")

            touch_heartbeat()  # señal de vida para el healthcheck del contenedor

            if once or STOP_EVENT.wait(poll_seconds):
                break


# ===========================================================================
# CLI / arranque
# ===========================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bot EMA Cross en vivo: replica el backtest y publica sus "
        "acciones como eventos en signal.db."
    )
    parser.add_argument(
        "--feed-db",
        default=os.getenv("DB_PATH", "data/feed.db"),
        help="Ruta a feed.db (por defecto DB_PATH o data/feed.db).",
    )
    parser.add_argument(
        "--signal-db",
        default=os.getenv("SIGNAL_DB", "data/signal.db"),
        help="Ruta a signal.db (por defecto SIGNAL_DB o data/signal.db).",
    )
    parser.add_argument("--symbol", default=os.getenv("SYMBOL", "BTCUSDT"))
    parser.add_argument("--interval", default=os.getenv("INTERVAL", "1m"))
    parser.add_argument("--timeframe", default=None, help="Override de STRATEGY_TIMEFRAME.")
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=env_float("POLL_SECONDS", 30.0),
        help="Intervalo de sondeo de feed.db en modo continuo.",
    )
    parser.add_argument("--once", action="store_true", help="Un solo ciclo y salir.")
    parser.add_argument(
        "--live-only",
        action="store_true",
        help="Publica solo señales desde el arranque (ignora el histórico).",
    )
    parser.add_argument(
        "--live-since",
        default=None,
        help="Marca temporal (epoch ms o ISO 8601) desde la que publicar; "
        "implica --live-only.",
    )
    parser.add_argument(
        "--http-host",
        default=os.getenv("HTTP_HOST", "127.0.0.1"),
        help="Host del endpoint HTTP de series (por defecto HTTP_HOST o 127.0.0.1).",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=env_int("HTTP_PORT", 8080),
        help="Puerto del endpoint HTTP de series (0 lo desactiva).",
    )
    return parser.parse_args()


def request_stop(signum=None, frame=None) -> None:
    logger.info("Señal de parada recibida; cerrando bot…")
    STOP_EVENT.set()


def main() -> int:
    args = parse_args()
    cfg = build_strategy_config()
    if args.timeframe:
        cfg.timeframe = args.timeframe

    symbol = args.symbol.upper()
    interval = args.interval
    feed_path = resolve_path(args.feed_db)
    signal_path = resolve_path(args.signal_db)

    logger.info(
        "Bot iniciado: symbol=%s interval=%s timeframe=%s feed=%s signal=%s",
        symbol, interval, cfg.timeframe, feed_path, signal_path,
    )

    live_only = args.live_only or args.live_since is not None or env_bool("LIVE_ONLY", False)
    live_since_ms = parse_live_since(args.live_since) if args.live_since else None

    feed = FeedReader(feed_path, symbol, interval)
    store = SignalStore(signal_path)
    snapshot = SeriesSnapshot(symbol, interval) if args.http_port > 0 else None
    http_server = None
    try:
        store.connect()
        if snapshot is not None:
            try:
                http_server = start_server(snapshot, args.http_host, args.http_port)
            except OSError:
                logger.exception(
                    "No se pudo abrir el endpoint HTTP en %s:%d; el bot sigue sin él.",
                    args.http_host, args.http_port,
                )
                snapshot = None
        SignalBot(
            cfg, feed, store, symbol, interval,
            live_only=live_only, live_since_ms=live_since_ms, snapshot=snapshot,
        ).run(once=args.once, poll_seconds=args.poll_seconds)
    finally:
        if http_server is not None:
            http_server.shutdown()
            http_server.server_close()
        feed.close()
        store.close()

    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        STOP_EVENT.set()
        raise SystemExit(130)
