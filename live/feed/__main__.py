"""Permite ejecutar el colector con `python -m feed`."""

import importlib
from pathlib import Path

from env_profiles import load_direct_profile

load_direct_profile(Path(__file__).resolve().parents[1])

run = importlib.import_module("feed.cli").run

run()
