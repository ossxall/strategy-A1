#!/usr/bin/env python3
"""Inicia el servidor web de Glances."""

import os
import sys
from pathlib import Path


CONFIG_FILE = Path(__file__).resolve().with_name("glances.conf")


def main() -> None:
    command = [
        sys.executable,
        "-m",
        "glances",
        "-C",
        str(CONFIG_FILE),
        "-w",
        *sys.argv[1:],
    ]
    os.execv(sys.executable, command)


if __name__ == "__main__":
    main()
