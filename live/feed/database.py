"""Persistencia SQLite, estado del colector y diagnóstico de la base."""

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import (
    DB_PATH,
    INTERVAL,
    INTERVAL_MS,
    STATUS_DB_PROBE_INTERVAL,
    STATUS_DB_TIMEOUT,
    SYMBOL,
    utc_iso,
)
from .state import STATE, update_diagnostics

try:
    from ..db.sqlite_config import (
        SQLiteSchemaError,
        connect_sqlite,
        sqlite_user_tables,
        validate_index,
        validate_index_set,
        validate_no_views_or_triggers,
        validate_quick_check,
        validate_table,
    )
except ImportError:  # ejecución como paquete top-level desde live/
    from db.sqlite_config import (
        SQLiteSchemaError,
        connect_sqlite,
        sqlite_user_tables,
        validate_index,
        validate_index_set,
        validate_no_views_or_triggers,
        validate_quick_check,
        validate_table,
    )


FEED_TABLES = {"candles", "collector_state"}
FEED_CANDLE_COLUMNS = {
    "symbol": "TEXT",
    "interval": "TEXT",
    "open_time": "INTEGER",
    "close_time": "INTEGER",
    "open": "REAL",
    "high": "REAL",
    "low": "REAL",
    "close": "REAL",
    "volume": "REAL",
    "quote_volume": "REAL",
    "trades": "INTEGER",
    "taker_buy_base": "REAL",
    "taker_buy_quote": "REAL",
    "is_closed": "INTEGER",
    "source": "TEXT",
    "updated_at": "TEXT",
}
FEED_STATE_COLUMNS = {
    "key": "TEXT",
    "value": "TEXT",
    "updated_at": "TEXT",
}


def validate_feed_schema(connection):
    """Verifica el contrato SQL usado por el colector y por sus lectores."""
    validate_no_views_or_triggers(connection, "feed.db")
    tables = sqlite_user_tables(connection)
    if tables != FEED_TABLES:
        raise SQLiteSchemaError(
            "feed.db contiene tablas "
            f"{sorted(tables)!r}; se requieren exactamente {sorted(FEED_TABLES)!r}"
        )

    validate_table(
        connection,
        "candles",
        FEED_CANDLE_COLUMNS,
        primary_key=("symbol", "interval", "open_time"),
        exact_columns=True,
        not_null={
            "symbol": True,
            "interval": True,
            "open_time": True,
            "close_time": True,
            "open": True,
            "high": True,
            "low": True,
            "close": True,
            "volume": True,
            "quote_volume": True,
            "trades": True,
            "taker_buy_base": True,
            "taker_buy_quote": True,
            "is_closed": True,
            "source": True,
            "updated_at": True,
        },
        defaults={
            "quote_volume": "0",
            "trades": "0",
            "taker_buy_base": "0",
            "taker_buy_quote": "0",
            "is_closed": "0",
            "source": "'rest'",
        },
    )
    validate_table(
        connection,
        "collector_state",
        FEED_STATE_COLUMNS,
        primary_key=("key",),
        exact_columns=True,
        not_null={"value": True, "updated_at": True},
    )
    validate_index(
        connection,
        "candles",
        "idx_candles_time",
        ("symbol", "interval", "open_time"),
    )
    validate_index_set(connection, "candles", {"idx_candles_time"})
    validate_index_set(connection, "collector_state", set())
    validate_quick_check(connection, "feed.db")


def _validate_existing_feed_database(path: Path) -> None:
    """Valida una base existente sin modificarla, antes de inicializar SQLite."""
    if not path.exists() or path.stat().st_size == 0:
        return
    connection = connect_sqlite(path, read_only=True, query_only=True)
    try:
        tables = sqlite_user_tables(connection)
        if tables:
            validate_feed_schema(connection)
        else:
            validate_no_views_or_triggers(connection, "feed.db")
            validate_quick_check(connection, "feed.db")
    finally:
        connection.close()


def db_connect():
    return connect_sqlite(
        DB_PATH,
        synchronous="NORMAL",
        check_same_thread=False,
    )


def init_db():
    db_path = Path(DB_PATH)
    db_path.resolve().parent.mkdir(parents=True, exist_ok=True)
    # No dejar que CREATE TABLE IF NOT EXISTS oculte una base antigua/malformada.
    _validate_existing_feed_database(db_path)
    with STATE.db_lock:
        # Candles are recoverable from REST; NORMAL avoids syncing every commit.
        conn = connect_sqlite(
            DB_PATH,
            synchronous="NORMAL",
            ensure_wal=True,
            check_same_thread=False,
        )
        try:
            conn.execute("BEGIN IMMEDIATE")
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
            validate_feed_schema(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
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


def get_first_missing_candle_range(start_time_ms):
    """Devuelve el primer rango de velas ausente desde la fecha configurada."""
    with STATE.db_lock:
        conn = db_connect()
        try:
            first = conn.execute(
                """
                SELECT MIN(open_time)
                FROM candles
                WHERE symbol=? AND interval=? AND open_time >= ?
                """,
                (SYMBOL, INTERVAL, start_time_ms),
            ).fetchone()[0]
            if first is None:
                return None
            if first > start_time_ms:
                return start_time_ms, first - INTERVAL_MS

            gap = conn.execute(
                """
                SELECT open_time, next_open_time
                FROM (
                    SELECT
                        open_time,
                        LEAD(open_time) OVER (ORDER BY open_time) AS next_open_time
                    FROM candles
                    WHERE symbol=? AND interval=? AND open_time >= ?
                )
                WHERE next_open_time > open_time + ?
                ORDER BY open_time
                LIMIT 1
                """,
                (SYMBOL, INTERVAL, start_time_ms, INTERVAL_MS),
            ).fetchone()
            if gap is None:
                return None
            return gap[0] + INTERVAL_MS, gap[1] - INTERVAL_MS
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
