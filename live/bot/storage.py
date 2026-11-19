"""Persistencia de eventos y lectura de velas cerradas."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pandas as pd

from .config import logger, opt_float
from .signal_contract import apply_signal_policy

try:
    from ..feed.database import history_revision_key
except ImportError:  # ejecución como paquete top-level desde live/
    from feed.database import history_revision_key

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

SIGNAL_SCHEMA_VERSION = 4
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
    schema_version INTEGER NOT NULL DEFAULT 4,
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
        raise TypeError("payload debe ser un objeto JSON")
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
        raise TypeError("position_id debe ser un entero")
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
    filled_qty       REAL NOT NULL DEFAULT 0,
    average_fill_price REAL,
    order_status     TEXT,
    result_json      TEXT NOT NULL DEFAULT '{}',
    last_reconciled_at_ms INTEGER,
    last_error       TEXT,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (account_id, signal_id),
    FOREIGN KEY (signal_id) REFERENCES signals(signal_id)
        ON UPDATE CASCADE ON DELETE RESTRICT
)
"""

BINANCE_ACCOUNT_SNAPSHOT_DDL = """
CREATE TABLE IF NOT EXISTS binance_account_snapshot (
    account_id        TEXT PRIMARY KEY,
    environment       TEXT NOT NULL,
    observed_at_ms    INTEGER NOT NULL,
    wallet_balance    REAL,
    available_balance REAL,
    positions_json    TEXT NOT NULL DEFAULT '[]',
    open_orders_json  TEXT NOT NULL DEFAULT '[]',
    last_error        TEXT
)
"""

