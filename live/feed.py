#!/usr/bin/env python3
"""Compatibility launcher para el paquete `feed`."""

import importlib
import sys
from pathlib import Path

LIVE_DIR = Path(__file__).resolve().parent
if str(LIVE_DIR) in sys.path:
    sys.path.remove(str(LIVE_DIR))
sys.path.insert(0, str(LIVE_DIR))

run = importlib.import_module("feed.cli").run


if __name__ == "__main__":
    run()
