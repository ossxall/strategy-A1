"""Persistencia de eventos y lectura de velas cerradas."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pandas as pd

from .config import logger, opt_float

try:
    from ..db.sqlite_config import connect_sqlite
except ImportError:  # ejecución como paquete top-level desde live/
    from db.sqlite_config import connect_sqlite

SIGNAL_SCHEMA_VERSION = 2
SIGNAL_ID_FIELDS = (
    "symbol",
    "interval",
    "timeframe",
    "event",
    "action",
    "signal",
    "reason",
    "bar_time",
    "position_id",
)

SIGNALS_DDL = """
CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id   TEXT    NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 2,
    symbol      TEXT    NOT NULL,
    interval    TEXT    NOT NULL,
    timeframe   TEXT    NOT NULL,
    bar_time    INTEGER NOT NULL,
    signal_time INTEGER NOT NULL,
    event       TEXT    NOT NULL,
    action      TEXT    NOT NULL CHECK (action IN ('OPEN', 'CLOSE', 'PARTIAL_CLOSE')),
    signal      TEXT    NOT NULL,
    side        TEXT,
    position_id INTEGER,
    scheduled   INTEGER NOT NULL DEFAULT 0 CHECK (scheduled IN (0, 1)),
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


def _payload_dict(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("payload debe ser JSON válido") from exc
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("payload debe ser un objeto JSON")
    return dict(value)


def _action_for(event: dict, payload: dict) -> str:
    action = event.get("action") or payload.get("action")
    if action:
        return str(action).upper()
    return "OPEN" if event.get("event") == "ENTRY" else "CLOSE"


def _position_id_for(event: dict, payload: dict) -> int | None:
    value = event.get("position_id")
    if value is None:
        value = payload.get("position_id")
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("position_id debe ser un entero")
    parsed = int(value)
    if parsed != value:
        raise ValueError("position_id debe ser un entero")
    return parsed


def build_signal_id(event: dict) -> str:
    """ID idempotente v2; identifica una acción dentro de una posición."""
    payload = _payload_dict(event.get("payload"))
    identity_event = {
        **event,
        "action": _action_for(event, payload),
        "position_id": _position_id_for(event, payload),
    }
    identity = [identity_event[field] for field in SIGNAL_ID_FIELDS]
    canonical = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sig_v2_{digest}"


SIGNALS_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_signals_signal_id ON signals(signal_id)",
    """
    CREATE INDEX IF NOT EXISTS idx_signals_event_order
    ON signals(signal_time, bar_time, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_signals_identity
    ON signals(symbol, interval, timeframe, event, action, signal, reason, bar_time, position_id)
    """,
    "CREATE INDEX IF NOT EXISTS idx_signals_bar_time ON signals(bar_time)",
    "CREATE INDEX IF NOT EXISTS idx_signals_signal_time ON signals(signal_time)",
    "CREATE INDEX IF NOT EXISTS idx_signals_executed ON signals(executed)",
)

SIGNAL_EXECUTION_DDL = """
CREATE TABLE IF NOT EXISTS signal_execution (
    account_id       TEXT NOT NULL,
    signal_id        TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'READY'
                     CHECK (status IN (
                         'READY', 'CLAIMED', 'SUBMITTED', 'PARTIALLY_FILLED',
                         'FILLED', 'REJECTED', 'EXPIRED', 'CANCELLED',
                         'RECONCILIATION_REQUIRED'
                     )),
    attempt_count    INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    lease_owner      TEXT,
    lease_until_ms   INTEGER,
    client_order_id  TEXT,
    exchange_order_id TEXT,
    last_error       TEXT,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (account_id, signal_id),
    FOREIGN KEY (signal_id) REFERENCES signals(signal_id)
        ON UPDATE CASCADE ON DELETE RESTRICT
)
"""

SIGNAL_EXECUTION_INDEXES = (
    """
    CREATE INDEX IF NOT EXISTS idx_signal_execution_claim
    ON signal_execution(account_id, status, lease_until_ms)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_signal_execution_client_order
    ON signal_execution(account_id, client_order_id)
    WHERE client_order_id IS NOT NULL
    """,
)

