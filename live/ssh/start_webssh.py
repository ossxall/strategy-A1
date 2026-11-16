#!/usr/bin/env python3
"""Instala y arranca WebSSH en un entorno virtual local."""

import os
from pathlib import Path
import subprocess
import sys


SCRIPT_DIR = Path(__file__).resolve().parent
VENV_DIR = SCRIPT_DIR / ".venv"
VENV_PYTHON = VENV_DIR / "bin" / "python"
WSSH = VENV_DIR / "bin" / "wssh"


def main() -> None:
    if sys.version_info < (3, 10):
        raise SystemExit("WebSSH requiere Python 3.10 o superior.")

    if not VENV_PYTHON.is_file():
        subprocess.run(
            [sys.executable, "-m", "venv", str(VENV_DIR)],
            check=True,
        )

    if not WSSH.is_file():
        subprocess.run(
            [
                str(VENV_PYTHON),
                "-m",
                "pip",
                "install",
                "-r",
                str(SCRIPT_DIR / "requirements.txt"),
            ],
            check=True,
        )

    host = os.environ.get("WEBSSH_HOST", "127.0.0.1")
    port = os.environ.get("WEBSSH_PORT", "8888")
    policy = os.environ.get("WEBSSH_POLICY", "reject")

    print(
        f"WebSSH escuchando en http://{host}:{port} (Ctrl+C para detener)",
        flush=True,
    )
    os.execv(
        str(WSSH),
        [
            str(WSSH),
            f"--address={host}",
            f"--port={port}",
            f"--policy={policy}",
        ],
    )


if __name__ == "__main__":
    main()
