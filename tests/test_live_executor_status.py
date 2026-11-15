import json
import threading
import time
from urllib.error import HTTPError
from urllib.request import urlopen

from live.bot.execution.status import (
    ExecutionHealth,
    start_execution_status_server,
)
from live.bot.execution.worker import ExecutorSettings


def _settings(tmp_path, **overrides):
    values = {
        "enabled": False,
        "test_mode": False,
        "environment": "testnet",
        "allow_live_trading": False,
        "account_id": "status-test-account",
        "api_key": "must-not-be-exposed",
        "api_secret": "must-not-be-exposed",
        "symbol": "BTCUSDT",
        "poll_seconds": 1.0,
        "account_poll_seconds": 15.0,
        "lease_seconds": 30,
        "max_entry_age_seconds": 90,
        "initial_backfill_seconds": 90,
        "max_leverage": 10,
        "set_leverage": True,
        "max_equity_fraction": 0.95,
        "max_risk_pct": 0.03,
        "heartbeat_path": tmp_path / "executor.heartbeat",
        "signal_db": tmp_path / "signal.db",
    }
    values.update(overrides)
    return ExecutorSettings(**values)


def _start_server(health):
    server, http_thread = start_execution_status_server(
        health,
        "127.0.0.1",
        0,
        worker_thread=threading.current_thread(),
    )
    return server, http_thread, (
        f"http://127.0.0.1:{server.server_address[1]}"
    )


def _stop_server(health, server, http_thread):
    health.mark_stopped()
    server.shutdown()
    server.server_close()
    http_thread.join(timeout=1)


def test_execution_status_and_health_alias_return_json_snapshot(tmp_path):
    health = ExecutionHealth(_settings(tmp_path))
    health.mark_running()
    health.begin_cycle()
    health.finish_cycle(status="ok")
    server, http_thread, base_url = _start_server(health)
    try:
        for route in (
            "/live/bot/execution/status",
            "/live/bot/execution/health",
        ):
            with urlopen(base_url + route, timeout=1) as response:
                payload = json.load(response)
                assert response.status == 200
                assert response.headers["Content-Type"].startswith(
                    "application/json"
                )
                assert payload["status"] == "healthy"
                assert payload["config"]["enabled"] is False
                assert payload["executor"]["successful_cycle_count"] == 1
                assert payload["checks"]["http_server_alive"] is True
                assert "must-not-be-exposed" not in json.dumps(payload)
    finally:
        _stop_server(health, server, http_thread)


def test_execution_status_returns_promptly_if_snapshot_lock_is_held(tmp_path):
    health = ExecutionHealth(_settings(tmp_path))
    health.mark_running()
    server, http_thread, base_url = _start_server(health)
    health._lock.acquire()
    started = time.monotonic()
    try:
        try:
            urlopen(
                base_url + "/live/bot/execution/status",
                timeout=0.5,
            )
        except HTTPError as response:
            payload = json.load(response)
            assert response.code == 503
            assert payload["healthy"] is False
        assert time.monotonic() - started < 0.5
    finally:
        health._lock.release()
        _stop_server(health, server, http_thread)


def test_enabled_executor_reports_account_and_reconciliation_health(tmp_path):
    health = ExecutionHealth(_settings(tmp_path, enabled=True))
    health.mark_running()
    health.record_worker_initialized(True)
    health.record_account_snapshot(
        {
            "wallet_balance": 100.0,
            "available_balance": 90.0,
            "positions": [{"symbol": "BTCUSDT", "position_amt": 0.1}],
            "open_orders": [],
            "open_algo_orders": [{"clientAlgoId": "stop"}],
        }
    )
    health.begin_cycle()
    health.finish_cycle(status="ok")
    server, http_thread, base_url = _start_server(health)
    try:
        with urlopen(
            base_url + "/live/bot/execution/status",
            timeout=1,
        ) as response:
            payload = json.load(response)
        assert response.status == 200
        assert payload["account"]["wallet_balance"] == 100.0
        assert payload["account"]["open_algo_orders_count"] == 1
        assert payload["checks"]["account_consistent"] is True
    finally:
        _stop_server(health, server, http_thread)
