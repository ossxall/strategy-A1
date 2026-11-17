#!/usr/bin/env python3
"""Prepara WebSSH y lo arranca para el proxy público /webssh/."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
VENV_DIR = SCRIPT_DIR / ".venv"
VENV_PYTHON = VENV_DIR / "bin" / "python"
REQUIREMENTS = SCRIPT_DIR.parent / "requirements.txt"


def _can_import_webssh(python: str | Path) -> bool:
    result = subprocess.run(
        [str(python), "-c", "import webssh"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _webssh_requirement() -> str:
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        requirement = line.split("#", 1)[0].strip()
        if requirement.lower().startswith("webssh"):
            return requirement
    raise SystemExit(f"No se encontró WebSSH en {REQUIREMENTS}.")


def ensure_webssh() -> str:
    # En Docker, WebSSH ya está instalado en el Python de la imagen.
    # Reutilizarlo evita crear otro entorno virtual durante el arranque.
    if _can_import_webssh(sys.executable):
        return sys.executable

    if not VENV_PYTHON.is_file():
        subprocess.run(
            [sys.executable, "-m", "venv", str(VENV_DIR)],
            check=True,
        )

    if not _can_import_webssh(VENV_PYTHON):
        subprocess.run(
            [
                str(VENV_PYTHON),
                "-m",
                "pip",
                "install",
                _webssh_requirement(),
            ],
            check=True,
        )
    return str(VENV_PYTHON)


def main() -> None:
    if sys.version_info < (3, 10):  # noqa: UP036
        raise SystemExit("WebSSH requiere Python 3.10 o superior.")

    try:
        python = ensure_webssh()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            "No se pudo preparar WebSSH. Comprueba la conexión a Internet "
            "y que Python incluya el módulo venv."
        ) from exc

    host = os.environ.get("WEBSSH_HOST", "127.0.0.1")
    port = os.environ.get("WEBSSH_PORT", "8888")
    policy = os.environ.get("WEBSSH_POLICY", "reject")
    public_url = os.environ.get("WEBSSH_PUBLIC_URL")

    if public_url:
        print(
            f"WebSSH disponible en {public_url} (Ctrl+C para detener)",
            flush=True,
        )
    else:
        print(
            f"WebSSH escuchando en http://{host}:{port}/ "
            "(Ctrl+C para detener)",
            flush=True,
        )
    os.execv(
        python,
        [
            python,
            "-m",
            "webssh.main",
            f"--address={host}",
            f"--port={port}",
            f"--policy={policy}",
        ],
    )


if __name__ == "__main__":
    main()
