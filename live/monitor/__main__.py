"""Inicia el monitor con ``python -m monitor``."""

import asyncio
import logging
from pathlib import Path

try:
    from ..env_profiles import load_direct_profile, validate_prod_environment
except ImportError:
    from env_profiles import load_direct_profile, validate_prod_environment

load_direct_profile(Path(__file__).resolve().parents[1], service="monitor")

from .bot import run


def main() -> None:
    validate_prod_environment("monitor")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
