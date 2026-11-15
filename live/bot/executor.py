"""Compatibilidad con la ruta histórica del worker de ejecución.

La implementación vive en :mod:`bot.execution.worker`.
"""

import sys
from pathlib import Path

if __package__ in {None, ""}:
    # Permite ejecutar este shim desde live/bot o desde el directorio del repo.
    live_dir = Path(__file__).resolve().parents[1]
    if str(live_dir) not in sys.path:
        sys.path.insert(0, str(live_dir))
    from bot.env_profile import load_direct_profile

    load_direct_profile(live_dir)
    from bot.execution.worker import ExecutionWorker, ExecutorSettings, main
else:
    from .execution.worker import ExecutionWorker, ExecutorSettings, main

__all__ = ["ExecutionWorker", "ExecutorSettings", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
