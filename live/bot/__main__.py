"""Entrypoint del paquete bot; la configuración se toma de `.env`."""

from __future__ import annotations

import signal
import sys

from .app import main, request_stop
from .config import logger


def run() -> int:
    """Inicia la aplicación y rechaza cualquier argumento de terminal."""
    if len(sys.argv) > 1:
        logger.error("El bot no acepta argumentos; configura los valores en live/.env.")
        return 2

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    try:
        return main()
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(run())
