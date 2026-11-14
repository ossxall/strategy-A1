"""Entrypoint del worker de ejecución live."""

from .worker import main


if __name__ == "__main__":
    raise SystemExit(main())