BOT_STATE_DDL = """
CREATE TABLE IF NOT EXISTS bot_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

INSERT_SQL = """
INSERT OR IGNORE INTO signals (
    signal_id, schema_version, symbol, interval, timeframe, bar_time,
    signal_time, event, action, signal, side, position_id, scheduled,
    reason, price, ema_fast, ema_slow, adx_entry, executed, created_at, payload
) VALUES (
    :signal_id, :schema_version, :symbol, :interval, :timeframe, :bar_time,
    :signal_time, :event, :action, :signal, :side, :position_id, :scheduled,
    :reason, :price, :ema_fast, :ema_slow, :adx_entry, :executed,
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
        self.conn = connect_sqlite(
            self.path,
            synchronous="FULL",
            ensure_wal=True,
        )
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            # Signals and the live_since marker are important recovery state.
            self.conn.execute(SIGNALS_DDL)
            self._migrate_signal_contract()
            self.conn.execute(BOT_STATE_DDL)
            for ddl in SIGNALS_INDEXES:
                self.conn.execute(ddl)
            self.conn.execute(SIGNAL_EXECUTION_DDL)
            for ddl in SIGNAL_EXECUTION_INDEXES:
                self.conn.execute(ddl)
            self.conn.commit()
        except Exception:
            self.conn.close()
            self.conn = None
            raise
        logger.info("signal.db listo en %s", self.path)

    def _migrate_signal_contract(self) -> None:
        """Migra señales previas al contrato v2 sin descartar filas."""
        version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if version >= SIGNAL_SCHEMA_VERSION:
            return

        columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(signals)")
        }
        additions = {
            "signal_id": "TEXT",
            "schema_version": "INTEGER NOT NULL DEFAULT 2",
            "action": "TEXT",
            "position_id": "INTEGER",
            "scheduled": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, declaration in additions.items():
            if name not in columns:
                self.conn.execute(
                    f"ALTER TABLE signals ADD COLUMN {name} {declaration}"
                )

        # El índice antiguo no incluía position_id. La clave v2 lo sustituye.
        self.conn.execute("DROP INDEX IF EXISTS ux_signals_event")
        self.conn.execute("DROP INDEX IF EXISTS ux_signals_signal_id")
        rows = self.conn.execute(
            """
            SELECT id, symbol, interval, timeframe, bar_time, signal_time,
                   event, signal, reason, payload, action, position_id, scheduled
            FROM signals
            """
        ).fetchall()
        for row in rows:
            (
                row_id, symbol, interval, timeframe, bar_time, signal_time,
                event_type, signal, reason, raw_payload, action, position_id,
                scheduled,
            ) = row
            payload = _payload_dict(raw_payload)
            event = {
                "symbol": symbol,
                "interval": interval,
                "timeframe": timeframe,
                "bar_time": bar_time,
                "signal_time": signal_time,
                "event": event_type,
                "signal": signal,
                "reason": reason,
                "payload": payload,
                "action": action or _action_for({"event": event_type}, payload),
                "position_id": (
                    position_id
                    if position_id is not None
                    else _position_id_for({}, payload)
                ),
            }
            is_scheduled = int(bool(scheduled or payload.get("pending", False)))
            self.conn.execute(
                """
                UPDATE signals
                SET signal_id = ?, schema_version = ?, action = ?,
                    position_id = ?, scheduled = ?
                WHERE id = ?
                """,
                (
                    build_signal_id(event),
                    SIGNAL_SCHEMA_VERSION,
                    event["action"],
                    event["position_id"],
                    is_scheduled,
                    row_id,
                ),
            )
        self.conn.execute(f"PRAGMA user_version = {SIGNAL_SCHEMA_VERSION}")

    @staticmethod
    def _normalize_event(event: dict) -> dict:
        payload = _payload_dict(event.get("payload"))
        event_type = str(event.get("event", "")).upper()
        if event_type not in {"ENTRY", "EXIT"}:
            raise ValueError(f"event no válido: {event_type!r}")

        signal = str(event.get("signal") or event.get("side") or "").upper()
        side = str(event.get("side") or signal).upper()
        if signal not in {"LONG", "SHORT"} or side != signal:
            raise ValueError("signal y side deben ser LONG/SHORT e iguales")

        action = _action_for(event, payload)
        if action not in {"OPEN", "CLOSE", "PARTIAL_CLOSE"}:
            raise ValueError(f"action no válida: {action!r}")
        if (event_type == "ENTRY") != (action == "OPEN"):
            raise ValueError("ENTRY debe ser OPEN y EXIT debe ser CLOSE/PARTIAL_CLOSE")
        if event.get("action") and payload.get("action") \
                and str(event["action"]).upper() != str(payload["action"]).upper():
            raise ValueError("action no coincide con payload.action")

        position_id = _position_id_for(event, payload)
        if position_id is not None and position_id <= 0:
            raise ValueError("position_id debe ser un entero positivo")
        if event.get("position_id") is not None \
                and payload.get("position_id") is not None \
                and int(event["position_id"]) != int(payload["position_id"]):
            raise ValueError("position_id no coincide con payload.position_id")

        scheduled_value = event.get("scheduled", payload.get("pending", False))
        if scheduled_value not in (False, True, 0, 1):
            raise ValueError("scheduled debe ser booleano")
        created_at = str(event.get("created_at", "")).strip()
        if not created_at:
            raise ValueError("created_at es obligatorio")

        normalized = dict(event)
        normalized.update(
            {
                "schema_version": SIGNAL_SCHEMA_VERSION,
                "event": event_type,
                "action": action,
                "signal": signal,
                "side": side,
                "position_id": position_id,
                "scheduled": int(bool(scheduled_value)),
                "symbol": str(event.get("symbol", "")).strip().upper(),
                "interval": str(event.get("interval", "")).strip(),
                "timeframe": str(event.get("timeframe", "")).strip(),
                "reason": str(event.get("reason", "")).strip(),
                "bar_time": int(event["bar_time"]),
                "signal_time": int(event["signal_time"]),
                "created_at": created_at,
                "price": opt_float(event.get("price")),
                "ema_fast": opt_float(event.get("ema_fast")),
                "ema_slow": opt_float(event.get("ema_slow")),
                "adx_entry": opt_float(event.get("adx_entry")),
                "executed": 0,
            }
        )
        if not all(
            normalized[name]
            for name in ("symbol", "interval", "timeframe", "reason")
        ):
            raise ValueError("symbol, interval, timeframe y reason son obligatorios")
        if normalized["bar_time"] < 0 or normalized["signal_time"] < normalized["bar_time"]:
            raise ValueError("timestamps inválidos: signal_time debe ser >= bar_time")

        payload["action"] = action
        if position_id is not None:
            payload["position_id"] = position_id
        if "pending" in payload or scheduled_value:
            payload["pending"] = bool(scheduled_value)
        normalized["payload"] = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        normalized["signal_id"] = build_signal_id(normalized)
        return normalized

    def insert(self, events: list[dict]) -> int:
        if not events or self.conn is None:
            return 0
        rows = [self._normalize_event(event) for event in events]
        try:
            cursor = self.conn.executemany(INSERT_SQL, rows)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
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
        self.conn = connect_sqlite(
            self.path,
            read_only=True,
            query_only=True,
        )

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
