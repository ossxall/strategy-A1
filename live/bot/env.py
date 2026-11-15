"""Carga del perfil local para entrypoints ejecutados directamente."""

from __future__ import annotations

import os
from pathlib import Path


def load_direct_profile(live_dir: Path) -> None:
    """Carga .env.dev por defecto o el perfil indicado por APP_ENV."""
    profile = os.getenv("APP_ENV", "dev").strip().lower() or "dev"
    if profile not in {"dev", "prod"}:
        raise ValueError("APP_ENV debe ser dev o prod")

    profile_path = live_dir / f".env.{profile}"
    common_path = live_dir / ".env"
    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        if profile_path.is_file() or common_path.is_file():
            raise RuntimeError(
                "Para cargar el perfil local instala python-dotenv "
                "con: pip install -r live/requirements.txt"
            ) from exc
        return

    # El perfil prevalece sobre el .env común; las variables exportadas en
    # la shell mantienen prioridad por override=False.
    if profile_path.is_file():
        load_dotenv(profile_path, override=False)
    load_dotenv(common_path, override=False)
    load_dotenv(override=False)
