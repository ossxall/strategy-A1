"""Persistencia SQLite, estado del colector y diagnóstico de la base."""

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import (
    DB_PATH,
    INTERVAL,
    STATUS_DB_PROBE_INTERVAL,
    STATUS_DB_TIMEOUT,
    SYMBOL,
    utc_iso,
)
from .state import STATE, update_diagnostics

try:
    from ..db.sqlite_config import connect_sqlite
except ImportError:  # ejecución como paquete top-level desde live/
    from db.sqlite_config import connect_sqlite


def db_connect():
    return connect_sqlite(
        DB_PATH,
        synchronous="NORMAL",
        check_same_thread=False,
    )


def init_db():
    Path(DB_PATH).resolve().parent.mkdir(parents=True, exist_ok=True)
    with STATE.db_lock:
        # Candles are recoverable from REST; NORMAL avoids syncing every commit.
        conn = connect_sqlite(
            DB_PATH,
            synchronous="NORMAL",
            ensure_wal=True,
            check_same_thread=False,
        )
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS candles (
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    open_time INTEGER NOT NULL,
                    close_time INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    quote_volume REAL NOT NULL DEFAULT 0,
                    trades INTEGER NOT NULL DEFAULT 0,
                    taker_buy_base REAL NOT NULL DEFAULT 0,
                    taker_buy_quote REAL NOT NULL DEFAULT 0,
                    is_closed INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT 'rest',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, interval, open_time)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_candles_time
                ON candles(symbol, interval, open_time)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS collector_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()


def set_state(key, value):
    with STATE.status_lock:
        STATE.runtime_state[key] = str(value)
    with STATE.db_lock:
        conn = db_connect()
        try:
            conn.execute(
                """
                INSERT INTO collector_state(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                (key, str(value), utc_iso()),
            )
            conn.commit()
        finally:
            conn.close()


def get_latest_open_time():
    with STATE.db_lock:
        conn = db_connect()
        try:
            row = conn.execute(
                """
                SELECT MAX(open_time)
                FROM candles
                WHERE symbol=? AND interval=?
                """,
                (SYMBOL, INTERVAL),
            ).fetchone()
            return row[0] if row and row[0] is not None else None
        finally:
            conn.close()


def probe_database():
    """Comprueba SQLite fuera del hilo HTTP y con timeout acotado."""
    started = time.monotonic()
    connection = None
    try:
        connection = connect_sqlite(
            DB_PATH,
            timeout_ms=max(1, int(STATUS_DB_TIMEOUT * 1000)),
            read_only=True,
            query_only=True,
        )
        row = connection.execute(
            """
            SELECT open_time, close_time, is_closed, source
            FROM candles
            WHERE symbol=? AND interval=?
            ORDER BY open_time DESC
            LIMIT 1
            """,
            (SYMBOL, INTERVAL),
        ).fetchone()
        persisted_state = dict(
            connection.execute(
                """
                SELECT key, value
                FROM collector_state
                WHERE key IN (
                    'collector_status',
                    'websocket_status',
                    'backfill_status'
                )
                """
            ).fetchall()
        )
        latest_candle = None
        if row is not None:
            latest_candle = {
                "open_time_ms": int(row[0]),
                "open_time": datetime.fromtimestamp(
                    int(row[0]) / 1000, timezone.utc
                ).isoformat(),
                "close_time_ms": int(row[1]),
                "is_closed": bool(row[2]),
                "source": row[3],
            }
        return {
            "available": True,
            "checked_at": utc_iso(),
            "check_duration_seconds": round(time.monotonic() - started, 4),
            "last_error": None,
            "latest_candle": latest_candle,
            "persisted_state": persisted_state,
            "_checked_monotonic": time.monotonic(),
        }
    except (OSError, sqlite3.Error, ValueError, OverflowError) as exc:
        return {
            "available": False,
            "checked_at": utc_iso(),
            "check_duration_seconds": round(time.monotonic() - started, 4),
            "last_error": str(exc),
            "latest_candle": None,
            "persisted_state": {},
            "_checked_monotonic": time.monotonic(),
        }
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass


def database_diagnostics_worker():
    """Actualiza el diagnóstico de SQLite; nunca corre dentro de una petición."""
    while not STATE.stop_event.is_set():
        update_diagnostics("database", **probe_database())
        STATE.stop_event.wait(STATUS_DB_PROBE_INTERVAL)


def save_candle(candle):
    """
    REST puede corregir una vela cerrada.
    Una vela provisional del WS nunca sobrescribe una cerrada.
    """
    with STATE.db_lock:
        conn = db_connect()
        try:
            conn.execute(
                """
                INSERT INTO candles (
                    symbol, interval, open_time, close_time,
                    open, high, low, close, volume,
                    quote_volume, trades, taker_buy_base,
                    taker_buy_quote, is_closed, source, updated_at
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(symbol, interval, open_time)
                DO UPDATE SET
                    close_time=excluded.close_time,
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume,
                    quote_volume=excluded.quote_volume,
                    trades=excluded.trades,
                    taker_buy_base=excluded.taker_buy_base,
                    taker_buy_quote=excluded.taker_buy_quote,
                    is_closed=MAX(candles.is_closed, excluded.is_closed),
                    source=excluded.source,
                    updated_at=excluded.updated_at
                WHERE
                    candles.is_closed=0
                    OR excluded.is_closed=1
                """,
                (
                    SYMBOL,
                    INTERVAL,
                    candle["open_time"],
                    candle["close_time"],
                    candle["open"],
                    candle["high"],
                    candle["low"],
                    candle["close"],
                    candle["volume"],
                    candle["quote_volume"],
                    candle["trades"],
                    candle["taker_buy_base"],
                    candle["taker_buy_quote"],
                    candle["is_closed"],
                    candle["source"],
                    utc_iso(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
