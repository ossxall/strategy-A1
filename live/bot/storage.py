"""Persistencia de eventos y lectura de velas cerradas."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from .config import logger

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
    """Almacén SQLite de eventos idempotentes y estado persistente del bot."""

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


class FeedReader:
    """Lector de solo lectura de velas cerradas de feed.db."""

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
        self.conn.execute("PRAGMA query_only=ON")

    def load_closed(self) -> pd.DataFrame:
        """Devuelve las velas cerradas indexadas por timestamp UTC."""
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