SIGNAL_CONSUMER_STATE_DDL = """
CREATE TABLE IF NOT EXISTS signal_consumer_state (
    account_id          TEXT PRIMARY KEY,
    activation_cutoff_ms INTEGER NOT NULL,
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

EXECUTION_ADJUSTMENTS_DDL = """
CREATE TABLE IF NOT EXISTS execution_adjustments (
    account_id   TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    position_id  INTEGER NOT NULL,
    adjustment_id TEXT NOT NULL,
    quantity     REAL NOT NULL CHECK (quantity >= 0),
    price        REAL,
    reason       TEXT NOT NULL,
    observed_at_ms INTEGER NOT NULL,
    PRIMARY KEY (account_id, symbol, position_id, adjustment_id)
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

UPGRADE_UNCONSUMED_SIGNAL_SQL = """
UPDATE signals
SET schema_version = :schema_version,
    signal_time = :signal_time,
    action = :action,
    signal = :signal,
    side = :side,
    position_id = :position_id,
    scheduled = :scheduled,
    price = :price,
    ema_fast = :ema_fast,
    ema_slow = :ema_slow,
    adx_entry = :adx_entry,
    created_at = :created_at,
    payload = :payload
WHERE signal_id = :signal_id
  AND schema_version < :schema_version
  AND executed = 0
"""


SIGNAL_TABLES = {
    "signals",
    "signal_execution",
    "bot_state",
    "binance_account_snapshot",
    "signal_consumer_state",
    "execution_adjustments",
}
SIGNAL_COLUMNS = {
    "id": "INTEGER",
    "signal_id": "TEXT",
    "schema_version": "INTEGER",
    "symbol": "TEXT",
    "interval": "TEXT",
    "timeframe": "TEXT",
    "bar_time": "INTEGER",
    "signal_time": "INTEGER",
    "event": "TEXT",
    "action": "TEXT",
    "signal": "TEXT",
    "side": "TEXT",
    "position_id": "INTEGER",
    "scheduled": "INTEGER",
    "reason": "TEXT",
    "price": "REAL",
    "ema_fast": "REAL",
    "ema_slow": "REAL",
    "adx_entry": "REAL",
    "executed": "INTEGER",
    "created_at": "TEXT",
    "payload": "TEXT",
}
SIGNAL_EXECUTION_COLUMNS = {
    "account_id": "TEXT",
    "signal_id": "TEXT",
    "status": "TEXT",
    "attempt_count": "INTEGER",
    "lease_owner": "TEXT",
    "lease_until_ms": "INTEGER",
    "client_order_id": "TEXT",
    "exchange_order_id": "TEXT",
    "filled_qty": "REAL",
    "average_fill_price": "REAL",
    "order_status": "TEXT",
    "result_json": "TEXT",
    "last_reconciled_at_ms": "INTEGER",
    "last_error": "TEXT",
    "created_at": "TEXT",
    "updated_at": "TEXT",
}


def validate_signal_schema(connection) -> None:
    """Valida el contrato SQLite compartido por el bot y el executor."""
    validate_no_views_or_triggers(connection, "signal.db")
    tables = sqlite_user_tables(connection)
    if tables != SIGNAL_TABLES:
        raise SQLiteSchemaError(
            "signal.db contiene tablas "
            f"{sorted(tables)!r}; se requieren exactamente "
            f"{sorted(SIGNAL_TABLES)!r}"
        )

    validate_table(
        connection,
        "signals",
        SIGNAL_COLUMNS,
        primary_key=("id",),
        exact_columns=True,
        not_null={
            "symbol": True,
            "interval": True,
            "timeframe": True,
            "bar_time": True,
            "signal_time": True,
            "event": True,
            "signal": True,
            "reason": True,
            "executed": True,
            "created_at": True,
            "payload": True,
        },
        defaults={
            "scheduled": "0",
            "executed": "0",
            "payload": "'{}'",
        },
    )
    validate_table(
        connection,
        "signal_execution",
        SIGNAL_EXECUTION_COLUMNS,
        primary_key=("account_id", "signal_id"),
        exact_columns=True,
        not_null={
            "account_id": True,
            "signal_id": True,
            "status": True,
            "attempt_count": True,
            "filled_qty": True,
            "result_json": True,
            "created_at": True,
            "updated_at": True,
        },
        defaults={
            "status": "'READY'",
            "attempt_count": "0",
            "filled_qty": "0",
            "result_json": "'{}'",
            "created_at": "CURRENT_TIMESTAMP",
            "updated_at": "CURRENT_TIMESTAMP",
        },
    )
    validate_table(
        connection,
        "bot_state",
        {"key": "TEXT", "value": "TEXT"},
        primary_key=("key",),
        exact_columns=True,
        not_null={"value": True},
    )
    validate_table(
        connection,
        "binance_account_snapshot",
        {
            "account_id": "TEXT",
            "environment": "TEXT",
            "observed_at_ms": "INTEGER",
            "wallet_balance": "REAL",
            "available_balance": "REAL",
            "positions_json": "TEXT",
            "open_orders_json": "TEXT",
            "last_error": "TEXT",
        },
        primary_key=("account_id",),
        exact_columns=True,
        not_null={
            "environment": True,
            "observed_at_ms": True,
            "positions_json": True,
            "open_orders_json": True,
        },
        defaults={
            "positions_json": "'[]'",
            "open_orders_json": "'[]'",
        },
    )
    validate_table(
        connection,
        "signal_consumer_state",
        {
            "account_id": "TEXT",
            "activation_cutoff_ms": "INTEGER",
            "created_at": "TEXT",
        },
        primary_key=("account_id",),
        exact_columns=True,
        not_null={"activation_cutoff_ms": True, "created_at": True},
        defaults={"created_at": "CURRENT_TIMESTAMP"},
    )
    validate_table(
        connection,
        "execution_adjustments",
        {
            "account_id": "TEXT",
            "symbol": "TEXT",
            "position_id": "INTEGER",
            "adjustment_id": "TEXT",
            "quantity": "REAL",
            "price": "REAL",
            "reason": "TEXT",
            "observed_at_ms": "INTEGER",
        },
        primary_key=("account_id", "symbol", "position_id", "adjustment_id"),
        exact_columns=True,
        not_null={
            "account_id": True,
            "symbol": True,
            "position_id": True,
            "adjustment_id": True,
            "quantity": True,
            "reason": True,
            "observed_at_ms": True,
        },
    )

    for name, columns, unique in (
        ("ux_signals_signal_id", ("signal_id",), True),
        ("idx_signals_event_order", ("signal_time", "bar_time", "id"), False),
        (
            "idx_signals_identity",
            (
                "symbol",
                "interval",
                "timeframe",
                "event",
                "action",
                "signal",
                "reason",
                "bar_time",
                "position_id",
            ),
            False,
        ),
        ("idx_signals_bar_time", ("bar_time",), False),
        ("idx_signals_signal_time", ("signal_time",), False),
        ("idx_signals_executed", ("executed",), False),
    ):
        validate_index(connection, "signals", name, columns, unique=unique)
    validate_index(
        connection,
        "signal_execution",
        "idx_signal_execution_claim",
        ("account_id", "status", "lease_until_ms"),
    )
    validate_index(
        connection,
        "signal_execution",
        "ux_signal_execution_client_order",
        ("account_id", "client_order_id"),
        unique=True,
        partial=True,
        where_contains="WHERE client_order_id IS NOT NULL",
    )
    validate_index_set(
        connection,
        "signals",
        {
            "ux_signals_signal_id",
            "idx_signals_event_order",
            "idx_signals_identity",
            "idx_signals_bar_time",
            "idx_signals_signal_time",
            "idx_signals_executed",
        },
    )
    validate_index_set(
        connection,
        "signal_execution",
        {
            "idx_signal_execution_claim",
            "ux_signal_execution_client_order",
        },
    )
    for table in (
        "bot_state",
        "binance_account_snapshot",
        "signal_consumer_state",
        "execution_adjustments",
    ):
        validate_index_set(connection, table, set())

    foreign_keys = connection.execute(
        "PRAGMA foreign_key_list(signal_execution)"
    ).fetchall()
    if foreign_keys:
        actual_foreign_keys = {
            (
                str(row[2]),
                str(row[3]),
                str(row[4]),
                str(row[5]).upper(),
                str(row[6]).upper(),
            )
            for row in foreign_keys
        }
        expected_foreign_keys = {
            ("signals", "signal_id", "signal_id", "CASCADE", "RESTRICT")
        }
        if actual_foreign_keys != expected_foreign_keys:
            raise SQLiteSchemaError(
                "signal_execution tiene claves foráneas incompatibles: "
                f"{sorted(actual_foreign_keys)!r}"
            )
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise SQLiteSchemaError(
            f"signal.db contiene referencias foráneas inválidas: {violations[:5]!r}"
        )
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != SIGNAL_SCHEMA_VERSION:
        raise SQLiteSchemaError(
            "signal.db tiene PRAGMA user_version incompatible; "
            f"se esperaba {SIGNAL_SCHEMA_VERSION} y se encontró {version}"
        )
    validate_quick_check(connection, "signal.db")


def _validate_existing_signal_database(path: Path) -> None:
    """Revisa una base previa en solo lectura antes de migrarla o escribirla."""
    if not path.exists() or path.stat().st_size == 0:
        return
    connection = connect_sqlite(path, read_only=True, query_only=True)
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SIGNAL_SCHEMA_VERSION:
            raise SQLiteSchemaError(
                f"signal.db usa esquema futuro {version}; "
                f"este código solo admite hasta {SIGNAL_SCHEMA_VERSION}"
            )
        tables = sqlite_user_tables(connection)
        unknown = tables - SIGNAL_TABLES
        if unknown:
            raise SQLiteSchemaError(
                f"signal.db tiene tablas desconocidas: {sorted(unknown)!r}"
            )
        if not tables:
            if version != 0:
                raise SQLiteSchemaError(
                    f"signal.db no tiene tablas pero declara user_version={version}"
                )
            validate_quick_check(connection, "signal.db")
            return
        if "signals" not in tables:
            raise SQLiteSchemaError(
                "signal.db contiene tablas del bot/executor pero falta 'signals'"
            )

        # Se permiten las columnas históricas que las migraciones existentes
        # saben actualizar. El contrato actual se exige antes del COMMIT.
        legacy_signal_columns = {
            name: SIGNAL_COLUMNS[name]
            for name in (
                "id",
                "symbol",
                "interval",
                "timeframe",
                "bar_time",
                "signal_time",
                "event",
                "signal",
                "side",
                "reason",
                "price",
                "ema_fast",
                "ema_slow",
                "adx_entry",
                "executed",
                "created_at",
                "payload",
            )
        }
        validate_table(
            connection,
            "signals",
            legacy_signal_columns,
            primary_key=("id",),
            exact_columns=False,
        )
        signal_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(signals)")
        }
        if signal_columns - set(SIGNAL_COLUMNS):
            raise SQLiteSchemaError(
                "signals tiene columnas que esta versión no reconoce: "
                f"{sorted(signal_columns - set(SIGNAL_COLUMNS))!r}"
            )

        if "signal_execution" in tables:
            legacy_execution_columns = {
                name: SIGNAL_EXECUTION_COLUMNS[name]
                for name in (
                    "account_id",
                    "signal_id",
                    "status",
                    "attempt_count",
                    "lease_owner",
                    "lease_until_ms",
                    "client_order_id",
                    "exchange_order_id",
                    "last_error",
                    "created_at",
                    "updated_at",
                )
            }
            validate_table(
                connection,
                "signal_execution",
                legacy_execution_columns,
                primary_key=("account_id", "signal_id"),
                exact_columns=False,
            )
            execution_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(signal_execution)"
                )
            }
            if execution_columns - set(SIGNAL_EXECUTION_COLUMNS):
                raise SQLiteSchemaError(
                    "signal_execution tiene columnas desconocidas: "
                    f"{sorted(execution_columns - set(SIGNAL_EXECUTION_COLUMNS))!r}"
                )
        validate_quick_check(connection, "signal.db")
    finally:
        connection.close()


