"""Carga de perfiles de entorno para entrypoints ejecutados directamente."""

from __future__ import annotations

import os
import re
from pathlib import Path


def _load_simple_env(path: Path) -> None:
    """Fallback para perfiles sencillos KEY=VALUE si python-dotenv falta."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith("export "):
            text = text[7:].lstrip()
        key, separator, raw_value = text.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if key in os.environ:
            continue

        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        os.environ[key] = os.path.expandvars(value)


def load_direct_profile(live_dir: Path) -> None:
    """Carga .env.dev por defecto o el perfil indicado por APP_ENV."""
    profile = os.getenv("APP_ENV", "dev").strip().lower() or "dev"
    if profile not in {"dev", "prod"}:
        raise ValueError("APP_ENV debe ser dev o prod")

    profile_path = live_dir / f".env.{profile}"
    common_path = live_dir / ".env"
    try:
        from dotenv import load_dotenv
    except ImportError:
        _load_simple_env(profile_path)
        _load_simple_env(common_path)
        cwd_env = Path.cwd() / ".env"
        if cwd_env.resolve() != common_path.resolve():
            _load_simple_env(cwd_env)
        return

    # El perfil prevalece sobre el .env común; las variables exportadas en
    # la shell mantienen prioridad por override=False.
    if profile_path.is_file():
        load_dotenv(profile_path, override=False)
    load_dotenv(common_path, override=False)
    load_dotenv(override=False)
