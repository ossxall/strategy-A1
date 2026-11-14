"""Entry point con instalación de señales de cierre."""

import signal

from .app import main, request_stop
from .config import logger
from .state import STATE


def run():
    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        STATE.stop_event.set()
        logger.info("Interrupción recibida; salida.")
        raise SystemExit(130)