class SignalStore:
    """Almacén SQLite de eventos idempotentes y estado persistente del bot."""

    def __init__(self, path: Path):
        self.path = path
        self.conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _validate_existing_signal_database(self.path)
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
            self._migrate_execution_contract()
            self.conn.execute(BINANCE_ACCOUNT_SNAPSHOT_DDL)
            self.conn.execute(SIGNAL_CONSUMER_STATE_DDL)
            self.conn.execute(EXECUTION_ADJUSTMENTS_DDL)
            validate_signal_schema(self.conn)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            self.conn.close()
            self.conn = None
            raise
        logger.info("signal.db listo en %s", self.path)

    def _migrate_signal_contract(self) -> None:
        """Migra señales previas al contrato v4 sin descartar filas."""
        version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if version >= SIGNAL_SCHEMA_VERSION:
            return
        if version >= 3:
            # v4 only adds payload-level execution metadata. Keep deployed v3
            # signal IDs and execution foreign keys intact.
            self.conn.execute(f"PRAGMA user_version = {SIGNAL_SCHEMA_VERSION}")
            return

        columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(signals)")
        }
        additions = {
            "signal_id": "TEXT",
            "schema_version": "INTEGER NOT NULL DEFAULT 1",
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
        rows = self.conn.execute(
            """
            SELECT id, symbol, interval, timeframe, bar_time, signal_time,
                   event, signal, reason, payload, action, position_id, scheduled,
                   schema_version
            FROM signals
            """
        ).fetchall()
        for row in rows:
            (
                row_id, symbol, interval, timeframe, bar_time, signal_time,
                event_type, signal, reason, raw_payload, action, position_id,
                scheduled, row_schema_version,
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
                    int(row_schema_version or 1),
                    event["action"],
                    event["position_id"],
                    is_scheduled,
                    row_id,
                ),
            )
        self.conn.execute(f"PRAGMA user_version = {SIGNAL_SCHEMA_VERSION}")

    def _migrate_execution_contract(self) -> None:
        """Añade campos de resultado a ledgers creados por versiones previas."""
        columns = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(signal_execution)")
        }
        additions = {
            "filled_qty": "REAL NOT NULL DEFAULT 0",
            "average_fill_price": "REAL",
            "order_status": "TEXT",
            "result_json": "TEXT NOT NULL DEFAULT '{}'",
            "last_reconciled_at_ms": "INTEGER",
        }
        for name, declaration in additions.items():
            if name not in columns:
                self.conn.execute(
                    f"ALTER TABLE signal_execution ADD COLUMN {name} {declaration}"
                )

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
        reason = str(event.get("reason", "")).strip()
        created_at = str(event.get("created_at", "")).strip()
        if not created_at:
            raise ValueError("created_at es obligatorio")
        payload = apply_signal_policy(event_type, action, reason, payload)

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
                "reason": reason,
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
            inserted = max(cursor.rowcount, 0)
            upgraded = 0
            for row in rows:
                legacy = self.conn.execute(
                    """
                    SELECT schema_version, executed
                    FROM signals WHERE signal_id = ?
                    """,
                    (row["signal_id"],),
                ).fetchone()
                if legacy is None or legacy[0] >= SIGNAL_SCHEMA_VERSION or legacy[1]:
                    continue
                already_consumed = self.conn.execute(
                    """
                    SELECT 1 FROM signal_execution
                    WHERE signal_id = ? LIMIT 1
                    """,
                    (row["signal_id"],),
                ).fetchone()
                if already_consumed:
                    continue
                upgraded += max(
                    self.conn.execute(
                        UPGRADE_UNCONSUMED_SIGNAL_SQL, row
                    ).rowcount,
                    0,
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return inserted + upgraded

    def list_due_signals(self, now_ms: int, limit: int = 100) -> list[dict]:
        """Lee intenciones accionables sin marcarlas como consumidas."""
        if self.conn is None:
            return []
        cursor = self.conn.execute(
            """
            SELECT *
            FROM signals
            WHERE signal_time <= ?
            ORDER BY signal_time,
                     bar_time,
                     CASE WHEN event = 'EXIT' THEN 0 ELSE 1 END,
                     id
            LIMIT ?
            """,
            (int(now_ms), max(1, int(limit))),
        )
        names = [column[0] for column in cursor.description]
        result = []
        for row in cursor.fetchall():
            item = dict(zip(names, row))
            item["payload"] = _payload_dict(item["payload"])
            result.append(item)
        return result

    def initialize_signal_consumer(
        self, account_id: str, now_ms: int, initial_backfill_ms: int
    ) -> int:
        """Fija el corte inicial por cuenta; reinicios conservan el mismo corte."""
        if self.conn is None:
            raise RuntimeError("signal.db no está conectado")
        cutoff = int(now_ms) - max(0, int(initial_backfill_ms))
        self.conn.execute(
            """
            INSERT OR IGNORE INTO signal_consumer_state(
                account_id, activation_cutoff_ms
            ) VALUES (?, ?)
            """,
            (account_id, cutoff),
        )
        self.conn.commit()
        row = self.conn.execute(
            """
            SELECT activation_cutoff_ms
            FROM signal_consumer_state
            WHERE account_id = ?
            """,
            (account_id,),
        ).fetchone()
        return int(row[0])

    def claim_next_signal(
        self,
        account_id: str,
        worker_id: str,
        now_ms: int,
        lease_ms: int,
        activation_cutoff_ms: int | None = None,
    ) -> dict | None:
        """Reclama atómicamente la señal vencida más antigua para una cuenta."""
        if self.conn is None:
            raise RuntimeError("signal.db no está conectado")
        if not account_id.strip() or not worker_id.strip():
            raise ValueError("account_id y worker_id son obligatorios")

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = self.conn.execute(
                """
                SELECT s.*
                FROM signals AS s
                LEFT JOIN signal_execution AS x
                  ON x.account_id = ? AND x.signal_id = s.signal_id
                WHERE s.signal_time <= ?
                  AND (? IS NULL OR s.signal_time >= ?)
                  AND (
                    x.signal_id IS NULL
                    OR x.status = 'READY'
                    OR (
                        x.status = 'CLAIMED'
                        AND COALESCE(x.lease_until_ms, 0) <= ?
                    )
                  )
                ORDER BY s.signal_time,
                         s.bar_time,
                         CASE WHEN s.event = 'EXIT' THEN 0 ELSE 1 END,
                         s.id
                LIMIT 1
                """,
                (
                    account_id,
                    int(now_ms),
                    activation_cutoff_ms,
                    activation_cutoff_ms,
                    int(now_ms),
                ),
            )
            row = cursor.fetchone()
            if row is None:
                self.conn.commit()
                return None

            names = [column[0] for column in cursor.description]
            signal = dict(zip(names, row))
            signal["payload"] = _payload_dict(signal["payload"])
            self.conn.execute(
                """
                INSERT OR IGNORE INTO signal_execution(account_id, signal_id)
                VALUES (?, ?)
                """,
                (account_id, signal["signal_id"]),
            )
            updated = self.conn.execute(
                """
                UPDATE signal_execution
                SET status = 'CLAIMED',
                    attempt_count = attempt_count + 1,
                    lease_owner = ?,
                    lease_until_ms = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE account_id = ? AND signal_id = ?
                  AND (
                    status = 'READY'
                    OR (status = 'CLAIMED' AND COALESCE(lease_until_ms, 0) <= ?)
                  )
                """,
                (
                    worker_id,
                    int(now_ms) + max(1, int(lease_ms)),
                    account_id,
                    signal["signal_id"],
                    int(now_ms),
                ),
            )
            if updated.rowcount != 1:
                self.conn.rollback()
                return None
            self.conn.commit()
            return signal
        except Exception:
            self.conn.rollback()
            raise

    def update_execution(
        self,
        account_id: str,
        signal_id: str,
        status: str,
        *,
        order_status: str | None = None,
        client_order_id: str | None = None,
        exchange_order_id: str | None = None,
        filled_qty: float | None = None,
        average_fill_price: float | None = None,
        result: dict | None = None,
        error: str | None = None,
        reconciled_at_ms: int | None = None,
    ) -> None:
        """Guarda el estado confirmado por el consumidor, nunca por el productor."""
        valid_statuses = {
            "READY",
            "CLAIMED",
            "SUBMITTED",
            "PARTIALLY_FILLED",
            "FILLED",
            "REJECTED",
            "EXPIRED",
            "CANCELLED",
            "RECONCILIATION_REQUIRED",
        }
        if status not in valid_statuses:
            raise ValueError(f"estado de ejecución no válido: {status!r}")
        if self.conn is None:
            raise RuntimeError("signal.db no está conectado")

        result_json = (
            json.dumps(
                result,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            if result is not None
            else None
        )
        self.conn.execute(
            """
            UPDATE signal_execution
            SET status = ?,
                order_status = COALESCE(?, order_status),
                client_order_id = COALESCE(?, client_order_id),
                exchange_order_id = COALESCE(?, exchange_order_id),
                filled_qty = COALESCE(?, filled_qty),
                average_fill_price = COALESCE(?, average_fill_price),
                result_json = COALESCE(?, result_json),
                last_error = ?,
                last_reconciled_at_ms = COALESCE(?, last_reconciled_at_ms),
                lease_owner = NULL,
                lease_until_ms = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE account_id = ? AND signal_id = ?
            """,
            (
                status,
                order_status,
                client_order_id,
                exchange_order_id,
                filled_qty,
                average_fill_price,
                result_json,
                error,
                reconciled_at_ms,
                account_id,
                signal_id,
            ),
        )
        if self.conn.execute("SELECT changes()").fetchone()[0] != 1:
            raise KeyError(
                f"No existe ejecución {account_id!r}/{signal_id!r} para actualizar"
            )
        self.conn.commit()

    def get_execution_state(
        self, account_id: str, signal_id: str
    ) -> dict | None:
        if self.conn is None:
            return None
        row = self.conn.execute(
            """
            SELECT status, client_order_id, order_status, result_json,
                   filled_qty, average_fill_price, last_error
            FROM signal_execution
            WHERE account_id = ? AND signal_id = ?
            """,
            (account_id, signal_id),
        ).fetchone()
        if row is None:
            return None
        return {
            "status": row[0],
            "client_order_id": row[1],
            "order_status": row[2],
            "result": _payload_dict(row[3]),
            "filled_qty": float(row[4] or 0),
            "average_fill_price": row[5],
            "error": row[6],
        }

    def record_order_intent(
        self,
        account_id: str,
        signal_id: str,
        client_order_id: str,
        order_status: str,
        intent: dict,
    ) -> None:
        """Persiste ID/intent antes del POST para resolver caídas sin duplicar."""
        if self.conn is None:
            raise RuntimeError("signal.db no está conectado")
        result_json = json.dumps(
            {"intent": intent},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        self.conn.execute(
            """
            UPDATE signal_execution
            SET client_order_id = ?,
                order_status = ?,
                result_json = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE account_id = ? AND signal_id = ?
            """,
            (
                client_order_id,
                order_status,
                result_json,
                account_id,
                signal_id,
            ),
        )
        if self.conn.execute("SELECT changes()").fetchone()[0] != 1:
            raise KeyError(
                f"No existe ejecución {account_id!r}/{signal_id!r} para preparar"
            )
        self.conn.commit()

    def list_inflight_executions(self, account_id: str) -> list[dict]:
        if self.conn is None:
            return []
        cursor = self.conn.execute(
            """
            SELECT s.*, x.status AS execution_status,
                   x.client_order_id, x.exchange_order_id,
                   x.order_status, x.filled_qty
            FROM signal_execution AS x
            JOIN signals AS s ON s.signal_id = x.signal_id
            WHERE x.account_id = ?
              AND (
                x.status IN (
                    'SUBMITTED', 'PARTIALLY_FILLED', 'RECONCILIATION_REQUIRED'
                )
                OR (x.status = 'CLAIMED' AND x.order_status = 'SUBMITTING')
              )
            ORDER BY s.signal_time, s.bar_time, s.id
            """,
            (account_id,),
        )
        names = [column[0] for column in cursor.description]
        result = []
        for row in cursor.fetchall():
            item = dict(zip(names, row))
            item["payload"] = _payload_dict(item["payload"])
            result.append(item)
        return result

    def get_position_execution_state(
        self, account_id: str, symbol: str, position_id: int
    ) -> dict:
        """Reconstruye la cantidad real contabilizada por este consumidor."""
        if self.conn is None:
            raise RuntimeError("signal.db no está conectado")
        row = self.conn.execute(
            """
            SELECT
                MAX(CASE WHEN s.event = 'ENTRY' THEN s.side END) AS side,
                COALESCE(SUM(
                    CASE WHEN s.event = 'ENTRY' THEN x.filled_qty ELSE 0 END
                ), 0) AS entered_qty,
                COALESCE(SUM(
                    CASE WHEN s.event = 'EXIT' THEN x.filled_qty ELSE 0 END
                ), 0) AS exited_qty,
                COALESCE((
                    SELECT SUM(a.quantity)
                    FROM execution_adjustments AS a
                    WHERE a.account_id = ?
                      AND a.symbol = ?
                      AND a.position_id = ?
                ), 0) AS adjusted_qty,
                SUM(CASE WHEN s.event = 'ENTRY' THEN 1 ELSE 0 END) AS entry_rows
            FROM signals AS s
            JOIN signal_execution AS x ON x.signal_id = s.signal_id
            WHERE x.account_id = ?
              AND s.symbol = ?
              AND s.position_id = ?
              AND x.status IN (
                  'FILLED', 'PARTIALLY_FILLED', 'SUBMITTED',
                  'RECONCILIATION_REQUIRED'
              )
            """,
            (
                account_id,
                symbol,
                int(position_id),
                account_id,
                symbol,
                int(position_id),
            ),
        ).fetchone()
        side, entered, exited, adjusted, entry_rows = row
        entered_qty = float(entered or 0)
        exited_qty = float(exited or 0)
        adjusted_qty = float(adjusted or 0)
        return {
            "side": side,
            "entered_qty": entered_qty,
            "exited_qty": exited_qty,
            "adjusted_qty": adjusted_qty,
            "open_qty": max(0.0, entered_qty - exited_qty - adjusted_qty),
            "has_entry": bool(entry_rows),
        }

    def get_active_execution_positions(self, account_id: str, symbol: str) -> list[dict]:
        if self.conn is None:
            return []
        position_ids = self.conn.execute(
            """
            SELECT DISTINCT s.position_id
            FROM signals AS s
            JOIN signal_execution AS x ON x.signal_id = s.signal_id
            WHERE x.account_id = ?
              AND s.symbol = ?
              AND s.event = 'ENTRY'
              AND s.position_id IS NOT NULL
              AND x.status IN (
                  'FILLED', 'PARTIALLY_FILLED', 'SUBMITTED',
                  'RECONCILIATION_REQUIRED'
              )
            ORDER BY s.position_id
            """,
            (account_id, symbol),
        ).fetchall()
        active = []
        for (position_id,) in position_ids:
            state = self.get_position_execution_state(
                account_id, symbol, int(position_id)
            )
            if state["open_qty"] > 0:
                active.append({"position_id": int(position_id), **state})
        return active

    def save_execution_adjustment(
        self,
        account_id: str,
        symbol: str,
        position_id: int,
        adjustment_id: str,
        quantity: float,
        price: float | None,
        reason: str,
        observed_at_ms: int,
    ) -> bool:
        if self.conn is None:
            raise RuntimeError("signal.db no está conectado")
        cursor = self.conn.execute(
            """
            INSERT INTO execution_adjustments(
                account_id, symbol, position_id, adjustment_id, quantity,
                price, reason, observed_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, symbol, position_id, adjustment_id)
            DO UPDATE SET
                quantity = MAX(execution_adjustments.quantity, excluded.quantity),
                price = COALESCE(excluded.price, execution_adjustments.price),
                observed_at_ms = MAX(
                    execution_adjustments.observed_at_ms,
                    excluded.observed_at_ms
                )
            """,
            (
                account_id,
                symbol,
                int(position_id),
                adjustment_id,
                float(quantity),
                price,
                reason,
                int(observed_at_ms),
            ),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def save_account_snapshot(
        self,
        account_id: str,
        environment: str,
        observed_at_ms: int,
        *,
        wallet_balance: float | None,
        available_balance: float | None,
        positions: list[dict],
        open_orders: list[dict],
        error: str | None = None,
    ) -> None:
        if self.conn is None:
            raise RuntimeError("signal.db no está conectado")
        self.conn.execute(
            """
            INSERT INTO binance_account_snapshot(
                account_id, environment, observed_at_ms, wallet_balance,
                available_balance, positions_json, open_orders_json, last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                environment = excluded.environment,
                observed_at_ms = excluded.observed_at_ms,
                wallet_balance = excluded.wallet_balance,
                available_balance = excluded.available_balance,
                positions_json = excluded.positions_json,
                open_orders_json = excluded.open_orders_json,
                last_error = excluded.last_error
            """,
            (
                account_id,
                environment,
                int(observed_at_ms),
                wallet_balance,
                available_balance,
                json.dumps(positions, sort_keys=True, separators=(",", ":")),
                json.dumps(open_orders, sort_keys=True, separators=(",", ":")),
                error,
            ),
        )
        self.conn.commit()

    def set_account_snapshot_error(
        self, account_id: str, error: str | None
    ) -> None:
        if self.conn is None:
            raise RuntimeError("signal.db no está conectado")
        self.conn.execute(
            """
            UPDATE binance_account_snapshot
            SET last_error = ?
            WHERE account_id = ?
            """,
            (error, account_id),
        )
        self.conn.commit()

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
        SELECT open_time, open, high, low, close, volume,
               COALESCE(
                   (SELECT CAST(value AS INTEGER)
                    FROM collector_state WHERE key=?),
                   0
               ) AS history_revision
        FROM candles
        WHERE symbol = ? AND interval = ? AND is_closed = 1
        ORDER BY open_time
    """

    def __init__(self, path: Path, symbol: str, interval: str):
        self.path = path
        self.symbol = symbol
        self.interval = interval
        self.conn: sqlite3.Connection | None = None
        self.history_revision = 0

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
        try:
            try:
                from ..feed.database import validate_feed_schema
            except ImportError:  # ejecución como paquete top-level desde live/
                from feed.database import validate_feed_schema

            validate_feed_schema(self.conn)
        except Exception:
            self.conn.close()
            self.conn = None
            raise

    def load_closed(self) -> pd.DataFrame:
        """Devuelve las velas cerradas indexadas por timestamp UTC."""
        if self.conn is None:
            self.connect()
        frame = pd.read_sql_query(
            self.QUERY,
            self.conn,
            params=(
                history_revision_key(self.symbol, self.interval),
                self.symbol,
                self.interval,
            ),
        )
        if frame.empty:
            return frame.drop(columns=["history_revision"])
        self.history_revision = int(frame["history_revision"].iloc[0])
        frame = frame.drop(columns=["history_revision"])
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
