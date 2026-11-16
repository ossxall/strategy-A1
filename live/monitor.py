"""Launcher directo del servicio ``live/monitor``."""

import importlib
from pathlib import Path

from env_profiles import load_direct_profile

LIVE_DIR = Path(__file__).resolve().parent
load_direct_profile(LIVE_DIR)

try:
    importlib.import_module("yaml")
    importlib.import_module("aiogram")
except ModuleNotFoundError as exc:
    raise SystemExit(
        "Faltan dependencias del monitor. Desde live/, ejecuta: "
        "python -m pip install -r monitor/requirements.txt"
    ) from exc

main = importlib.import_module("monitor.__main__").main


if __name__ == "__main__":
    main()
