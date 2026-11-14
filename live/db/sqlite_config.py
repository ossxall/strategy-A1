"""Apertura y opciones comunes de SQLite para los servicios de ``live``."""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB_BUSY_TIMEOUT_MS = 5_000
DB_JOURNAL_SIZE_LIMIT = 67_108_864
DB_WAL_AUTOCHECKPOINT = 1_000
_SYNCHRONOUS_MODES = {"OFF", "NORMAL", "FULL", "EXTRA"}


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
