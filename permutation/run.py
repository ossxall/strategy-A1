#!/usr/bin/env python3
"""Gated strategy-permutation agent using OpenCode GPT-6 Luna for analysis.

No search starts during ``prepare``. A plan must be approved by its exact hash,
unless ``--autonomous`` was explicitly selected. Validation selects and freezes
finalists; the test segment is only run later via the one-shot ``test`` command.
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from hashlib import sha256
import io
import math
import tempfile
from dataclasses import asdict, fields, replace
from datetime import datetime, timezone
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from typing import Any

import numpy as np
import pandas as pd

PERMUTATION_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PERMUTATION_DIR.parent
DEFAULT_GRID = PERMUTATION_DIR / "grid.json"
DEFAULT_POLICY = PERMUTATION_DIR / "config.yaml"
MODEL = "opencode-go/gpt-6-luna"
RUNS_DIR = PERMUTATION_DIR / "runs"
NON_SEARCH_FIELDS = {"initial_capital", "commission_bps", "slippage_bps", "timeframe"}


ANALYST_AGENT = r"""---
description: Read-only reviewer for strategy permutation runs
mode: primary
model: opencode-go/gpt-6-luna
permissions:
  - action: "*"
    resource: "*"
    effect: deny
---

Eres el analista de un agente de optimización. No tienes permiso ni debes usar
herramientas. Analiza únicamente el código y los datos incluidos en el mensaje.
No inventes métricas, no alteres la estrategia original y no cambies score,
umbrales, splits ni costes.

Para TASK: STATIC_AND_TRUNCATION_AUDIT devuelve solo JSON:
{"verdict":"pass|stop|needs_review","code_summary":[],"lookahead_findings":[],"other_risks":[],"parameterization_notes":[]}.
`stop` requiere evidencia concreta de sesgo futuro o bug invalidante; `needs_review`
significa que no puedes verificar un punto material.

Para TASK: RESULTS_REVIEW devuelve solo JSON:
{"observations":[],"anomalies":[],"ranking_changed":false}.
Interpreta solo métricas incluidas. No cambies ranking ni parámetros y nunca
infieras métricas de test.
"""

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from strategy.config import Config
from strategy.report import calculate_metrics, positions_from_trades
from strategy.strategy import Backtester, EMACrossStrategy
from strategy.utils import normalize_columns, to_timeframe



def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def immutable_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in plan.items()
        if key not in {"plan_hash", "status", "approval", "test_evaluated_at"}
    }


def load_policy(path: Path = DEFAULT_POLICY) -> dict[str, Any]:
    """Read config.yaml as YAML when PyYAML exists, otherwise as JSON-subset YAML."""
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        try:
            policy = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Instala PyYAML para leer config.yaml (o conserva el formato JSON, "
                "que también es YAML válido)."
            ) from exc
    else:
        policy = yaml.safe_load(text)
    if not isinstance(policy, dict):
        raise ValueError("La raíz de config.yaml debe ser un mapping.")
    return policy


def load_grid(path: Path) -> dict[str, list[Any]]:
    try:
        grid = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"No se pudo leer el grid {path}: {exc}") from exc
    if not isinstance(grid, dict) or any(
        not isinstance(values, list) or not values for values in grid.values()
    ):
        raise ValueError("El grid debe mapear parámetros a listas no vacías.")
    valid_fields = {item.name for item in fields(Config)}
    unknown = sorted(set(grid) - valid_fields)
    if unknown:
        raise ValueError(f"Campos que no existen en Config: {', '.join(unknown)}")
    forbidden = sorted(set(grid) & NON_SEARCH_FIELDS)
    if forbidden:
        raise ValueError(
            "Capital, costes y temporalidad quedan fijos en el plan; no se pueden "
            f"permutar: {', '.join(forbidden)}"
        )
    for name, values in grid.items():
        unique: list[Any] = []
        seen: set[str] = set()
        for value in values:
            marker = canonical_json(value)
            if marker not in seen:
                seen.add(marker)
                unique.append(value)
        grid[name] = unique
    return grid


def parse_model_json(text: str) -> dict[str, Any]:
    """Parse a model's JSON object without accepting arbitrary surrounding prose."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.I)
    try:
        result = json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("GPT-6 Luna no devolvió un objeto JSON.")
        result = json.loads(stripped[start : end + 1])
    if not isinstance(result, dict):
        raise ValueError("La respuesta de GPT-6 Luna no es un objeto JSON.")
    return result


def call_opencode(prompt: str, *, timeout: int = 300) -> str:
    executable = shutil.which("opencode")
    if not executable:
        raise RuntimeError(
            "No se encontró `opencode`. Instálalo y confirma que "
            "`opencode models` lista opencode-go/gpt-6-luna."
        )
    temp_parent = Path("/tmp/opencode")
    if not temp_parent.is_dir():
        raise RuntimeError("Falta el directorio temporal aislado /tmp/opencode.")
    with tempfile.TemporaryDirectory(
        prefix="permutation-agent-", dir=temp_parent
    ) as isolated:
        workspace = Path(isolated)
        agent_dir = workspace / ".opencode" / "agents"
        agent_dir.mkdir(parents=True)
        agent_file = agent_dir / "permutation-analyst.md"
        agent_file.write_text(ANALYST_AGENT, encoding="utf-8")
        command = [
            executable,
            "run",
            "--standalone",
            "--model",
            MODEL,
            "--agent",
            "permutation-analyst",
            "--title",
            "Auditoría de permutaciones aislada",
            prompt,
        ]
        isolated_env = os.environ.copy()
        # OpenCode's temporary session state is kept beside the ephemeral agent
        # definition and removed with the sandbox. Provider credentials/config
        # are read from the user's normal setup but are never written by us.
        isolated_env["OPENCODE_DB"] = str(workspace / "opencode-session.db")
        completed = subprocess.run(
            command,
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=isolated_env,
        )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"`opencode run` falló ({completed.returncode}): {detail[-3000:]}")
    if not completed.stdout.strip():
        raise RuntimeError("OpenCode terminó sin una respuesta del modelo.")
    return completed.stdout.strip()


def strategy_sources() -> tuple[list[Path], str]:
    names = ("strategy.py", "config.py", "indicators.py", "utils.py", "models.py")
    paths = [PROJECT_ROOT / "strategy" / name for name in names]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Faltan archivos de estrategia: {', '.join(missing)}")
    sections = []
    for path in paths:
        source = path.read_text(encoding="utf-8")
        sections.append(f"\n===== {path.relative_to(PROJECT_ROOT)} =====\n{source}")
    return paths, "\n".join(sections)


def make_cost_config(
    *,
    capital: float,
    timeframe: str,
    commission_bps: float,
    spread_bps: float,
    slippage_bps: float,
) -> tuple[Config, dict[str, Any]]:
    if capital <= 0:
        raise ValueError("El capital inicial debe ser positivo.")
    if min(commission_bps, spread_bps, slippage_bps) < 0:
        raise ValueError("Comisión, spread y slippage no pueden ser negativos.")
    # Backtester applies adverse slippage on both entry and exit. Adding half the
    # quoted bid/ask spread per side models one full spread per round trip.
    effective_slippage = slippage_bps + spread_bps / 2.0
    config = replace(
        Config(),
        initial_capital=capital,
        timeframe=timeframe,
        commission_bps=commission_bps,
        slippage_bps=effective_slippage,
    )
    costs = {
        "commission_bps": commission_bps,
        "spread_bps": spread_bps,
        "slippage_bps": slippage_bps,
        "effective_slippage_bps_in_backtester": effective_slippage,
        "spread_model": "half quoted spread added per side to adverse slippage",
    }
    return config, costs


def get_data_segments(
    raw: pd.DataFrame,
    config: Config,
    policy: dict[str, Any],
) -> tuple[pd.DataFrame, dict[str, dict[str, Any]], int]:
    bars = to_timeframe(raw, config)
    split = policy["split"]
    n = len(bars)
    train_end = int(n * float(split["train"]))
    validation_end = int(n * (float(split["train"]) + float(split["validation"])))
    requested = policy.get("embargo_bars", "auto")
    if requested == "auto":
        embargo = max(
            config.ema_fast,
            config.ema_slow,
            config.macd_slow,
            config.entry_atr_lookback,
            config.rsi_divergence_lookback,
            config.adx_exit_range,
        )
    else:
        embargo = int(requested)
    boundaries = {
        "train": (0, train_end),
        "validation": (min(train_end + embargo, validation_end), validation_end),
        "test": (min(validation_end + embargo, n), n),
    }
    descriptions: dict[str, dict[str, Any]] = {}
    for name, (start, end) in boundaries.items():
        if start >= end:
            descriptions[name] = {
                "start_index": start,
                "end_index_exclusive": end,
                "bars": 0,
                "start_utc": None,
                "end_utc": None,
            }
        else:
            descriptions[name] = {
                "start_index": start,
                "end_index_exclusive": end,
                "bars": end - start,
                "start_utc": bars.index[start].isoformat(),
                "end_utc": bars.index[end - 1].isoformat(),
            }
    return bars, descriptions, embargo


