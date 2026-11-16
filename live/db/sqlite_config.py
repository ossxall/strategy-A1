"""Apertura y opciones comunes de SQLite para los servicios de ``live``."""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB_BUSY_TIMEOUT_MS = 5_000
DB_JOURNAL_SIZE_LIMIT = 67_108_864
DB_WAL_AUTOCHECKPOINT = 1_000
_SYNCHRONOUS_MODES = {"OFF", "NORMAL", "FULL", "EXTRA"}


class SQLiteSchemaError(sqlite3.DatabaseError):
    """Indica que una base SQLite no cumple el contrato de un servicio live."""


def sqlite_user_tables(connection: sqlite3.Connection) -> set[str]:
    """Nombres de tablas de usuario, excluyendo las tablas internas de SQLite."""
    return {
        str(row[0])
        for row in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            """
        )
    }


def validate_no_views_or_triggers(connection: sqlite3.Connection, label: str) -> None:
    objects = connection.execute(
        """
        SELECT type, name FROM sqlite_master
        WHERE type IN ('view', 'trigger')
        """
    ).fetchall()
    if objects:
        raise SQLiteSchemaError(
            f"{label} contiene vistas/triggers no reconocidos: {objects!r}"
        )


def validate_table(
    connection: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
    *,
    primary_key: tuple[str, ...] = (),
    exact_columns: bool = True,
    not_null: dict[str, bool] | None = None,
    defaults: dict[str, str | None] | None = None,
) -> None:
    """Valida nombres/tipos de columnas y, cuando se indica, claves/defaults."""
    quoted_table = table.replace('"', '""')
    rows = connection.execute(f'PRAGMA table_info("{quoted_table}")').fetchall()
    if not rows:
        raise SQLiteSchemaError(f"falta la tabla requerida {table!r}")

    actual = {str(row[1]): row for row in rows}
    expected_names = set(columns)
    actual_names = set(actual)
    missing = sorted(expected_names - actual_names)
    extra = sorted(actual_names - expected_names) if exact_columns else []
    problems = []
    if missing:
        problems.append(f"faltan columnas {missing}")
    if extra:
        problems.append(f"columnas inesperadas {extra}")

    for name in sorted(expected_names & actual_names):
        row = actual[name]
        actual_type = str(row[2] or "").strip().upper()
        expected_type = columns[name].strip().upper()
        if actual_type != expected_type:
            problems.append(
                f"{name}: tipo {actual_type or '<vacío>'}, esperado {expected_type}"
            )
        if not_null is not None and name in not_null:
            if bool(row[3]) != bool(not_null[name]):
                problems.append(
                    f"{name}: NOT NULL={bool(row[3])}, "
                    f"esperado {bool(not_null[name])}"
                )
        if defaults is not None and name in defaults:
            actual_default = row[4]
            if actual_default is not None:
                actual_default = str(actual_default).strip()
            expected_default = defaults[name]
            if actual_default != expected_default:
                problems.append(
                    f"{name}: DEFAULT={actual_default!r}, "
                    f"esperado {expected_default!r}"
                )

    actual_primary_key = tuple(
        str(row[1])
        for row in sorted(rows, key=lambda item: int(item[5]))
        if int(row[5]) > 0
    )
    if actual_primary_key != primary_key:
        problems.append(
            f"PRIMARY KEY={actual_primary_key!r}, esperado {primary_key!r}"
        )
    if problems:
        raise SQLiteSchemaError(
            f"esquema incompatible en {table!r}: " + "; ".join(problems)
        )


def validate_index(
    connection: sqlite3.Connection,
    table: str,
    name: str,
    columns: tuple[str, ...],
    *,
    unique: bool = False,
    partial: bool = False,
    where_contains: str | None = None,
) -> None:
    """Comprueba que un índice requerido conserva nombre, columnas y función."""
    quoted_table = table.replace('"', '""')
    indexes = connection.execute(
        f'PRAGMA index_list("{quoted_table}")'
    ).fetchall()
    row = next((item for item in indexes if str(item[1]) == name), None)
    if row is None:
        raise SQLiteSchemaError(f"falta el índice requerido {name!r} en {table!r}")
    if bool(row[2]) != unique:
        raise SQLiteSchemaError(
            f"índice {name!r}: unique={bool(row[2])}, esperado {unique}"
        )
    if bool(row[4]) != partial:
        raise SQLiteSchemaError(
            f"índice {name!r}: partial={bool(row[4])}, esperado {partial}"
        )
    quoted_index = name.replace('"', '""')
    actual_columns = tuple(
        str(item[2])
        for item in connection.execute(f'PRAGMA index_info("{quoted_index}")')
    )
    if actual_columns != columns:
        raise SQLiteSchemaError(
            f"índice {name!r}: columnas {actual_columns!r}, "
            f"esperadas {columns!r}"
        )
    if where_contains is not None:
        sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (name,),
        ).fetchone()
        actual_sql = "".join(str(sql_row[0] or "").upper().split()) if sql_row else ""
        expected_fragment = "".join(where_contains.upper().split())
        if expected_fragment not in actual_sql:
            raise SQLiteSchemaError(
                f"índice {name!r}: condición WHERE incompatible"
            )


def validate_index_set(
    connection: sqlite3.Connection,
    table: str,
    expected_names: set[str],
) -> None:
    """Rechaza índices adicionales que puedan alterar unicidad o consultas."""
    quoted_table = table.replace('"', '""')
    actual_names = {
        str(row[1])
        for row in connection.execute(f'PRAGMA index_list("{quoted_table}")')
        if not str(row[1]).startswith("sqlite_autoindex_")
    }
    if actual_names != expected_names:
        raise SQLiteSchemaError(
            f"índices incompatibles en {table!r}: "
            f"actuales={sorted(actual_names)!r}, "
            f"esperados={sorted(expected_names)!r}"
        )


def validate_quick_check(connection: sqlite3.Connection, label: str) -> None:
    """Rechaza bases con errores de integridad detectados por SQLite."""
    result = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
    if result != ["ok"]:
        details = "; ".join(result) if result else "sin resultado"
        raise SQLiteSchemaError(f"integridad SQLite inválida en {label}: {details}")


def connect_sqlite(
    database: str | Path,
    *,
    timeout_ms: int = DB_BUSY_TIMEOUT_MS,
    synchronous: str | None = "NORMAL",
    read_only: bool = False,
    query_only: bool = False,
    ensure_wal: bool = False,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    """Abre una conexión y aplica los PRAGMA comunes en esa conexión.

    ``journal_mode`` es persistente en el archivo y se comprueba solo cuando
    ``ensure_wal`` es True. El resto de las opciones de escritura se reaplican
    al abrir cada conexión. Las lecturas conservan su timeout acotado y no
    intentan modificar la base.
    """
    if timeout_ms < 0:
        raise ValueError("timeout_ms no puede ser negativo")
    if synchronous is not None and synchronous.upper() not in _SYNCHRONOUS_MODES:
        raise ValueError(f"Modo synchronous no válido: {synchronous!r}")
    if read_only and ensure_wal:
        raise ValueError("No se puede configurar WAL en una conexión de solo lectura")

    timeout_seconds = timeout_ms / 1000
    if read_only:
        uri = f"{Path(database).expanduser().resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=timeout_seconds,
            check_same_thread=check_same_thread,
        )
    else:
        connection = sqlite3.connect(
            database,
            timeout=timeout_seconds,
            check_same_thread=check_same_thread,
        )

    try:
        connection.execute(f"PRAGMA busy_timeout={int(timeout_ms)}")
        connection.execute("PRAGMA foreign_keys=ON")

        if not read_only:
            if ensure_wal:
                row = connection.execute("PRAGMA journal_mode=WAL").fetchone()
                mode = str(row[0]).lower() if row else ""
                if mode != "wal":
                    raise sqlite3.OperationalError(
                        "No se pudo activar WAL para "
                        f"{database}: modo devuelto {mode!r}"
                    )
            if synchronous is not None:
                connection.execute(f"PRAGMA synchronous={synchronous.upper()}")
            connection.execute(
                f"PRAGMA wal_autocheckpoint={DB_WAL_AUTOCHECKPOINT}"
            )
            connection.execute(
                f"PRAGMA journal_size_limit={DB_JOURNAL_SIZE_LIMIT}"
            )

        if query_only:
            connection.execute("PRAGMA query_only=ON")
        return connection
    except Exception:
        connection.close()
        raise
