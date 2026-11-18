from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from permutation import run as agent
from permutation.run import (
    calculate_score,
    digest_json,
    init_database,
    insert_experiment,
    load_market_data,
)


def test_default_invocation_prints_required_intake(capsys):
    assert agent.main([]) == 0
    output = capsys.readouterr().out.lower()
    assert "costes reales" in output
    assert "modo autónomo" in output
    assert "no se lanzará ninguna optimización" in output


def test_parse_model_json_accepts_json_fence():
    result = agent.parse_model_json(
        '```json\n{"verdict":"pass","code_summary":[]}\n```'
    )
    assert result["verdict"] == "pass"


def test_read_plan_ignores_mutable_workflow_status_but_detects_edits(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(agent, "RUNS_DIR", tmp_path)
    run_dir = tmp_path / "example"
    run_dir.mkdir()
    plan = {"run_id": "example", "status": "pending_approval", "grid": {"x": [1]}}
    plan["plan_hash"] = digest_json(agent.immutable_plan(plan))
    path = run_dir / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")

    plan["status"] = "approved"
    path.write_text(json.dumps(plan), encoding="utf-8")
    assert agent.read_plan(run_dir)["status"] == "approved"

    plan["grid"]["x"] = [2]
    path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(ValueError, match="modificado"):
        agent.read_plan(run_dir)


def test_spread_is_explicitly_added_as_half_per_side_to_slippage():
    config, costs = agent.make_cost_config(
        capital=25_000,
        timeframe="30min",
        commission_bps=4,
        spread_bps=2,
        slippage_bps=1,
    )
    assert config.initial_capital == 25_000
    assert config.slippage_bps == 2
    assert costs["effective_slippage_bps_in_backtester"] == 2


def test_score_is_zero_when_hard_filters_fail():
    metrics = {
        "passed_filters": False,
        "sharpe": 1,
        "sortino": 1,
        "pf": 2,
        "calmar": 1,
        "max_dd": -10,
        "n_trades": 200,
        "profitable_subperiod_fraction": 1,
        "top5_share": 0.2,
    }
    assert calculate_score(metrics) == 0


def test_grid_cannot_permute_capital_costs_or_timeframe(tmp_path: Path):
    grid = tmp_path / "grid.json"
    grid.write_text('{"commission_bps":[0,1]}', encoding="utf-8")
    with pytest.raises(ValueError, match="quedan fijos"):
        agent.load_grid(grid)


def test_default_grid_contains_exactly_the_ten_valid_trend_ema_pairs():
    grid = agent.load_grid(agent.DEFAULT_GRID)
    candidates = agent.candidate_grid(grid)

    assert candidates == [
        {"trend_ema_fast": fast, "trend_ema_slow": slow}
        for fast, slow in (
            (9, 20),
            (9, 50),
            (9, 100),
            (9, 200),
            (20, 50),
            (20, 100),
            (20, 200),
            (50, 100),
            (50, 200),
            (100, 200),
        )
    ]
    assert len(candidates) == 10


def test_results_sandbox_rejects_runs_symlink_to_external_directory(tmp_path, monkeypatch):
    outside = tmp_path / "external"
    outside.mkdir()
    link = tmp_path / "runs"
    link.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(agent, "RUNS_DIR", link)

    with pytest.raises(ValueError, match="Sandbox inválido"):
        agent.ensure_runs_directory()


def test_loader_reads_headerless_binance_csv_and_hashes_it(tmp_path: Path):
    path = tmp_path / "klines.csv"
    pd.DataFrame(
        [
            [1577836800000, 10, 11, 9, 10.5, 100, 0, 0, 0, 0, 0, 0],
            [1577836860000, 10.5, 12, 10, 11.5, 110, 0, 0, 0, 0, 0, 0],
        ]
    ).to_csv(path, index=False, header=False)

    data, quality, content_hash = load_market_data(path)
    assert len(data) == 2
    assert quality["bars"] == 2
    assert quality["median_source_interval_minutes"] == 1
    assert len(content_hash) == 64


def test_sqlite_keeps_metrics_for_resume_and_audit(tmp_path: Path):
    database = tmp_path / "experiments.db"
    init_database(database)
    insert_experiment(
        database,
        param_hash="abc",
        params={"ema_fast": 34},
        cycle=1,
        segment="train",
        metrics={"net_profit": 123.0, "passed_filters": True},
        score=0.4,
        passed_filters=True,
        seed=42,
        timestamp="2026-11-17T00:00:00+00:00",
    )
    import sqlite3

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT metrics_json, score FROM experiments WHERE param_hash = 'abc'"
        ).fetchone()
    assert json.loads(row[0])["net_profit"] == 123
    assert row[1] == 0.4


def test_prepare_creates_pending_plan_without_running_search(tmp_path: Path, monkeypatch):
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(agent, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(agent, "ensure_runs_directory", lambda: None)
    monkeypatch.setattr(
        agent,
        "lookahead_check",
        lambda *_: {"status": "pass", "checks": [{"status": "pass"}]},
    )
    monkeypatch.setattr(
        agent,
        "call_opencode",
        lambda *_: json.dumps({
            "verdict": "pass",
            "code_summary": ["Estrategia comprobada."],
            "lookahead_findings": [],
            "other_risks": [],
            "parameterization_notes": [],
        }),
    )
    timestamps = pd.date_range("2020-01-01", periods=1200, freq="30min", tz="UTC")
    prices = [100 + (index % 20) * 0.1 for index in range(len(timestamps))]
    dataset = tmp_path / "bars.csv"
    pd.DataFrame({
        "timestamp": timestamps,
        "open": prices,
        "high": [price + 0.2 for price in prices],
        "low": [price - 0.2 for price in prices],
        "close": prices,
        "volume": [10] * len(prices),
    }).to_csv(dataset, index=False)
    grid = tmp_path / "grid.json"
    grid.write_text('{"ema_fast":[34]}', encoding="utf-8")
    policy = tmp_path / "config.yaml"
    settings = agent.load_policy()
    settings["embargo_bars"] = 0
    policy.write_text(json.dumps(settings), encoding="utf-8")
    args = argparse.Namespace(
        dataset=dataset,
        source="test fixture",
        instrument="TEST",
        timeframe="30min",
        capital=10_000.0,
        commission_bps=1.0,
        spread_bps=1.0,
        slippage_bps=1.0,
        leverage=1.0,
        contract_size=1.0,
        grid=grid,
        policy=policy,
        autonomous=False,
    )

    assert agent.prepare(args) == 0
    run_dir = next(runs_dir.iterdir())
    plan = agent.read_plan(run_dir)
    assert plan["status"] == "pending_approval"
    assert plan["eligible_for_optimization"] is True
    assert not (run_dir / "experiments.db").exists()

    assert agent.approve(argparse.Namespace(
        run_id=plan["run_id"],
        plan_hash=plan["plan_hash"],
    )) == 0
    approved = agent.read_plan(run_dir)
    agent.validate_approval(run_dir, approved)
    assert approved["status"] == "approved"
    assert not (run_dir / "experiments.db").exists()
