"""
Healthchecks de los contenedores de bot1
========================================

Se ejecuta dentro de cada contenedor (imagen con Python) y devuelve 0 (sano)
o 1 (no sano). Solo usa la librería estándar, así que es rápido.

Modos:
    python healthcheck.py feed   # feed.py: BD fresca y collector activo
    python healthcheck.py bot    # paquete bot: heartbeat reciente
    python healthcheck.py executor # consumidor de señales: heartbeat reciente

Variables:
    FEED_DB_PATH           ruta a feed.db (mode feed; fallback DB_PATH)
    FEED_INTERVAL          intervalo del feed, p.ej. 1m (fallback INTERVAL)
    BOT_HEARTBEAT          ruta del archivo de heartbeat (mode bot)
    BOT_EXECUTOR_HEARTBEAT ruta del archivo de heartbeat (mode executor)
    BOT_HEALTHCHECK_MAX_AGE segundos máximos de antigüedad (mode bot)
    BOT_EXECUTOR_HEALTHCHECK_MAX_AGE segundos máximos (mode executor)
    BOT_EXECUTOR_ACCOUNT_HEALTH_MAX_AGE segundos máximos del snapshot de cuenta
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86_400, "w": 604_800}
LIVE_DATA_DIR = Path(__file__).resolve().parent / "data"

try:
    from .db.sqlite_config import connect_sqlite
except ImportError:  # ejecución directa como ``python healthcheck.py``
    from db.sqlite_config import connect_sqlite


def interval_seconds(raw: str | None, default: float = 60.0) -> float:
    """Convierte un intervalo estilo Binance ('1m', '1h', '1d') a segundos."""
    value = (raw or "").strip().lower()
    if not value:
        return default
    try:
        unit = value[-1]
        amount = int(value[:-1])
    except (ValueError, IndexError):
        return default
    factor = _UNIT_SECONDS.get(unit)
    if factor is None or amount <= 0:
        return default
    return amount * factor


def check_feed() -> bool:
    """Sano si el colector está corriendo y la última vela es reciente."""
    db_path = Path(
        os.getenv(
            "FEED_DB_PATH",
            os.getenv("DB_PATH", str(LIVE_DATA_DIR / "feed.db")),
        )
    )
    if not db_path.exists():
        print(f"[feed] feed.db aún no existe: {db_path}")
        return False

    try:
        conn = connect_sqlite(
            db_path,
            read_only=True,
            query_only=True,
        )
        try:
            row = conn.execute(
                "SELECT value FROM collector_state WHERE key = 'collector_status'"
            ).fetchone()
            status = row[0] if row else None
            latest = conn.execute("SELECT MAX(open_time) FROM candles").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"[feed] error leyendo {db_path}: {exc}")
        return False

    interval_ms = interval_seconds(
        os.getenv("FEED_INTERVAL", os.getenv("INTERVAL", "1m"))
    ) * 1000
    max_age_ms = max(300_000, 5 * interval_ms)  # al menos 5 min
    age_ms = None if latest is None else (time.time() * 1000 - latest)

    age_txt = "n/a" if age_ms is None else f"{age_ms / 1000:.0f}s"
    print(
        f"[feed] status={status} vela_mas_reciente_hace={age_txt} "
        f"limite={max_age_ms / 1000:.0f}s"
    )
    return status == "running" and age_ms is not None and age_ms <= max_age_ms


def check_bot() -> bool:
    """Sano si el bot actualizó su heartbeat hace poco."""
    heartbeat = Path(
        os.getenv("BOT_HEARTBEAT", str(LIVE_DATA_DIR / "bot1.heartbeat"))
    )
    max_age = float(os.getenv("BOT_HEALTHCHECK_MAX_AGE", "180"))
    if not heartbeat.exists():
        print(f"[bot] heartbeat aún no existe: {heartbeat}")
        return False
    age = time.time() - heartbeat.stat().st_mtime
    print(f"[bot] heartbeat_hace={age:.1f}s limite={max_age:.0f}s")
    return age <= max_age


def check_executor() -> bool:
    """Sano si el consumidor de señales actualiza su heartbeat."""
    heartbeat = Path(
        os.getenv(
            "BOT_EXECUTOR_HEARTBEAT",
            str(LIVE_DATA_DIR / "executor.heartbeat"),
        )
    )
    max_age = float(os.getenv("BOT_EXECUTOR_HEALTHCHECK_MAX_AGE", "60"))
    if not heartbeat.exists():
        print(f"[executor] heartbeat aún no existe: {heartbeat}")
        return False
    age = time.time() - heartbeat.stat().st_mtime
    print(
        f"[executor] heartbeat_hace={age:.1f}s limite={max_age:.0f}s "
        f"trading_enabled={os.getenv('BOT_BINANCE_TRADING_ENABLED', 'false')}"
    )
    if age > max_age:
        return False
    if os.getenv("BOT_BINANCE_TRADING_ENABLED", "false").strip().lower() not in {
        "1", "true", "yes", "y", "on"
    }:
        return True

    db_path = Path(
        os.getenv("BOT_SIGNAL_DB", str(LIVE_DATA_DIR / "signal.db"))
    )
    environment = os.getenv("BOT_BINANCE_ENVIRONMENT", "testnet").strip().lower()
    account_id = os.getenv("BOT_BINANCE_ACCOUNT_ID", f"binance-{environment}")
    try:
        conn = connect_sqlite(db_path, read_only=True, query_only=True)
        try:
            row = conn.execute(
                """
                SELECT environment, observed_at_ms, last_error
                FROM binance_account_snapshot
                WHERE account_id = ?
                """,
                (account_id,),
            ).fetchone()
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        print(f"[executor] error leyendo snapshot: {exc}")
        return False
    if row is None:
        print(f"[executor] falta snapshot de cuenta {account_id}")
        return False
    snapshot_env, observed_at_ms, last_error = row
    snapshot_age = time.time() * 1000 - int(observed_at_ms)
    snapshot_limit = float(
        os.getenv("BOT_EXECUTOR_ACCOUNT_HEALTH_MAX_AGE", "60")
    ) * 1000
    healthy = (
        snapshot_env == environment
        and last_error is None
        and 0 <= snapshot_age <= snapshot_limit
    )
    print(
        f"[executor] account={account_id} environment={snapshot_env} "
        f"snapshot_hace={snapshot_age / 1000:.1f}s "
        f"error={last_error or 'none'}"
    )
    return healthy


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else ""
    checks = {
        "feed": check_feed,
        "bot": check_bot,
        "executor": check_executor,
    }
    check = checks.get(mode)
    if check is None:
        print(f"[healthcheck] modo desconocido: '{mode}'. Usa 'feed' o 'bot'.")
        return 2
    return 0 if check() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
