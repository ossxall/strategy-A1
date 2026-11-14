"""Arranque y cierre coordinado de los trabajadores del colector."""

import threading
import time

from .config import DB_PATH, INTERVAL, SYMBOL, logger
from .database import database_diagnostics_worker, init_db, set_state
from .rest import initial_backfill_worker, rest_repair_worker
from .state import STATE
from .status import start_status_server
from .websocket import run_websocket_forever


def request_stop(signum=None, frame=None):
    logger.info("Señal de parada recibida.")
    STATE.stop_event.set()
    with STATE.ws_lock:
        ws = STATE.active_ws
    if ws is not None:
        try:
            ws.close()
        except Exception:  # noqa: BLE001 - cierre best-effort
            logger.debug("No se pudo cerrar WebSocket al recibir señal", exc_info=True)


def main():
    STATE.process_started_monotonic = time.monotonic()
    init_db()

    logger.info(
        "Iniciando colector: symbol=%s interval=%s db=%s",
        SYMBOL,
        INTERVAL,
        DB_PATH,
    )

    set_state("collector_status", "running")
    set_state("websocket_status", "starting")

    database_thread = threading.Thread(
        target=database_diagnostics_worker,
        name="database-diagnostics",
        daemon=True,
    )
    STATE.threads["database_diagnostics"] = database_thread
    database_thread.start()

    status_server, status_thread = start_status_server()
    STATE.threads["status_server"] = status_thread

    backfill_thread = threading.Thread(
        target=initial_backfill_worker,
        name="initial-backfill",
        daemon=True,
    )
    repair_thread = threading.Thread(
        target=rest_repair_worker,
        name="rest-repair",
        daemon=True,
    )
    STATE.threads["backfill"] = backfill_thread
    STATE.threads["repair"] = repair_thread
    backfill_thread.start()
    repair_thread.start()

    exit_code = 0
    try:
        recovered = run_websocket_forever()
        if not recovered:
            exit_code = 1
    except KeyboardInterrupt:
        logger.info("Ctrl+C recibido. Cerrando colector...")
        STATE.stop_event.set()
    finally:
        STATE.stop_event.set()

        with STATE.ws_lock:
            ws = STATE.active_ws
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001 - cierre best-effort
                logger.exception("Error cerrando WebSocket")

        backfill_thread.join(timeout=10)
        repair_thread.join(timeout=10)
        database_thread.join(timeout=2)

        set_state("collector_status", "stopped")
        set_state("websocket_status", "stopped")

        status_server.shutdown()
        status_server.server_close()
        status_thread.join(timeout=2)

        logger.info(
            "Colector detenido. Los trabajadores restantes pueden terminar "
            "cuando finalicen sus operaciones bloqueantes."
        )

    return exit_code
