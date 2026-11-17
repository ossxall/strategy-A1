#!/usr/bin/env python3
"""Barrido exhaustivo de combinaciones finitas para strategy.config.Config."""

from __future__ import annotations

import argparse
from dataclasses import asdict, fields, replace
from datetime import datetime
import itertools
import json
import math
from pathlib import Path
import sys
from typing import Any, Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from strategy.config import Config
from strategy.report import calculate_metrics, jsonable
from strategy.strategy import Backtester, EMACrossStrategy
from strategy.utils import load_parquet, to_timeframe

DEFAULT_DATASET = PROJECT_ROOT / "dataset/binance/BTCUSDT/merged/BTCUSDT-1m.parquet"
DEFAULT_GRID = Path(__file__).with_name("grid.json")
DEFAULT_OUTPUT = Path(__file__).with_name("results")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Prueba todas las combinaciones del grid finito indicado. "
            "Los parámetros ausentes del grid conservan Config() por defecto."
        )
    )
    parser.add_argument("--grid", type=Path, default=DEFAULT_GRID, help="JSON con listas de candidatos.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET, help="Dataset OHLCV Parquet.")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Directorio padre para los resultados (se crea un subdirectorio por ejecución).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Cuenta combinaciones válidas sin ejecutar backtests.")
    parser.add_argument(
        "--max-combinations",
        type=int,
        default=10_000,
        help="Límite de seguridad; usa 0 para no imponer límite.",
    )
    return parser.parse_args()


def load_grid(path: Path) -> dict[str, list[Any]]:
    try:
        grid = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"No se pudo leer el grid {path}: {exc}") from exc
    if not isinstance(grid, dict):
        raise ValueError("El grid debe ser un objeto JSON de parámetro: lista de candidatos.")

    known = {item.name for item in fields(Config)}
    unknown = sorted(set(grid) - known)
    if unknown:
        raise ValueError(f"Parámetros que no existen en Config: {', '.join(unknown)}")

    for name, values in grid.items():
        if not isinstance(values, list) or not values:
            raise ValueError(f"El parámetro {name!r} debe tener una lista no vacía de candidatos.")
    return grid


def iter_configs(
    base: Config, grid: dict[str, list[Any]]
) -> Iterator[tuple[dict[str, Any], Config | None, str | None]]:
    """Genera cada combinación; informa por separado las que Config rechaza."""
    names = list(grid)
    value_sets = [grid[name] for name in names]
    combinations = itertools.product(*value_sets) if names else [()]

    for values in combinations:
        params = dict(zip(names, values))
        try:
            yield params, replace(base, **params), None
        except (TypeError, ValueError) as exc:
            yield params, None, str(exc)


def rank_results(rows: list[dict[str, Any]], output_dir: Path) -> None:
    import pandas as pd

    successful = [row for row in rows if row.get("status") == "ok"]
    if not successful:
        print("[WARN] No hubo backtests válidos; revisa failed.jsonl.")
        return

    results = pd.DataFrame(successful)
    # max_drawdown_pct es negativo: ordenar descendente pone -3% antes que -8%.
    by_objective = results.sort_values(
        ["final_equity", "max_drawdown_pct", "win_rate_pct"],
        ascending=[False, False, False],
        kind="stable",
    ).reset_index(drop=True)
    by_objective.insert(0, "rank", range(1, len(by_objective) + 1))
    by_objective.to_csv(output_dir / "results_by_equity_drawdown.csv", index=False)

    by_win_rate = results.sort_values(
        ["win_rate_pct", "final_equity", "max_drawdown_pct"],
        ascending=[False, False, False],
        kind="stable",
    ).reset_index(drop=True)
    by_win_rate.insert(0, "rank", range(1, len(by_win_rate) + 1))
    by_win_rate.to_csv(output_dir / "results_by_win_rate.csv", index=False)

    best = by_objective.iloc[0]
    print(
        "[BEST equity/drawdown] "
        f"equity={best['final_equity']:.2f} | "
        f"drawdown={best['max_drawdown_pct']:.2f}% | "
        f"win_rate={best['win_rate_pct']:.2f}%"
    )
    print(f"[OUT] Rankings guardados en {output_dir}")


