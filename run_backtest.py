#!/usr/bin/env python3
"""Ejecuta el backtest con overrides BACKTEST_* de .env.

Los parámetros de estrategia omitidos conservan los defaults de Config.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
DEFAULT_DATASET = "dataset/binance/BTCUSDT/merged/BTCUSDT-1m.parquet"
TRUE_VALUES = {"true", "yes", "on"}
FALSE_VALUES = {"false", "no", "off"}
ONE_WAY_FLAGS = {
    "equity-csv",
    "no-force-close",
    "no-long",
    "no-output",
    "no-plot",
    "no-short",
    "verbose",
}


def read_env(path: Path = ENV_FILE) -> dict[str, str]:
    """Lee pares KEY=VALUE sencillos, permitiendo comentarios y comillas."""
    if not path.is_file():
        raise FileNotFoundError(
            f"No existe {path}. Crea el archivo con: cp .env.example .env"
        )

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise ValueError(f"Línea {line_number} inválida en {path}: se esperaba KEY=VALUE")

        key, value = (part.strip() for part in line.split("=", 1))
        if not key:
            raise ValueError(f"Línea {line_number} inválida en {path}: clave vacía")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def build_backtest_args(values: dict[str, str]) -> list[str]:
    """Traduce BACKTEST_* a argumentos aceptados por strategy.strategy."""
    dataset = values.get("BACKTEST_DATASET", DEFAULT_DATASET).strip() or DEFAULT_DATASET
    output_dir = values.get("BACKTEST_OUTPUT_DIR", "report/backtest_out").strip() or "report/backtest_out"
    args = [dataset, "--output-dir", output_dir]

    for key, raw_value in values.items():
        if not key.startswith("BACKTEST_") or key in {"BACKTEST_DATASET", "BACKTEST_OUTPUT_DIR"}:
            continue
        name = key.removeprefix("BACKTEST_").lower().replace("_", "-")
        value = raw_value.strip()
        if not value:
            continue

        normalized = value.lower()
        if normalized in TRUE_VALUES | FALSE_VALUES:
            enabled = normalized in TRUE_VALUES
            if name in ONE_WAY_FLAGS:
                if enabled:
                    args.append(f"--{name}")
            else:
                args.append(f"--{name}" if enabled else f"--no-{name}")
        else:
            args.extend((f"--{name}", value))
    return args


def main() -> int:
    try:
        values = read_env()
    except (OSError, ValueError) as exc:
        print(f"Error de configuración: {exc}", file=sys.stderr)
        return 2

    args = build_backtest_args(values)
    dataset = Path(args[0])
    if not dataset.is_absolute():
        dataset = ROOT / dataset
    if not dataset.is_file():
        print(f"No se encontró el dataset del backtest: {dataset}", file=sys.stderr)
        return 2

    command = [sys.executable, "-m", "strategy.strategy", *args]
    print(f"Iniciando backtest con {dataset}", flush=True)
    return subprocess.run(command, cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
