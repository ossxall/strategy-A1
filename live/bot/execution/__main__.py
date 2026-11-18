"""Entrypoint del worker de ejecución live."""

from pathlib import Path

try:
    from ..env import load_direct_profile
except ImportError:
    from bot.env import load_direct_profile

load_direct_profile(Path(__file__).resolve().parents[2], service="executor")

from .worker import main


if __name__ == "__main__":
    raise SystemExit(main())