def main() -> int:
    args = parse_args()
    if args.max_combinations < 0:
        raise ValueError("--max-combinations debe ser >= 0.")

    grid = load_grid(args.grid)
    candidate_count = math.prod(len(values) for values in grid.values()) if grid else 1
    if args.max_combinations and candidate_count > args.max_combinations:
        raise ValueError(
            f"El grid tiene {candidate_count:,} combinaciones, por encima del límite "
            f"{args.max_combinations:,}. Reduce el grid o pasa --max-combinations 0."
        )

    base = Config()
    if args.dry_run:
        valid = invalid = 0
        for _, cfg, _ in iter_configs(base, grid):
            if cfg is None:
                invalid += 1
            else:
                valid += 1
        print(
            f"[DRY RUN] candidatos={candidate_count:,} | "
            f"válidos={valid:,} | rechazados por Config={invalid:,}"
        )
        return 0

    dataset = args.dataset if args.dataset.is_absolute() else PROJECT_ROOT / args.dataset
    if not dataset.is_file():
        raise FileNotFoundError(f"No se encontró el dataset: {dataset}")

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    output_dir = args.output / f"run_{run_id}"
    if not output_dir.is_absolute():
        output_dir = PROJECT_ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=False)
    journal_path = output_dir / "results.jsonl"
    failed_path = output_dir / "failed.jsonl"

    raw = load_parquet(dataset)
    bars_cache: dict[str, Any] = {}
    completed: list[dict[str, Any]] = []
    invalid_count = 0

    with journal_path.open("w", encoding="utf-8") as journal, failed_path.open(
        "w", encoding="utf-8"
    ) as failures:
        for index, (params, cfg, config_error) in enumerate(iter_configs(base, grid), start=1):
            if cfg is None:
                invalid_count += 1
                record = {"index": index, "params": jsonable(params), "error": config_error}
                failures.write(json.dumps(record, ensure_ascii=False) + "\n")
                continue

            try:
                if cfg.timeframe not in bars_cache:
                    bars_cache[cfg.timeframe] = to_timeframe(raw, cfg)
                bars = bars_cache[cfg.timeframe]
                if len(bars) < cfg.ema_slow + 2:
                    raise ValueError(
                        f"Dataset insuficiente para EMA{cfg.ema_slow}: {len(bars)} velas."
                    )

                strategy = EMACrossStrategy(cfg)
                signals = strategy.generate_signals(bars, raw)
                adx_exit, weak_exit, rsi_exit = strategy.prepare_exits(raw, bars)
                trades, equity = Backtester(
                    bars,
                    cfg,
                    signals,
                    adx_exit,
                    weak_exit,
                    rsi_exit,
                    verbose=False,
                ).run()
                metrics = calculate_metrics(trades, equity, cfg.initial_capital)
                record = {
                    "index": index,
                    "status": "ok",
                    **asdict(cfg),
                    **metrics,
                }
                record = jsonable(record)
                completed.append(record)
                journal.write(json.dumps(record, ensure_ascii=False) + "\n")
                journal.flush()
                print(
                    f"[{index}/{candidate_count}] "
                    f"equity={metrics['final_equity']:.2f} "
                    f"DD={metrics['max_drawdown_pct']:.2f}% "
                    f"WR={metrics['win_rate_pct']:.2f}% | {params}",
                    flush=True,
                )
            except Exception as exc:
                record = {
                    "index": index,
                    "params": jsonable(params),
                    "error": f"{type(exc).__name__}: {exc}",
                }
                failures.write(json.dumps(record, ensure_ascii=False) + "\n")
                failures.flush()
                print(f"[ERROR {index}/{candidate_count}] {record['error']}", file=sys.stderr, flush=True)

    rank_results(completed, output_dir)
    print(
        f"[DONE] combinaciones={candidate_count:,} | "
        f"Config inválidas={invalid_count:,} | "
        f"backtests correctos={len(completed):,} | "
        f"fallidos={candidate_count - invalid_count - len(completed):,}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2)