def segment_data(
    raw: pd.DataFrame,
    bars: pd.DataFrame,
    start: int,
    end: int,
) -> pd.DataFrame:
    if start >= end:
        return raw.iloc[0:0]
    start_time = bars.index[start]
    if end < len(bars):
        end_time = bars.index[end]
        return raw.loc[(raw.index >= start_time) & (raw.index < end_time)]
    return raw.loc[raw.index >= start_time]


def candidate_grid(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    names = list(grid)
    return [
        dict(zip(names, combination))
        for combination in itertools.product(*(grid[name] for name in names))
    ] if names else [{}]


def parameter_inventory(grid: dict[str, list[Any]], base: Config) -> list[dict[str, Any]]:
    original = asdict(base)
    inventory = []
    for name, candidates in grid.items():
        ordered = sorted(set(candidates))
        inventory.append({
            "parameter": name,
            "type": type(ordered[0]).__name__,
            "min": ordered[0],
            "max": ordered[-1],
            "step": (
                ordered[1] - ordered[0]
                if len(ordered) > 1 and all(
                    isinstance(value, (int, float)) and not isinstance(value, bool)
                    for value in ordered
                )
                else "discreto"
            ),
            "original": original[name],
            "candidates": candidates,
            "constraints": "Config.__post_init__ valida la combinación.",
        })
    return inventory


def build_audit_prompt(
    source: str,
    quality: dict[str, Any],
    lookahead: dict[str, Any],
    cfg: Config,
) -> str:
    return (
        "TASK: STATIC_AND_TRUNCATION_AUDIT\n"
        "Audita la estrategia indicada para una búsqueda de parámetros. La estrategia "
        "original debe permanecer intacta. El test de señales truncadas fue ejecutado "
        "por Python; trata sus resultados como evidencia, no inventes métricas.\n"
        f"CONFIG_ORIGINAL_WITH_APPROVED_COSTS:\n{json.dumps(asdict(cfg), ensure_ascii=False)}\n"
        f"DATA_QUALITY:\n{json.dumps(quality, ensure_ascii=False)}\n"
        f"PYTHON_LOOKAHEAD_CHECK:\n{json.dumps(lookahead, ensure_ascii=False)}\n"
        "CODE:\n"
        f"{source}\n"
        "Responde exclusivamente con el JSON definido por tu prompt de sistema. "
        "Detente si el código contiene una fuga probable no cubierta por el test."
    )


def make_plan_markdown(plan: dict[str, Any]) -> str:
    lines = [
        "# Plan de optimización — aprobación requerida",
        "",
        f"- Run ID: `{plan['run_id']}`",
        f"- Hash del plan: `{plan['plan_hash']}`",
        f"- Estado: **{plan['status']}**",
        f"- Modelo de auditoría: `{plan['model']}`",
        f"- Dataset SHA-256: `{plan['dataset']['sha256']}`",
        f"- Búsqueda: {plan['space_size']:,} combinaciones candidatas",
        f"- Split cronológico: train/validation/test; embargo {plan['embargo_bars']} barras.",
        "",
        "## Datos, costes y supuestos",
        "",
        f"- Instrumento: {plan['inputs']['instrument']}",
        f"- Fuente declarada: {plan['inputs']['source']}",
        f"- Temporalidad de estrategia: {plan['inputs']['timeframe']}",
        f"- Capital inicial: {plan['inputs']['capital']}",
        f"- Apalancamiento: {plan['inputs']['leverage']}x",
        f"- Tamaño de contrato: {plan['inputs']['contract_size']}",
        f"- Costes: `{json.dumps(plan['costs'], ensure_ascii=False)}`",
        "- El spread se convierte en medio spread por lado y se suma al slippage adverso.",
        "- El motor de backtest existente es cash-only; no se admite apalancamiento ni contratos distintos de 1.",
        "",
        "## Calidad de datos",
        "",
        f"- Barras: {plan['dataset']['bars']:,}",
        f"- Cobertura: {plan['dataset']['start_utc']} → {plan['dataset']['end_utc']} "
        f"({plan['dataset']['years_covered']:.2f} años)",
        f"- Timestamps duplicados: {plan['dataset']['duplicate_timestamps']:,}; "
        f"timestamps inválidos: {plan['dataset']['invalid_timestamps']:,}; "
        f"filas duplicadas/no utilizables removidas: "
        f"{plan['dataset']['duplicate_or_invalid_rows_removed']:,}.",
        f"- Gaps sobre intervalo mediano: {plan['dataset']['gaps_above_median_interval']:,}",
        f"- Cambios absolutos de cierre >15%: {plan['dataset']['absolute_close_changes_over_15pct']:,}",
        *[f"- Advertencia: {warning}" for warning in plan["preflight"]["warnings"]],
        *[f"- BLOQUEO: {issue}" for issue in plan["preflight"]["issues"]],
        "",
        "## Auditoría y control de look-ahead",
        "",
        f"- Test de truncación Python: **{plan['lookahead']['status']}**",
        f"- Auditor GPT-6 Luna: **{plan['model_audit'].get('verdict', 'needs_review')}**",
        "",
        "### Resumen del código",
        "",
        *[f"- {item}" for item in plan["model_audit"].get("code_summary", [])],
        "### Hallazgos de look-ahead",
        "",
        *[
            f"- `{item.get('file', '?')}:{item.get('line_or_symbol', '?')}` — "
            f"{item.get('evidence', '')}"
            for item in plan["model_audit"].get("lookahead_findings", [])
        ],
        "",
        "## Inventario de parámetros",
        "",
        "| Parámetro | Tipo | Mín | Máx | Paso | Original | Candidatos | Restricciones |",
        "|---|---|---:|---:|---:|---:|---|---|",
    ]
    for row in plan["parameter_inventory"]:
        lines.append(
            f"| `{row['parameter']}` | {row['type']} | {row['min']} | {row['max']} | "
            f"{row['step']} | {row['original']} | `{row['candidates']}` | "
            f"{row['constraints']} |"
        )
    lines.extend([
        "",
        "## Splits cronológicos (fechas UTC exactas)",
        "",
        "| Tramo | Barras | Inicio | Fin |",
        "|---|---:|---|---|",
    ])
    for name, segment in plan["splits"].items():
        lines.append(
            f"| {name} | {segment['bars']:,} | {segment['start_utc'] or '—'} | "
            f"{segment['end_utc'] or '—'} |"
        )
    lines.extend([
        "",
        "## Score, umbrales y límites congelados",
        "",
        f"- Score: `{plan['score_definition']}`",
        f"- Filtros duros: `{json.dumps(plan['policy']['hard_filters'], ensure_ascii=False)}`",
        f"- Pruebas/aceptación OOS: `{json.dumps(plan['policy']['oos_acceptance'], ensure_ascii=False)}`",
        f"- Límite: `{json.dumps(plan['policy']['stop'], ensure_ascii=False)}`",
        "",
        "## Aprobación",
        "",
        "No se ejecutará ningún backtest de optimización hasta aprobar explícitamente "
        "este plan. Ejecuta `python permutation/run.py approve --run-id "
        f"{plan['run_id']} --plan-hash {plan['plan_hash']}`. No apruebes si el auditor "
        "o el test de truncación no indica `pass`.",
        "",
    ])
    return "\n".join(lines)


def prepare(args: argparse.Namespace) -> int:
    if args.leverage != 1.0:
        raise ValueError(
            "El backtester de /strategy es cash-only. No optimizo con apalancamiento "
            "distinto de 1; cambiar eso requiere un modelo de ejecución autorizado."
        )
    if args.contract_size != 1.0:
        raise ValueError(
            "El backtester expresa la cantidad en unidades del activo y no aplica "
            "multiplicador de contrato. Usa contract_size=1 o solicita esa extensión."
        )
    if not args.source or not args.instrument or not args.timeframe:
        raise ValueError("source, instrumento y temporalidad son obligatorios.")

    policy = load_policy(args.policy)
    grid = load_grid(args.grid)
    raw, quality, dataset_hash = load_market_data(args.dataset)
    base, costs = make_cost_config(
        capital=args.capital,
        timeframe=args.timeframe,
        commission_bps=args.commission_bps,
        spread_bps=args.spread_bps,
        slippage_bps=args.slippage_bps,
    )
    bars, splits, embargo = get_data_segments(raw, base, policy)
    split_issues = [
        f"{name} tiene {item['bars']} barras; hacen falta al menos {base.ema_slow + 2}."
        for name, item in splits.items()
        if int(item["bars"]) < base.ema_slow + 2
    ]
    space_size = len(candidate_grid(grid))
    if space_size > int(policy["stop"]["max_backtests"]):
        split_issues.append(
            f"El grid ({space_size}) supera max_backtests "
            f"({policy['stop']['max_backtests']})."
        )
    preflight_warnings = []
    if float(quality["years_covered"]) < 3:
        preflight_warnings.append(
            "Menos de 3 años de cobertura: reducir ambición estadística y complejidad."
        )
    preflight_warnings.extend([
        "Sesiones/horarios, sesgo de supervivencia, ajustes corporativos y "
        "cambios de régimen requieren interpretación según el instrumento.",
        "El número esperado de operaciones solo se conocerá tras el baseline aprobado.",
    ])
    sources, code = strategy_sources()
    lookahead = lookahead_check(raw, base)

    response = call_opencode(
        build_audit_prompt(code, quality, lookahead, base)
    )
    try:
        model_audit = parse_model_json(response)
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Auditoría GPT-6 Luna no verificable; no se crea un plan aprobable. "
            f"Respuesta: {response[:3000]}"
        ) from exc
    expected_verdicts = {"pass", "stop", "needs_review"}
    if model_audit.get("verdict") not in expected_verdicts:
        raise RuntimeError("La auditoría de GPT-6 Luna no cumple el esquema requerido.")

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    ensure_runs_directory()
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    if run_dir.resolve().parent != RUNS_DIR.resolve():
        raise ValueError("El directorio del run sale del sandbox /permutation/runs.")
    for folder in ("checkpoints", "reports", "logs"):
        (run_dir / folder).mkdir()
    plan: dict[str, Any] = {
        "run_id": run_id,
        "created_at": utc_now(),
        "model": MODEL,
        "status": "pending_approval",
        "eligible_for_optimization": (
            lookahead["status"] == "pass"
            and model_audit["verdict"] == "pass"
            and not split_issues
        ),
        "autonomous": bool(args.autonomous),
        "inputs": {
            "dataset": str(Path(args.dataset).expanduser().resolve()),
            "source": args.source,
            "instrument": args.instrument,
            "timeframe": args.timeframe,
            "capital": args.capital,
            "leverage": args.leverage,
            "contract_size": args.contract_size,
            "commission_bps": args.commission_bps,
            "spread_bps": args.spread_bps,
            "slippage_bps": args.slippage_bps,
        },
        "costs": costs,
        "dataset": quality,
        "strategy_sources": [str(path.relative_to(PROJECT_ROOT)) for path in sources],
        "strategy_sha256": hash_paths(sources),
        "lookahead": lookahead,
        "model_audit": model_audit,
        "policy": policy,
        "policy_sha256": hash_paths([args.policy.resolve()]),
        "grid": grid,
        "grid_sha256": hash_paths([args.grid.resolve()]),
        "parameter_inventory": parameter_inventory(grid, base),
        "space_size": space_size,
        "seed": int(policy.get("seed", 42)),
        "splits": splits,
        "embargo_bars": embargo,
        "strategy_bars": len(bars),
        "preflight": {
            "issues": split_issues,
            "warnings": preflight_warnings,
        },
        "score_definition": (
            "base=0.30*min(Sharpe,3)+0.20*min(Sortino,4)/1.5+"
            "0.20*min(PF,3)+0.30*min(Calmar,3); score=base*P_dd*P_n*"
            "P_cons*P_conc*P_neigh"
        ),
    }
    plan["plan_hash"] = digest_json(immutable_plan(plan))
    if lookahead["status"] != "pass" or model_audit["verdict"] != "pass":
        plan["status"] = "blocked_by_audit"
    elif split_issues:
        plan["status"] = "blocked_by_preflight"
    elif args.autonomous:
        plan["status"] = "approved"
        plan["approval"] = {
            "status": "approved",
            "mode": "autonomous_explicitly_requested",
            "approved_at": utc_now(),
            "plan_hash": plan["plan_hash"],
        }
    (run_dir / "plan.json").write_text(
        json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (run_dir / "plan.md").write_text(make_plan_markdown(plan), encoding="utf-8")
    (run_dir / "model_audit.json").write_text(
        json.dumps(model_audit, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (run_dir / "config.yaml").write_text(
        args.policy.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (run_dir / "environment.txt").write_text(
        environment_snapshot(int(policy.get("seed", 42))), encoding="utf-8"
    )
    (run_dir / "approval.json").write_text(
        json.dumps(
            plan.get("approval", {"status": "pending", "plan_hash": plan["plan_hash"]}),
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[PLAN] {run_dir / 'plan.md'}")
    print(f"[PLAN HASH] {plan['plan_hash']}")
    print(f"[LOOK-AHEAD] {lookahead['status']} | [GPT-6 LUNA] {model_audit['verdict']}")
    print(f"[COMBINATIONS] {plan['space_size']:,}")
    if plan["status"].startswith("blocked_by_"):
        print("[BLOCKED] No optimización: resuelve/revisa los hallazgos antes de continuar.")
        return 3
    if args.autonomous:
        print("[APPROVED] Modo autónomo solicitado explícitamente.")
    else:
        print("[WAITING] Revisa el plan; aún no se ejecutó ningún backtest de búsqueda.")
    return 0


def environment_snapshot(seed: int) -> str:
    python = sys.version.replace("\n", " ")
    try:
        frozen = subprocess.run(
            [sys.executable, "-m", "pip", "freeze"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        packages = frozen.stdout if frozen.returncode == 0 else "pip freeze no disponible\n"
    except (OSError, subprocess.TimeoutExpired):
        packages = "pip freeze no disponible\n"
    return f"Python: {python}\nSemilla: {seed}\n\n{packages}"


def run_directory(run_id: str) -> Path:
    ensure_runs_directory()
    folder = (RUNS_DIR / run_id).resolve()
    if folder.parent != RUNS_DIR.resolve() or not folder.is_dir():
        raise ValueError(f"Run ID no válido: {run_id}")
    return folder


def ensure_runs_directory() -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    resolved = RUNS_DIR.resolve()
    if resolved.parent != PERMUTATION_DIR.resolve():
        raise ValueError(
            f"Sandbox inválido: {RUNS_DIR} debe ser una carpeta directa de /permutation."
        )


def read_plan(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "plan.json"
    plan = json.loads(path.read_text(encoding="utf-8"))
    claimed = plan.pop("plan_hash")
    actual = digest_json(immutable_plan(plan))
    plan["plan_hash"] = claimed
    if claimed != actual:
        raise ValueError("plan.json fue modificado después de su creación; no se puede continuar.")
    return plan


def approve(args: argparse.Namespace) -> int:
    run_dir = run_directory(args.run_id)
    plan = read_plan(run_dir)
    if not plan["eligible_for_optimization"]:
        raise ValueError("El plan está bloqueado por la auditoría; no se puede aprobar.")
    if plan["status"] != "pending_approval":
        raise ValueError(f"Estado no aprobable: {plan['status']}")
    if args.plan_hash != plan["plan_hash"]:
        raise ValueError("El hash proporcionado no coincide con el plan revisado.")
    approval = {
        "status": "approved",
        "mode": "human",
        "approved_at": utc_now(),
        "plan_hash": plan["plan_hash"],
    }
    plan["status"] = "approved"
    plan["approval"] = approval
    (run_dir / "plan.json").write_text(
        json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    # Keep the approved hash bound to all immutable plan contents, excluding only
    # mutable workflow state and approval metadata.
    approved_content = {
        key: value for key, value in plan.items()
        if key not in {"status", "approval", "plan_hash"}
    }
    approval["approved_content_hash"] = digest_json(approved_content)
    (run_dir / "approval.json").write_text(
        json.dumps(approval, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"[APPROVED] {args.run_id}")
    return 0


def validate_approval(run_dir: Path, plan: dict[str, Any]) -> None:
    approval_path = run_dir / "approval.json"
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    if approval.get("status") != "approved":
        raise ValueError("Aprobación ausente: revisa y aprueba el plan primero.")
    expected = approval.get("approved_content_hash")
    if expected:
        actual_content = immutable_plan(plan)
        if digest_json(actual_content) != expected:
            raise ValueError("El plan aprobado cambió; se requiere una aprobación nueva.")
    elif approval.get("plan_hash") != plan["plan_hash"]:
        raise ValueError("El hash del plan aprobado no coincide.")


def write_metrics_csv(db_path: Path, report_dir: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM experiments ORDER BY cycle, segment, score DESC"
        ).fetchall()
        columns = [description[0] for description in connection.execute(
            "SELECT * FROM experiments LIMIT 0"
        ).description]
    records = [dict(zip(columns, row)) for row in rows]
    csv_safe_records(records, report_dir / "todos_los_experimentos.csv")
    return records


def write_checkpoint(run_dir: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    path = run_dir / "checkpoints" / "state.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def update_plan_status(run_dir: Path, plan: dict[str, Any], status: str) -> None:
    plan["status"] = status
    (run_dir / "plan.json").write_text(
        json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def run_optimizer(args: argparse.Namespace) -> int:
    run_dir = run_directory(args.run_id)
    plan = read_plan(run_dir)
    validate_approval(run_dir, plan)
    if not plan["eligible_for_optimization"]:
        raise ValueError("Auditoría no aprobada; optimización bloqueada.")
    if plan["status"] not in {
        "approved", "search_running", "stopped_max_backtests",
        "stopped_max_runtime", "stopped_max_ram",
    }:
        raise ValueError(f"Estado no compatible con búsqueda: {plan['status']}")
    if (run_dir / "reports" / "finalists.json").exists():
        raise ValueError("Ya se congelaron finalistas; no se permite reoptimizar este run.")

    raw, _, dataset_hash = load_market_data(Path(plan["inputs"]["dataset"]))
    if dataset_hash != plan["dataset"]["sha256"]:
        raise ValueError("El dataset cambió desde la aprobación del plan.")
    sources, _ = strategy_sources()
    if hash_paths(sources) != plan["strategy_sha256"]:
        raise ValueError("El código de /strategy cambió desde la aprobación.")

    base, _ = make_cost_config(
        capital=plan["inputs"]["capital"],
        timeframe=plan["inputs"]["timeframe"],
        commission_bps=plan["inputs"]["commission_bps"],
        spread_bps=plan["inputs"]["spread_bps"],
        slippage_bps=plan["inputs"]["slippage_bps"],
    )
    bars = to_timeframe(raw, base)
    segments = {
        name: (
            int(info["start_index"]),
            int(info["end_index_exclusive"]),
        )
        for name, info in plan["splits"].items()
    }
    train_start, train_end = segments["train"]
    val_start, val_end = segments["validation"]
    test_start, test_end = segments["test"]
    if min(train_end - train_start, val_end - val_start, test_end - test_start) <= 0:
        raise ValueError("Uno o varios splits quedan vacíos; reduce embargo o revisa el dataset.")

    database = run_dir / "experiments.db"
    init_database(database)
    meta = {
        "plan_hash": plan["plan_hash"],
        "dataset_sha256": dataset_hash,
        "seed": str(plan["seed"]),
        "n_trials": "0",
        "test_evaluated": "false",
        "started_at": utc_now(),
    }
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT OR REPLACE INTO run_meta(key, value) VALUES (?, ?)",
            list(meta.items()),
        )
        connection.commit()

    thresholds = plan["policy"]["hard_filters"]
    base_values = asdict(base)
    grid = plan["grid"]
    candidates = candidate_grid(grid)
    max_backtests = int(plan["policy"]["stop"]["max_backtests"])
    checkpoint_every = max(1, int(plan["policy"].get("checkpoint_every", 500)))
    state = {
        "status": "search_running",
        "completed_trials": 0,
        "total_candidates": len(candidates),
        "current_segment": "train",
        "started_at": utc_now(),
    }
    update_plan_status(run_dir, plan, "search_running")
    write_checkpoint(run_dir, state)

    # Baseline is recorded only after approval and only on train/validation.
    baseline_cfg = base
    baseline_hash = digest_json({"baseline": asdict(baseline_cfg)})
    for name, (start, end) in (("train", (train_start, train_end)), ("validation", (val_start, val_end))):
        if not existing_experiment(database, baseline_hash, 0, f"baseline_{name}"):
            data = segment_data(raw, bars, start, end)
            metrics, _ = backtest_metrics(data, baseline_cfg)
            passed, failures = apply_hard_filters(metrics, thresholds, segment=name)
            metrics["passed_filters"] = passed
            metrics["filter_failures"] = failures
            score = calculate_score(metrics)
            insert_experiment(
                database,
                param_hash=baseline_hash,
                params={"baseline": True, **base_values},
                cycle=0,
                segment=f"baseline_{name}",
                metrics=metrics,
                score=score,
                passed_filters=passed,
                seed=plan["seed"],
                timestamp=utc_now(),
            )

    completed_rows: list[dict[str, Any]] = []
    # Restore completed trial details from SQLite so a stopped run does not
    # repeat already-finished parameter configurations.
    with sqlite3.connect(database) as connection:
        cached_train = connection.execute(
            """
            SELECT param_hash, params_json, metrics_json, score, passed_filters
            FROM experiments
            WHERE cycle = 1 AND segment = 'train'
            """
        ).fetchall()
    for param_hash, params_json, metrics_json, score, passed_filters in cached_train:
        try:
            cached_metrics = json.loads(metrics_json)
        except (TypeError, json.JSONDecodeError):
            cached_metrics = {}
        completed_rows.append({
            "param_hash": param_hash,
            "params": json.loads(params_json),
            **cached_metrics,
            "score": float(score or 0.0),
            "passed_filters": bool(passed_filters),
        })
    failures_path = run_dir / "logs" / "failed.jsonl"
    with sqlite3.connect(database) as connection:
        attempted = connection.execute(
            """
            SELECT COUNT(DISTINCT param_hash) FROM experiments
            WHERE cycle = 1 AND segment IN ('train', 'train_failed')
            """
        ).fetchone()[0]
    trials = int(attempted or 0)
    started = time.monotonic()
    last_checkpoint = started
    with failures_path.open("a", encoding="utf-8") as failures:
        for index, params in enumerate(candidates, start=1):
            param_hash = digest_json(params)
            if (
                existing_experiment(database, param_hash, 1, "train")
                or existing_experiment(database, param_hash, 1, "train_failed")
            ):
                continue
            process_hours = (time.monotonic() - started) / 3600
            max_runtime_hours = float(plan["policy"]["stop"]["max_runtime_hours"])
            max_ram_gb = float(plan["policy"]["stop"]["max_ram_gb"])
            if trials >= max_backtests or process_hours >= max_runtime_hours:
                stop_status = (
                    "stopped_max_runtime"
                    if process_hours >= max_runtime_hours
                    else "stopped_max_backtests"
                )
                state.update({"status": stop_status, "completed_trials": trials})
                write_checkpoint(run_dir, state)
                update_plan_status(run_dir, plan, stop_status)
                return 4
            try:
                import resource

                current_ram_gb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (
                    1024 * 1024
                )
            except (ImportError, AttributeError):
                current_ram_gb = 0.0
            if current_ram_gb > max_ram_gb:
                state.update({
                    "status": "stopped_max_ram",
                    "completed_trials": trials,
                    "max_rss_gb": current_ram_gb,
                })
                write_checkpoint(run_dir, state)
                update_plan_status(run_dir, plan, "stopped_max_ram")
                return 4
            try:
                cfg = replace(base, **params)
                data = segment_data(raw, bars, train_start, train_end)
                metrics, _ = backtest_metrics(data, cfg)
                passed, reasons = apply_hard_filters(metrics, thresholds, segment="train")
                metrics["passed_filters"] = passed
                metrics["filter_failures"] = reasons
                metrics["neighbor_ratio"] = 1.0
                score = calculate_score(metrics)
                insert_experiment(
                    database,
                    param_hash=param_hash,
                    params=params,
                    cycle=1,
                    segment="train",
                    metrics=metrics,
                    score=score,
                    passed_filters=passed,
                    seed=plan["seed"],
                    timestamp=utc_now(),
                )
                completed_rows.append({
                    "param_hash": param_hash,
                    "params": params,
                    **metrics,
                    "score": score,
                })
                trials += 1
                now = time.monotonic()
                if trials % checkpoint_every == 0 or now - last_checkpoint >= 300:
                    state.update({
                        "completed_trials": trials,
                        "current_candidate": index,
                        "elapsed_seconds": now - started,
                    })
                    write_checkpoint(run_dir, state)
                    last_checkpoint = now
                print(
                    f"[TRAIN {index}/{len(candidates)}] score={score:.5f} "
                    f"PF={metrics['pf']} DD={metrics['max_dd']:.2f}% "
                    f"trades={metrics['n_trades']} {params}",
                    flush=True,
                )
            except Exception as exc:
                error_record = {
                    "candidate": index,
                    "param_hash": param_hash,
                    "params": params,
                    "error": f"{type(exc).__name__}: {exc}",
                    "timestamp": utc_now(),
                }
                insert_experiment(
                    database,
                    param_hash=param_hash,
                    params=params,
                    cycle=1,
                    segment="train_failed",
                    metrics={"error": error_record["error"]},
                    score=0.0,
                    passed_filters=False,
                    seed=plan["seed"],
                    timestamp=error_record["timestamp"],
                )
                failures.write(json.dumps(error_record, ensure_ascii=False) + "\n")
                failures.flush()
                trials += 1

    # Top 5% by train score proceeds to walk-forward consistency and validation.
    passed_rows = [row for row in completed_rows if row["passed_filters"]]
    passed_rows.sort(key=lambda row: row["score"], reverse=True)
    shortlist_count = max(1, math_ceil_percent(len(passed_rows), 5)) if passed_rows else 0
    shortlist = passed_rows[:shortlist_count]
    folds = int(plan["policy"]["walk_forward"]["folds"])
    fold_count = 0
    fold_rows: dict[str, list[dict[str, Any]]] = {row["param_hash"]: [] for row in shortlist}
    fold_width = max(1, (train_end - train_start) // folds)
    for row in shortlist:
        cfg = replace(base, **row["params"])
        for fold in range(folds):
            fold_start = train_start + fold * fold_width
            fold_end = train_end if fold == folds - 1 else min(train_end, fold_start + fold_width)
            fold_data = segment_data(raw, bars, fold_start, fold_end)
            if fold_start >= fold_end or fold_data.empty:
                continue
            try:
                fold_metrics, _ = backtest_metrics(fold_data, cfg)
                fold_pass = fold_metrics["n_trades"] >= int(
                    thresholds["min_trades_per_fold"]
                )
                insert_experiment(
                    database,
                    param_hash=row["param_hash"],
                    params=row["params"],
                    cycle=1,
                    segment=f"fold_{fold + 1}",
                    metrics=fold_metrics,
                    score=0.0,
                    passed_filters=fold_pass,
                    seed=plan["seed"],
                    timestamp=utc_now(),
                )
                fold_rows[row["param_hash"]].append({
                    **fold_metrics,
                    "passed_filters": fold_pass,
                    "fold": fold + 1,
                })
                fold_count += 1
            except Exception as exc:
                fold_rows[row["param_hash"]].append({
                    "passed_filters": False,
                    "fold": fold + 1,
                    "error": f"{type(exc).__name__}: {exc}",
                })
    stable_shortlist = [
        row for row in shortlist
        if len(fold_rows[row["param_hash"]]) == folds
        and all(fold["passed_filters"] for fold in fold_rows[row["param_hash"]])
    ]

    # Validation is used once to select the already-shortlisted candidates.
    validation_rows = []
    for row in stable_shortlist:
        cfg = replace(base, **row["params"])
        data = segment_data(raw, bars, val_start, val_end)
        try:
            metrics, _ = backtest_metrics(data, cfg)
            passed, reasons = apply_hard_filters(metrics, thresholds, segment="validation")
            metrics["passed_filters"] = passed
            metrics["filter_failures"] = reasons
            metrics["neighbor_ratio"] = 1.0
            score = calculate_score(metrics)
            insert_experiment(
                database,
                param_hash=row["param_hash"],
                params=row["params"],
                cycle=1,
                segment="validation",
                metrics=metrics,
                score=score,
                passed_filters=passed,
                seed=plan["seed"],
                timestamp=utc_now(),
            )
            validation_rows.append({
                "param_hash": row["param_hash"],
                "params": row["params"],
                "train": {key: row.get(key) for key in (
                    "net_profit", "return_pct", "max_dd", "pf", "sharpe",
                    "sortino", "calmar", "win_rate", "n_trades", "expectancy",
                    "top5_share", "score",
                )},
                "folds": fold_rows[row["param_hash"]],
                "validation": metrics,
                "validation_score": score,
            })
        except Exception as exc:
            failures.write(json.dumps({
                "stage": "validation",
                "param_hash": row["param_hash"],
                "params": row["params"],
                "error": f"{type(exc).__name__}: {exc}",
                "timestamp": utc_now(),
            }, ensure_ascii=False) + "\n")

    validation_rows.sort(key=lambda row: row["validation_score"], reverse=True)
    model_review: dict[str, Any] = {"status": "unavailable"}
    if validation_rows:
        review_prompt = (
            "TASK: RESULTS_REVIEW\n"
            "Python calculó y ordenó estos candidatos exclusivamente con train, "
            "folds cronológicos y validation. No accedas ni infieras métricas test. "
            "No cambies el ranking ni los parámetros; describe anomalías y límites.\n"
            f"N_TRIALS: {trials}\n"
            f"RESULTS:\n{json.dumps(validation_rows, ensure_ascii=False, allow_nan=False)}\n"
            "Devuelve únicamente el JSON de RESULTS_REVIEW de tu prompt de sistema."
        )
        try:
            model_review = parse_model_json(call_opencode(review_prompt))
            if (
                not isinstance(model_review.get("observations", []), list)
                or not isinstance(model_review.get("anomalies", []), list)
                or model_review.get("ranking_changed") is not False
            ):
                raise ValueError("El análisis de resultados no cumple el esquema.")
            model_review["status"] = "ok"
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
            model_review = {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
    (run_dir / "reports" / "model_result_review.json").write_text(
        json.dumps(model_review, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    final_count = int(plan["policy"]["finalists_frozen"])
    frozen = validation_rows[:final_count]
    finalists_payload = {
        "run_id": plan["run_id"],
        "plan_hash": plan["plan_hash"],
        "frozen_at": utc_now(),
        "selection_data": "train + walk-forward folds + validation only",
        "test_evaluated": False,
        "finalists": frozen,
    }
    finalists_payload["sha256"] = digest_json(finalists_payload)
    finalist_path = run_dir / "reports" / "finalists.json"
    finalist_path.write_text(
        json.dumps(finalists_payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    all_records = write_metrics_csv(database, run_dir / "reports")
    leaderboard = sorted(
        validation_rows,
        key=lambda row: row["validation_score"],
        reverse=True,
    )
    csv_safe_records(leaderboard[:50], run_dir / "reports" / "mejores_combinaciones.csv")
    final_status = "finalists_frozen" if frozen else "no_candidates"
    state.update({
        "status": final_status,
        "completed_trials": trials,
        "fold_backtests": fold_count,
        "n_trials": trials,
        "train_candidates_passed_filters": len(passed_rows),
        "elapsed_seconds": time.monotonic() - started,
        "finalists_sha256": finalists_payload["sha256"],
    })
    write_checkpoint(run_dir, state)
    update_plan_status(run_dir, plan, final_status)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO run_meta(key, value) VALUES ('n_trials', ?)",
            (str(state["n_trials"]),),
        )
        connection.execute(
            "INSERT OR REPLACE INTO run_meta(key, value) VALUES ('test_evaluated', 'false')"
        )
        connection.commit()
    write_initial_report(run_dir, plan, validation_rows, state, all_records, model_review)
    if frozen:
        print(f"[FROZEN] {finalist_path} | finalistas={len(frozen)}")
        print("[TEST LOCKED] Para evaluar test una sola vez, usa `test --confirm-test`.")
    else:
        print("[NO CANDIDATES] Ningún candidato superó selección train/folds/validación.")
    return 0


def math_ceil_percent(count: int, percent: int) -> int:
    return max(1, (count * percent + 99) // 100)


def write_initial_report(
    run_dir: Path,
    plan: dict[str, Any],
    validation_rows: list[dict[str, Any]],
    state: dict[str, Any],
    experiment_records: list[dict[str, Any]],
    model_review: dict[str, Any],
) -> None:
    successful = int(state.get("train_candidates_passed_filters", 0))
    baseline = {
        row["segment"].removeprefix("baseline_"): row
        for row in experiment_records
        if row["segment"] in {"baseline_train", "baseline_validation"}
    }
    lines = [
        "# Informe de optimización (test aún bloqueado)",
        "",
        "## 1. Estado",
        "",
        f"- Veredicto provisional: **{'PENDIENTE DE TEST OOS' if validation_rows else 'NO ROBUSTA'}**.",
        f"- `N_trials`: {state.get('n_trials', 0)} configuraciones únicas del ciclo de búsqueda.",
        f"- Configuraciones que superaron filtros duros en train: {successful}.",
        f"- Finalistas congelados: {len(validation_rows[:plan['policy']['finalists_frozen']])}.",
        "- No se afirma rentabilidad futura.",
        "",
        "## 2. Datos, supuestos y costes",
        "",
        f"- Instrumento/fuente/temporalidad: {plan['inputs']['instrument']} / "
        f"{plan['inputs']['source']} / {plan['inputs']['timeframe']}.",
        f"- Dataset SHA-256: `{plan['dataset']['sha256']}`.",
        f"- Cobertura: {plan['dataset']['start_utc']} → {plan['dataset']['end_utc']}.",
        f"- Costes: `{json.dumps(plan['costs'], ensure_ascii=False)}`.",
        "",
        "## 3. Auditoría",
        "",
        f"- Look-ahead por truncación: **{plan['lookahead']['status']}**.",
        f"- Revisión GPT-6 Luna: **{plan['model_audit'].get('verdict')}**.",
        f"- Observaciones: `{json.dumps(plan['model_audit'].get('other_risks', []), ensure_ascii=False)}`.",
        f"- Revisión de resultados GPT-6 Luna: **{model_review.get('status')}**.",
        *[
            f"- Análisis de resultados: {item}"
            for item in model_review.get("observations", [])
        ],
        *[
            f"- Anomalía reportada por GPT: {item}"
            for item in model_review.get("anomalies", [])
        ],
        "",
        "## 4. Baseline original",
        "",
        "| Segmento | Beneficio neto | DD % | PF | Sharpe | Sortino | Trades |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ("train", "validation"):
        row = baseline.get(name)
        if row:
            lines.append(
                f"| {name} | {row['net_profit']} | {row['max_dd']} | {row['pf']} | "
                f"{row['sharpe']} | {row['sortino']} | {row['n_trades']} |"
            )
    lines.extend([
        "",
        "## 5. Resultados train / folds / validación",
        "",
        "| Parámetros | Train score | Train PF | Train DD % | Train trades | Validation score | Validation PF | Validation DD % | Validation trades |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for row in validation_rows:
        train = row["train"]
        val = row["validation"]
        lines.append(
            f"| `{json.dumps(row['params'], ensure_ascii=False)}` | {train['score']:.5f} | "
            f"{train['pf']} | {train['max_dd']:.2f} | {train['n_trades']} | "
            f"{row['validation_score']:.5f} | {val['pf']} | {val['max_dd']:.2f} | "
            f"{val['n_trades']} |"
        )
    lines.extend([
        "",
        "### Walk-forward dentro de train",
        "",
        "| Parámetros | Fold | Net PnL | DD % | PF | Trades |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for row in validation_rows:
        for fold in row["folds"]:
            lines.append(
                f"| `{json.dumps(row['params'], ensure_ascii=False)}` | {fold.get('fold')} | "
                f"{fold.get('net_profit')} | {fold.get('max_dd')} | {fold.get('pf')} | "
                f"{fold.get('n_trades')} |"
            )
    lines.extend([
        "",
        "## 6. Test",
        "",
        (
            "El tramo test sigue sin métricas. Los finalistas y sus hashes se guardaron "
            "antes de cualquier evaluación OOS. El test solo puede ejecutarse una vez."
            if validation_rows
            else "No hubo finalistas: no se desbloquea ni se evalúa test."
        ),
        "",
        "## 7. Limitaciones",
        "",
        "- El test de look-ahead por truncación no sustituye una revisión humana del código.",
        "- Los resultados dependen de los supuestos de costes y del modelo de ejecución del backtester.",
        "- Las pruebas DSR, PBO, Reality Check/SPA, Monte Carlo, estrés de costes y "
        "perturbación de vecinos no se declaran superadas por este flujo.",
        "- Esta versión ejecuta un ciclo exhaustivo del grid aprobado; no hace "
        "refinamiento iterativo ni genera gráficos/curvas de equity exportables.",
        "- El walk-forward aquí evalúa bloques cronológicos dentro de train; la ventana "
        "de cada fold se ejecuta como tramo independiente.",
        "- Una configuración seleccionada históricamente no garantiza resultados futuros.",
        "",
    ])
    (run_dir / "reports" / "informe_final.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )


def evaluate_test(args: argparse.Namespace) -> int:
    if not args.confirm_test:
        raise ValueError("El test requiere `--confirm-test` explícito.")
    run_dir = run_directory(args.run_id)
    plan = read_plan(run_dir)
    if plan["status"] != "finalists_frozen":
        raise ValueError("El test se habilita solo después de congelar finalistas.")
    database = run_dir / "experiments.db"
    with sqlite3.connect(database) as connection:
        evaluated = connection.execute(
            "SELECT value FROM run_meta WHERE key = 'test_evaluated'"
        ).fetchone()
    if evaluated and evaluated[0] != "false":
        raise ValueError(
            "El test de este run ya comenzó/se evaluó; está bloqueado permanentemente."
        )
    finalists_path = run_dir / "reports" / "finalists.json"
    frozen = json.loads(finalists_path.read_text(encoding="utf-8"))
    claimed_hash = frozen.pop("sha256")
    actual_hash = digest_json(frozen)
    frozen["sha256"] = claimed_hash
    if claimed_hash != actual_hash or frozen["plan_hash"] != plan["plan_hash"]:
        raise ValueError("El archivo de finalistas cambió desde su congelación.")
    if frozen.get("test_evaluated") or not frozen.get("finalists"):
        raise ValueError("Finalistas vacíos o test ya evaluado.")
    sources, _ = strategy_sources()
    if hash_paths(sources) != plan["strategy_sha256"]:
        raise ValueError("El código de /strategy cambió desde la aprobación.")
    raw, _, dataset_hash = load_market_data(Path(plan["inputs"]["dataset"]))
    if dataset_hash != plan["dataset"]["sha256"]:
        raise ValueError("El dataset cambió desde la aprobación.")
    base, _ = make_cost_config(
        capital=plan["inputs"]["capital"],
        timeframe=plan["inputs"]["timeframe"],
        commission_bps=plan["inputs"]["commission_bps"],
        spread_bps=plan["inputs"]["spread_bps"],
        slippage_bps=plan["inputs"]["slippage_bps"],
    )
    bars = to_timeframe(raw, base)
    start = int(plan["splits"]["test"]["start_index"])
    end = int(plan["splits"]["test"]["end_index_exclusive"])
    data = segment_data(raw, bars, start, end)
    # Reserve the one permitted OOS evaluation before invoking any test
    # backtest. If the process fails midway, the test remains locked.
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO run_meta(key, value) VALUES ('test_evaluated', 'running')"
        )
        connection.execute(
            "INSERT OR REPLACE INTO run_meta(key, value) VALUES ('test_started_at', ?)",
            (utc_now(),),
        )
        connection.commit()
    results = []
    for finalist in frozen["finalists"]:
        cfg = replace(base, **finalist["params"])
        metrics, _ = backtest_metrics(data, cfg)
        validation = finalist["validation"]
        accept = plan["policy"]["oos_acceptance"]
        passed = (
            metrics["n_trades"] >= int(accept["min_trades"])
            and metrics["pf"] is not None
            and metrics["pf"] >= float(accept["min_profit_factor"])
            and metrics["sharpe"] >= float(accept["min_sharpe_absolute"])
            and metrics["sharpe"]
            >= float(validation["sharpe"]) * float(accept["min_sharpe_vs_validation"])
            and abs(metrics["max_dd"])
            <= float(plan["policy"]["hard_filters"]["max_drawdown_pct"])
            * float(accept["max_drawdown_multiple"])
        )
        metrics["passed_oos_acceptance"] = passed
        insert_experiment(
            database,
            param_hash=finalist["param_hash"],
            params=finalist["params"],
            cycle=1,
            segment="test",
            metrics=metrics,
            score=0.0,
            passed_filters=passed,
            seed=plan["seed"],
            timestamp=utc_now(),
        )
        results.append({
            "param_hash": finalist["param_hash"],
            "params": finalist["params"],
            "test": metrics,
            "passed_oos_acceptance": passed,
        })
    frozen["test_evaluated"] = True
    frozen["test_evaluated_at"] = utc_now()
    frozen["test_results"] = results
    # Preserve the original pre-test finalist hash; hash the extended record
    # separately so the frozen selection is still independently verifiable.
    frozen["post_test_record_sha256"] = digest_json({
        key: value for key, value in frozen.items()
        if key != "post_test_record_sha256"
    })
    finalists_path.write_text(
        json.dumps(frozen, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO run_meta(key, value) VALUES ('test_evaluated', 'true')"
        )
        connection.commit()
    write_metrics_csv(database, run_dir / "reports")
    csv_safe_records(results, run_dir / "reports" / "resultados_test.csv")
    any_passed = any(row["passed_oos_acceptance"] for row in results)
    verdict = "MARGINAL" if any_passed else "NO ROBUSTA"
    if any_passed:
        # This is a research artifact only; it never mutates strategy/config.py.
        # When several candidates pass OOS, prefer lower test drawdown, then
        # higher validation score rather than the largest test profit.
        selected = min(
            (row for row in results if row["passed_oos_acceptance"]),
            key=lambda row: (
                abs(row["test"]["max_dd"]),
                -next(
                    item["validation_score"]
                    for item in frozen["finalists"]
                    if item["param_hash"] == row["param_hash"]
                ),
            ),
        )
        (run_dir / "reports" / "parametros_optimizados.py").write_text(
            "# Candidato histórico OOS; ver informe_final.md y limitaciones.\n"
            f"PARAMETERS = {selected['params']!r}\n",
            encoding="utf-8",
        )
    report = run_dir / "reports" / "informe_final.md"
    with report.open("a", encoding="utf-8") as output:
        output.write("\n## Evaluación final OOS\n\n")
        output.write(f"- Veredicto: **{verdict}**.\n")
        output.write("- El test se ejecutó una sola vez sobre los finalistas congelados.\n")
        output.write(
            "- No se declara ROBUSTA: las pruebas DSR, PBO, permutación/Reality "
            "Check, estrés y Monte Carlo no están implementadas como aprobaciones "
            "estadísticas en este motor.\n"
        )
        for row in results:
            output.write(
                f"- `{row['param_hash'][:12]}`: "
                f"{'pasa' if row['passed_oos_acceptance'] else 'no pasa'} OOS; "
                f"PF={row['test']['pf']}, Sharpe={row['test']['sharpe']:.3f}, "
                f"DD={row['test']['max_dd']:.2f}%, trades={row['test']['n_trades']}.\n"
            )
        output.write(
            "\nNo se reoptimiza con test. Un resultado histórico no garantiza rentabilidad futura.\n"
        )
    plan["status"] = "test_evaluated"
    plan["test_evaluated_at"] = utc_now()
    plan_path = run_dir / "plan.json"
    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    state_path = run_dir / "checkpoints" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update({"status": "test_evaluated", "verdict": verdict, "test_rows": len(results)})
    write_checkpoint(run_dir, state)
    print(f"[TEST EVALUATED ONCE] verdict={verdict} | rows={len(results)}")
    return 0


def status(args: argparse.Namespace) -> int:
    run_dir = run_directory(args.run_id)
    plan = read_plan(run_dir)
    checkpoint_path = run_dir / "checkpoints" / "state.json"
    checkpoint = (
        json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint_path.exists()
        else {}
    )
    print(json.dumps({
        "run_id": args.run_id,
        "plan_status": plan["status"],
        "plan_hash": plan["plan_hash"],
        "checkpoint": checkpoint,
    }, indent=2, ensure_ascii=False))
    return 0


def show_intake() -> int:
    print(
        "Entendí la misión: auditar /strategy y buscar configuraciones más robustas "
        "mediante permutaciones, sin modificar la lógica original.\n"
        "El agente separará cronológicamente train/validación/test, fijará score y "
        "umbrales antes de buscar y mantendrá test bloqueado hasta congelar finalistas.\n\n"
        "Para iniciar, proporciona:\n"
        "1. Dataset histórico y fuente (el código de /strategy ya está en este proyecto).\n"
        "2. Instrumento, temporalidad de la estrategia y período cubierto.\n"
        "3. Capital inicial.\n"
        "4. Costes reales: comisión, spread y slippage (bps).\n"
        "5. Apalancamiento y tamaño de contrato/lote.\n"
        "6. Si autorizas explícitamente «modo autónomo».\n\n"
        "Después ejecutaré la auditoría y presentaré el inventario/plan para aprobación. "
        "No se lanzará ninguna optimización antes de esos pasos."
    )
    return 0


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Agente de permutaciones para /strategy con OpenCode GPT-6 Luna."
    )
    subparsers = parser.add_subparsers(dest="command")

    prepare_parser = subparsers.add_parser("prepare", help="Audita datos/código y crea el plan.")
    prepare_parser.add_argument("--dataset", type=Path, required=True)
    prepare_parser.add_argument("--source", required=True, help="Fuente de los datos.")
    prepare_parser.add_argument("--instrument", required=True, help="Símbolo/contrato.")
    prepare_parser.add_argument("--timeframe", required=True, help="Temporalidad de la estrategia, p. ej. 30min.")
    prepare_parser.add_argument("--capital", type=float, required=True)
    prepare_parser.add_argument("--commission-bps", type=float, required=True)
    prepare_parser.add_argument("--spread-bps", type=float, required=True)
    prepare_parser.add_argument("--slippage-bps", type=float, required=True)
    prepare_parser.add_argument("--leverage", type=float, required=True)
    prepare_parser.add_argument("--contract-size", type=float, required=True)
    prepare_parser.add_argument("--grid", type=Path, default=DEFAULT_GRID)
    prepare_parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    prepare_parser.add_argument(
        "--autonomous",
        action="store_true",
        help="Autoriza explícitamente continuar sin aprobación humana del plan.",
    )

    approve_parser = subparsers.add_parser("approve", help="Aprueba un plan por hash.")
    approve_parser.add_argument("--run-id", required=True)
    approve_parser.add_argument("--plan-hash", required=True)

    run_parser = subparsers.add_parser("run", help="Ejecuta búsqueda train + selección validation.")
    run_parser.add_argument("--run-id", required=True)

    test_parser = subparsers.add_parser("test", help="Evalúa finalistas congelados en test una sola vez.")
    test_parser.add_argument("--run-id", required=True)
    test_parser.add_argument("--confirm-test", action="store_true")

    status_parser = subparsers.add_parser("status", help="Muestra el estado del run.")
    status_parser.add_argument("--run-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        return show_intake()
    try:
        return {
            "prepare": prepare,
            "approve": approve,
            "run": run_optimizer,
            "test": evaluate_test,
            "status": status,
        }[args.command](args)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2



# Motor de datos, métricas, backtest y SQLite integrado en este mismo archivo.
def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_json(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def hash_paths(paths: list[Path]) -> str:
    digest = sha256()
    for path in sorted(paths, key=lambda item: str(item)):
        digest.update(str(path).encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _read_csv(path: Path) -> pd.DataFrame:
    """Read OHLCV headers and headerless Binance Vision CSVs."""
    try:
        frame = pd.read_csv(path)
    except (OSError, pd.errors.ParserError, UnicodeDecodeError):
        frame = pd.read_csv(path, header=None)

    aliases = {"timestamp", "open_time", "datetime", "date", "time", "ts"}
    lowered = {str(column).strip().lower() for column in frame.columns}
    if aliases.intersection(lowered):
        return frame

    # Binance Vision 1m klines have 12 columns and no header. Keep the first
    # six only; the strategy consumes timestamp and OHLCV.
    frame = pd.read_csv(path, header=None)
    if frame.shape[1] < 6:
        raise ValueError(f"CSV sin cabecera OHLCV reconocible: {path}")
    frame = frame.iloc[:, :6].copy()
    frame.columns = ["timestamp", "open", "high", "low", "close", "volume"]
    return frame


def _timestamp_quality(frame: pd.DataFrame) -> tuple[int, int]:
    aliases = ("timestamp", "open_time", "datetime", "date", "time", "ts")
    by_name = {str(column).strip().lower(): column for column in frame.columns}
    name = next((by_name[key] for key in aliases if key in by_name), None)
    if name is None and len(frame.columns):
        name = frame.columns[0]
    if name is None:
        return 0, len(frame)
    values = frame[name]
    if pd.api.types.is_numeric_dtype(values):
        numeric = pd.to_numeric(values, errors="coerce")
        valid = numeric.dropna()
        magnitude = float(valid.abs().median()) if len(valid) else 0.0
        unit = (
            "ns" if magnitude >= 1e17 else "us" if magnitude >= 1e14
            else "ms" if magnitude >= 1e11 else "s" if magnitude >= 1e9
            else None
        )
        parsed = (
            pd.to_datetime(numeric, unit=unit, utc=True, errors="coerce")
            if unit else pd.to_datetime(values, utc=True, errors="coerce")
        )
    else:
        parsed = pd.to_datetime(values, utc=True, errors="coerce")
    invalid = int(parsed.isna().sum())
    duplicates = int(parsed.dropna().duplicated().sum())
    return duplicates, invalid


def load_market_data(path: Path) -> tuple[pd.DataFrame, dict[str, Any], str]:
    """Load a parquet, one CSV, or a directory of CSVs and audit its shape."""
    path = path.expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"No existe el dataset: {path}")

    if path.is_dir():
        sources = sorted(path.glob("*.csv"))
        if not sources:
            raise ValueError(f"No hay CSVs directamente dentro de {path}")
        source_frames = [_read_csv(item) for item in sources]
        original = pd.concat(source_frames, ignore_index=True)
        hash_files = sources
    elif path.suffix.lower() in {".parquet", ".pq"}:
        original = pd.read_parquet(path)
        hash_files = [path]
    elif path.suffix.lower() == ".csv":
        original = _read_csv(path)
        hash_files = [path]
    else:
        raise ValueError("Formato no soportado: usa parquet, pq, CSV o carpeta de CSVs.")

    raw_rows = len(original)
    duplicate_timestamps, invalid_timestamps = _timestamp_quality(original)
    clean = normalize_columns(original)
    finite = np.isfinite(clean[["open", "high", "low", "close", "volume"]].to_numpy(float)).all(axis=1)
    valid_ohlc = (
        (clean["high"] >= clean[["open", "close", "low"]].max(axis=1))
        & (clean["low"] <= clean[["open", "close", "high"]].min(axis=1))
        & (clean["volume"] >= 0)
    ).to_numpy()
    bad_rows = int((~finite | ~valid_ohlc).sum())
    clean = clean.loc[finite & valid_ohlc].copy()
    if clean.empty:
        raise ValueError("El dataset no contiene barras OHLCV válidas.")

    deltas = clean.index.to_series().diff().dropna().dt.total_seconds() / 60
    median_minutes = float(deltas.median()) if len(deltas) else None
    gaps = deltas[deltas > median_minutes] if median_minutes else pd.Series(dtype=float)
    returns = clean["close"].pct_change().abs().dropna()
    outliers = int((returns > 0.15).sum())
    years = max(
        0.0,
        (clean.index[-1] - clean.index[0]).total_seconds() / (365.25 * 24 * 3600),
    )
    quality = {
        "rows_in_source": raw_rows,
        "bars": int(len(clean)),
        "duplicate_or_invalid_rows_removed": int(raw_rows - len(clean) + bad_rows),
        "duplicate_timestamps": duplicate_timestamps,
        "invalid_timestamps": invalid_timestamps,
        "invalid_ohlcv_rows": bad_rows,
        "gaps_above_median_interval": int(len(gaps)),
        "max_gap_minutes": float(gaps.max()) if len(gaps) else None,
        "median_source_interval_minutes": median_minutes,
        "start_utc": clean.index[0].isoformat(),
        "end_utc": clean.index[-1].isoformat(),
        "years_covered": years,
        "absolute_close_changes_over_15pct": outliers,
        "timezone": "UTC (timestamps normalized by strategy.utils.normalize_columns)",
        "sha256": hash_paths(hash_files),
        "source_files": [str(item) for item in hash_files],
    }
    return clean, quality, quality["sha256"]


def compare_prefix(full: pd.DataFrame, truncated: pd.DataFrame, cut: int) -> list[str]:
    """Return names of columns whose prefix changed when future rows are removed."""
    common = [name for name in full.columns if name in truncated.columns]
    changed: list[str] = []
    for name in common:
        left = full[name].iloc[:cut].to_numpy()
        right = truncated[name].iloc[:cut].to_numpy()
        if len(left) != len(right):
            changed.append(name)
            continue
        if left.dtype.kind in "fiu" and right.dtype.kind in "fiu":
            if not np.allclose(left, right, rtol=1e-10, atol=1e-12, equal_nan=True):
                changed.append(name)
        elif not np.array_equal(left, right):
            changed.append(name)
    return changed


def lookahead_check(data: pd.DataFrame, cfg: Config) -> dict[str, Any]:
    """Compare full-history signals/exits with several chronologically truncated runs."""
    bars = to_timeframe(data, cfg)
    if len(bars) < max(cfg.ema_slow + 10, 50):
        return {
            "status": "inconclusive",
            "reason": "Dataset demasiado corto para un test de truncación informativo.",
            "checks": [],
        }
    model = EMACrossStrategy(cfg)
    with redirect_stdout(io.StringIO()):
        full_signals = model.generate_signals(bars, data)
        full_exits = model.prepare_exits(data, bars)

    checks: list[dict[str, Any]] = []
    for fraction in (0.5, 0.7, 0.9):
        cut = int(len(bars) * fraction)
        if cut <= cfg.ema_slow + 2 or cut >= len(bars):
            continue
        boundary = bars.index[cut]
        truncated_raw = data.loc[data.index < boundary]
        truncated_bars = to_timeframe(truncated_raw, cfg)
        if len(truncated_bars) < cut:
            checks.append({
                "fraction": fraction,
                "status": "inconclusive",
                "reason": "El truncado no conserva todas las barras completas hasta el corte.",
            })
            continue
        with redirect_stdout(io.StringIO()):
            truncated_signals = model.generate_signals(truncated_bars, truncated_raw)
            truncated_exits = model.prepare_exits(truncated_raw, truncated_bars)
        signal_changes = compare_prefix(full_signals, truncated_signals, cut)
        exit_changes = []
        for name, full_values, truncated_values in zip(
            ("adx_exit", "weak_exit", "rsi_div_exit"), full_exits, truncated_exits
        ):
            if not np.array_equal(
                np.asarray(full_values)[:cut], np.asarray(truncated_values)[:cut]
            ):
                exit_changes.append(name)
        checks.append({
            "fraction": fraction,
            "cut_utc": boundary.isoformat(),
            "bars_before_cut": cut,
            "signal_columns_changed": signal_changes,
            "exit_arrays_changed": exit_changes,
            "status": "fail" if signal_changes or exit_changes else "pass",
        })
    statuses = [item["status"] for item in checks]
    status = "fail" if "fail" in statuses else "pass" if statuses and all(
        item == "pass" for item in statuses
    ) else "inconclusive"
    return {"status": status, "checks": checks}


def init_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS experiments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                param_hash TEXT NOT NULL,
                params_json TEXT NOT NULL,
                cycle INTEGER NOT NULL,
                segment TEXT NOT NULL,
                net_profit REAL,
                return_pct REAL,
                max_dd REAL,
                pf REAL,
                sharpe REAL,
                sortino REAL,
                calmar REAL,
                win_rate REAL,
                n_trades INTEGER,
                expectancy REAL,
                top5_share REAL,
                score REAL,
                passed_filters INTEGER,
                seed INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                metrics_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(param_hash, cycle, segment)
            )
            """
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS run_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.commit()


def calculate_score(metrics: dict[str, Any], min_trades: int = 200) -> float:
    """Fixed score from config.yaml; no model-generated changes are accepted."""
    if not metrics.get("passed_filters", False):
        return 0.0
    sharpe = max(0.0, float(metrics.get("sharpe", 0.0)))
    sortino = max(0.0, float(metrics.get("sortino", 0.0)))
    pf = max(0.0, float(metrics.get("pf") or 0.0))
    calmar = max(0.0, float(metrics.get("calmar", 0.0)))
    base = (
        0.30 * min(sharpe, 3.0)
        + 0.20 * min(sortino, 4.0) / 1.5
        + 0.20 * min(pf, 3.0)
        + 0.30 * min(calmar, 3.0)
    )
    drawdown = abs(float(metrics.get("max_dd", 0.0))) / 100.0
    p_dd = 1.0 / (1.0 + (drawdown / 0.15) ** 2)
    p_n = min(1.0, int(metrics.get("n_trades", 0)) / max(1, min_trades)) ** 0.5
    p_cons = float(metrics.get("profitable_subperiod_fraction", 0.0))
    p_conc = 1.0 - max(0.0, float(metrics.get("top5_share", 0.0)) - 0.25)
    p_neigh = float(metrics.get("neighbor_ratio", 1.0))
    return float(base * p_dd * p_n * p_cons * p_conc * np.clip(p_neigh, 0, 1))


def backtest_metrics(data: pd.DataFrame, cfg: Config) -> tuple[dict[str, Any], pd.DataFrame]:
    """Backtest one already-selected chronological segment."""
    bars = to_timeframe(data, cfg)
    if len(bars) < cfg.ema_slow + 2:
        raise ValueError(
            f"Segmento insuficiente para EMA{cfg.ema_slow}: {len(bars)} barras."
        )
    strategy = EMACrossStrategy(cfg)
    with redirect_stdout(io.StringIO()):
        signals = strategy.generate_signals(bars, data)
        adx_exit, weak_exit, rsi_exit = strategy.prepare_exits(data, bars)
        trades, equity = Backtester(
            bars, cfg, signals, adx_exit, weak_exit, rsi_exit
        ).run()
    metrics = calculate_metrics(trades, equity, cfg.initial_capital)
    positions = positions_from_trades(trades)
    pnl = positions["pnl_net"].astype(float) if not positions.empty else pd.Series(dtype=float)
    gross_wins = float(pnl[pnl > 0].sum()) if len(pnl) else 0.0
    top5 = float(pnl.nlargest(5).clip(lower=0).sum()) if len(pnl) else 0.0
    top5_share = top5 / gross_wins if gross_wins > 0 else 0.0
    start = bars.index[0]
    end = bars.index[-1]
    years = max((end - start).total_seconds() / (365.25 * 24 * 3600), 1 / 365.25)
    final_equity = float(metrics["final_equity"])
    cagr_pct = (
        ((final_equity / cfg.initial_capital) ** (1 / years) - 1) * 100
        if final_equity > 0 else -100.0
    )
    dd_abs = abs(float(metrics["max_drawdown_pct"]))
    calmar = cagr_pct / dd_abs if dd_abs > 0 else (3.0 if cagr_pct > 0 else 0.0)
    if len(positions):
        edges = pd.date_range(start=start, end=end + pd.Timedelta(nanoseconds=1), periods=6)
        positions = positions.copy()
        positions["period"] = pd.cut(
            pd.to_datetime(positions["exit_time"], utc=True),
            bins=edges,
            labels=False,
            include_lowest=True,
        )
        period_profit = positions.groupby("period", observed=False)["pnl_net"].sum()
        profitable_fraction = float(
            sum(float(period_profit.get(period, 0.0)) > 0 for period in range(5)) / 5
        )
    else:
        profitable_fraction = 0.0
    net_profit = float(metrics["net_pnl"])
    pf = metrics.get("profit_factor")
    expectancy = float(pnl.mean()) if len(pnl) else 0.0
    result = {
        "net_profit": net_profit,
        "return_pct": float(metrics["return_pct"]),
        "cagr_pct": float(cagr_pct),
        "max_dd": float(metrics["max_drawdown_pct"]),
        "pf": float(pf) if pf is not None and math.isfinite(float(pf)) else None,
        "sharpe": float(metrics["sharpe_daily_365"]),
        "sortino": float(metrics["sortino_daily_365"]),
        "calmar": float(calmar),
        "win_rate": float(metrics["win_rate_pct"]),
        "n_trades": int(metrics["positions"]),
        "n_legs": int(metrics["legs"]),
        "expectancy": expectancy,
        "top5_share": float(top5_share),
        "max_consecutive_losses": int(metrics["max_consecutive_losses"]),
        "profitable_subperiod_fraction": profitable_fraction,
    }
    return result, trades


def apply_hard_filters(
    metrics: dict[str, Any],
    thresholds: dict[str, Any],
    *,
    segment: str,
) -> tuple[bool, list[str]]:
    failures: list[str] = []
    if segment == "train" and metrics["n_trades"] < thresholds["min_trades_train"]:
        failures.append("min_trades_train")
    if abs(metrics["max_dd"]) > thresholds["max_drawdown_pct"]:
        failures.append("max_drawdown_pct")
    if metrics["pf"] is None or metrics["pf"] < thresholds["min_profit_factor"]:
        failures.append("min_profit_factor")
    if metrics["expectancy"] <= thresholds["min_expectancy_net"]:
        failures.append("min_expectancy_net")
    if metrics["top5_share"] > thresholds["max_top5_trades_share"]:
        failures.append("max_top5_trades_share")
    if (
        segment == "train"
        and metrics["profitable_subperiod_fraction"]
        < thresholds["min_profitable_subperiods"]
    ):
        failures.append("min_profitable_subperiods")
    return not failures, failures


def insert_experiment(
    db_path: Path,
    *,
    param_hash: str,
    params: dict[str, Any],
    cycle: int,
    segment: str,
    metrics: dict[str, Any],
    score: float,
    passed_filters: bool,
    seed: int,
    timestamp: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO experiments (
                param_hash, params_json, cycle, segment, net_profit, return_pct,
                max_dd, pf, sharpe, sortino, calmar, win_rate, n_trades,
                expectancy, top5_share, score, passed_filters, seed, timestamp,
                metrics_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                param_hash,
                canonical_json(params),
                cycle,
                segment,
                metrics.get("net_profit"),
                metrics.get("return_pct"),
                metrics.get("max_dd"),
                metrics.get("pf"),
                metrics.get("sharpe"),
                metrics.get("sortino"),
                metrics.get("calmar"),
                metrics.get("win_rate"),
                metrics.get("n_trades"),
                metrics.get("expectancy"),
                metrics.get("top5_share"),
                score,
                int(passed_filters),
                seed,
                timestamp,
                canonical_json(metrics),
            ),
        )
        connection.commit()


def existing_experiment(db_path: Path, param_hash: str, cycle: int, segment: str) -> bool:
    with sqlite3.connect(db_path) as connection:
        result = connection.execute(
            "SELECT 1 FROM experiments WHERE param_hash = ? AND cycle = ? AND segment = ?",
            (param_hash, cycle, segment),
        ).fetchone()
    return result is not None


def csv_safe_records(records: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(records).to_csv(path, index=False)

if __name__ == "__main__":
    raise SystemExit(main())
