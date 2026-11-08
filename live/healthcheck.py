"""
Healthchecks de los contenedores de bot1
========================================

Se ejecuta dentro de cada contenedor (imagen con Python) y devuelve 0 (sano)
o 1 (no sano). Solo usa la librería estándar, así que es rápido.

Modos:
    python healthcheck.py feed   # feed.py: BD fresca y collector activo
    python healthcheck.py bot    # bot.py: heartbeat reciente

Variables:
    DB_PATH                ruta a feed.db (mode feed)
    INTERVAL               intervalo del feed, p.ej. 1m (mode feed)
    BOT_HEARTBEAT          ruta del archivo de heartbeat (mode bot)
    HEALTHCHECK_MAX_AGE    segundos máximos de antigüedad (mode bot)
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86_400, "w": 604_800}


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
    db_path = Path(os.getenv("DB_PATH", "/data/feed.db"))
    if not db_path.exists():
        print(f"[feed] feed.db aún no existe: {db_path}")
        return False

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
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

    interval_ms = interval_seconds(os.getenv("INTERVAL", "1m")) * 1000
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
    heartbeat = Path(os.getenv("BOT_HEARTBEAT", "/data/bot1.heartbeat"))
    max_age = float(os.getenv("HEALTHCHECK_MAX_AGE", "180"))
    if not heartbeat.exists():
        print(f"[bot] heartbeat aún no existe: {heartbeat}")
        return False
    age = time.time() - heartbeat.stat().st_mtime
    print(f"[bot] heartbeat_hace={age:.1f}s limite={max_age:.0f}s")
    return age <= max_age


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else ""
    checks = {"feed": check_feed, "bot": check_bot}
    check = checks.get(mode)
    if check is None:
        print(f"[healthcheck] modo desconocido: '{mode}'. Usa 'feed' o 'bot'.")
        return 2
    return 0 if check() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
