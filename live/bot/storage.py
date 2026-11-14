"""Persistencia de eventos y lectura de velas cerradas."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pandas as pd

from .config import logger

DB_BUSY_TIMEOUT_MS = 5_000
DB_JOURNAL_SIZE_LIMIT = 67_108_864
DB_WAL_AUTOCHECKPOINT = 1_000

SIGNALS_DDL = """
CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id   TEXT    NOT NULL,
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

SIGNAL_ID_FIELDS = (
    "symbol",
    "interval",
    "timeframe",
    "event",
    "signal",
    "reason",
    "bar_time",
)


def build_signal_id(event: dict) -> str:
    """Construye un ID estable a partir de la identidad lógica del evento."""
    identity = [event[field] for field in SIGNAL_ID_FIELDS]
    canonical = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sig_{digest}"


SIGNALS_INDEXES = (
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_signals_event
    ON signals(symbol, interval, timeframe, event, signal, reason, bar_time)
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_signals_signal_id ON signals(signal_id)",
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
    signal_id, symbol, interval, timeframe, bar_time, signal_time, event,
    signal, side, reason, price, ema_fast, ema_slow, adx_entry, executed,
    created_at, payload
) VALUES (
    :signal_id, :symbol, :interval, :timeframe, :bar_time, :signal_time,
    :event, :signal, :side, :reason, :price, :ema_fast, :ema_slow,
    :adx_entry, :executed, :created_at, :payload
)
"""


class SignalStore:
    """Almacén SQLite de eventos idempotentes y estado persistente del bot."""

    def __init__(self, path: Path):
        self.path = path
        self.conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            self.path,
            timeout=DB_BUSY_TIMEOUT_MS / 1000,
        )
        try:
            self.conn.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")
            self.conn.execute("PRAGMA foreign_keys=ON")
            row = self.conn.execute("PRAGMA journal_mode=WAL").fetchone()
            mode = str(row[0]).lower() if row else ""
            if mode != "wal":
                raise sqlite3.OperationalError(
                    f"No se pudo activar WAL para {self.path}: "
                    f"modo devuelto {mode!r}"
                )
            # Signals and the live_since marker are important recovery state.
            self.conn.execute("PRAGMA synchronous=FULL")
            self.conn.execute(f"PRAGMA wal_autocheckpoint={DB_WAL_AUTOCHECKPOINT}")
            self.conn.execute(f"PRAGMA journal_size_limit={DB_JOURNAL_SIZE_LIMIT}")
            self.conn.execute(SIGNALS_DDL)
            self._migrate_signal_ids()
            self.conn.execute(BOT_STATE_DDL)
            for ddl in SIGNALS_INDEXES:
                self.conn.execute(ddl)
            self.conn.commit()
        except Exception:
            self.conn.close()
            self.conn = None
            raise
        logger.info("signal.db listo en %s", self.path)

    def _migrate_signal_ids(self) -> None:
        """Añade y rellena signal_id en bases existentes sin cambiar sus filas."""
        columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(signals)")
        }
        if "signal_id" not in columns:
            self.conn.execute("ALTER TABLE signals ADD COLUMN signal_id TEXT")

        rows = self.conn.execute(
            """
            SELECT id, symbol, interval, timeframe, event, signal, reason, bar_time
            FROM signals
            WHERE signal_id IS NULL OR signal_id = ''
            """
        ).fetchall()
        for row in rows:
            row_id, *identity = row
            event = dict(zip(SIGNAL_ID_FIELDS, identity))
            self.conn.execute(
                "UPDATE signals SET signal_id = ? WHERE id = ?",
                (build_signal_id(event), row_id),
            )

    def insert(self, events: list[dict]) -> int:
        if not events or self.conn is None:
            return 0
        rows = [
            {**event, "signal_id": build_signal_id(event)}
            for event in events
        ]
        cursor = self.conn.executemany(INSERT_SQL, rows)
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
        self.conn = sqlite3.connect(
            self.path,
            timeout=DB_BUSY_TIMEOUT_MS / 1000,
        )
        self.conn.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")
        self.conn.execute("PRAGMA foreign_keys=ON")
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
