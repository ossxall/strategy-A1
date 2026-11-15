"""Compatibilidad con la ruta histórica del worker de ejecución.

La implementación vive en :mod:`bot.execution.worker`.
"""

import os
import sys
from pathlib import Path


def _load_direct_profile(live_dir: Path) -> None:
    """Carga el perfil local antes de importar el worker."""
    profile = os.getenv("APP_ENV", "dev").strip().lower() or "dev"
    if profile not in {"dev", "prod"}:
        raise ValueError("APP_ENV debe ser dev o prod")

    profile_path = live_dir / f".env.{profile}"
    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        if profile_path.is_file():
            raise RuntimeError(
                "Para cargar el perfil local instala python-dotenv "
                "con: pip install -r live/requirements.txt"
            ) from exc
        return

    # El perfil se carga antes del .env común, como en Docker Compose.
    # Las variables exportadas en la shell conservan prioridad.
    if profile_path.is_file():
        load_dotenv(profile_path, override=False)
    load_dotenv(live_dir / ".env", override=False)
    load_dotenv(override=False)


if __package__ in {None, ""}:
    # Permite ejecutar este shim desde live/bot o desde el directorio del repo.
    live_dir = Path(__file__).resolve().parents[1]
    if str(live_dir) not in sys.path:
        sys.path.insert(0, str(live_dir))
    _load_direct_profile(live_dir)
    from bot.execution.worker import ExecutionWorker, ExecutorSettings, main
else:
    from .execution.worker import ExecutionWorker, ExecutorSettings, main

__all__ = ["ExecutionWorker", "ExecutorSettings", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
