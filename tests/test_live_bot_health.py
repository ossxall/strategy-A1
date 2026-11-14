import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from live.bot.health import (
    BotHealth,
    probe_signal_database,
    start_health_server,
)


class _RawFeed:
    empty = False

    def __init__(self):
        self.index = [datetime.now(timezone.utc)]

    def __len__(self):
        return 1


def _make_health(tmp_path: Path) -> BotHealth:
    signal_path = tmp_path / "signal.db"
    connection = sqlite3.connect(signal_path)
    connection.execute("CREATE TABLE signals (signal_time INTEGER)")
    connection.execute("INSERT INTO signals(signal_time) VALUES (123)")
    connection.commit()
    connection.close()

    health = BotHealth(
        symbol="BTCUSDT",
        interval="1m",
        timeframe="30min",
        feed_path=tmp_path / "feed.db",
        signal_path=signal_path,
        poll_seconds=1,
        min_bars=2,
        live_only=True,
        live_since_ms=123,
        probe_interval_seconds=1,
    )
    health.mark_running()
    health.begin_cycle()
    health.record_feed(_RawFeed())
    health.record_strategy_ready(True)
    health.finish_cycle(status="ok", duration_seconds=0.01, events_inserted=1)
    health.record_database_probe(probe_signal_database(signal_path, 0.2))
    return health


def test_health_and_status_routes_return_nonblocking_json(tmp_path):
    signal_path = tmp_path / "signal.db"
    health = _make_health(tmp_path)
    server, http_thread, probe_thread = start_health_server(
        health,
        "127.0.0.1",
        0,
        signal_path,
        probe_interval=1,
    )
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        for route in ("/live/bot/health", "/live/bot/status"):
            with urlopen(base_url + route, timeout=1) as response:
                payload = json.load(response)
                assert response.status == 200
                assert response.headers["Content-Type"].startswith(
                    "application/json"
                )
                assert payload["healthy"] is True
                assert payload["status"] == "healthy"
                assert payload["checks"]["signal_database_available"] is True
                assert payload["feed"]["rows"] == 1
    finally:
        health.stop()
        server.shutdown()
        server.server_close()
        http_thread.join(timeout=1)
        probe_thread.join(timeout=1)


def test_backtest_route_serves_html_and_data_without_escaping_report_dir(tmp_path):
    html_dir = tmp_path / "backtest" / "html"
    (html_dir / "data" / "pages").mkdir(parents=True)
    (html_dir / "backtest.html").write_text("<h1>visor live</h1>", encoding="utf-8")
    (html_dir / "data" / "manifest.json").write_text('{"pages":[]}', encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("no publicar", encoding="utf-8")

    health = _make_health(tmp_path)
    server, http_thread, probe_thread = start_health_server(
        health,
        "127.0.0.1",
        0,
        tmp_path / "signal.db",
        backtest_dir=html_dir,
    )
    base_url = f"http://127.0.0.1:{server.server_address[1]}/live/bot/backtest"
    try:
        with urlopen(base_url + "/", timeout=1) as response:
            assert response.status == 200
            assert response.headers["Content-Type"].startswith("text/html")
            assert response.read() == b"<h1>visor live</h1>"

        with urlopen(base_url + "/data/manifest.json", timeout=1) as response:
            assert response.headers["Content-Type"].startswith("application/json")
            assert json.load(response) == {"pages": []}

        try:
            urlopen(base_url + "/%2e%2e/secret.txt", timeout=1)
        except HTTPError as response:
            assert response.code == 404
        else:
            raise AssertionError("El endpoint no debe servir fuera de html/")
    finally:
        health.stop()
        server.shutdown()
        server.server_close()
        http_thread.join(timeout=1)
        probe_thread.join(timeout=1)


def test_health_endpoint_returns_promptly_if_snapshot_lock_is_held(tmp_path):
    health = _make_health(tmp_path)
    server, http_thread, probe_thread = start_health_server(
        health,
        "127.0.0.1",
        0,
        tmp_path / "signal.db",
    )
    url = f"http://127.0.0.1:{server.server_address[1]}/live/bot/health"
    health._lock.acquire()
    started = time.monotonic()
    try:
        try:
            urlopen(url, timeout=0.5)
        except HTTPError as response:
            payload = json.load(response)
            assert response.code == 503
            assert payload["healthy"] is False
        assert time.monotonic() - started < 0.5
    finally:
        health._lock.release()
        health.stop()
        server.shutdown()
        server.server_close()
        http_thread.join(timeout=1)
        probe_thread.join(timeout=1)


def test_stale_feed_is_reported_as_degraded_by_both_routes(tmp_path):
    signal_path = tmp_path / "signal.db"
    health = _make_health(tmp_path)
    stale_feed = _RawFeed()
    stale_feed.index = [datetime.now(timezone.utc) - timedelta(minutes=10)]
    health.record_feed(stale_feed)
    server, http_thread, probe_thread = start_health_server(
        health,
        "127.0.0.1",
        0,
        signal_path,
        probe_interval=1,
    )
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        for route in ("/live/bot/health", "/live/bot/status"):
            try:
                urlopen(base_url + route, timeout=1)
            except HTTPError as response:
                payload = json.load(response)
                assert response.code == 503
                assert payload["status"] == "degraded"
                assert payload["healthy"] is False
                assert payload["checks"]["feed_candle_fresh"] is False
            else:
                raise AssertionError(f"{route} debía responder HTTP 503")
    finally:
        health.stop()
        server.shutdown()
        server.server_close()
        http_thread.join(timeout=1)
        probe_thread.join(timeout=1)


def test_build_payload_reports_healthy_when_all_health_checks_pass(tmp_path):
    health = _make_health(tmp_path)
    current_thread = threading.current_thread()
    health.attach_threads(current_thread, current_thread)

    payload = health.build_payload("127.0.0.1", 8080, 1.0)

    assert payload["status"] == "healthy"
    assert payload["healthy"] is True
    assert all(payload["checks"].values())
    assert payload["bot"]["last_cycle_status"] == "ok"
    assert payload["signal_database"]["available"] is True


def test_build_payload_reports_degraded_when_last_cycle_failed(tmp_path):
    health = _make_health(tmp_path)
    current_thread = threading.current_thread()
    health.attach_threads(current_thread, current_thread)
    health.finish_cycle(
        status="error",
        duration_seconds=0.25,
        error="fallo simulado",
    )

    payload = health.build_payload("127.0.0.1", 8080, 1.0)

    assert payload["status"] == "degraded"
    assert payload["healthy"] is False
    assert payload["checks"]["last_cycle_ok"] is False
    assert payload["bot"]["last_cycle_error"] == "fallo simulado"


def test_build_payload_reports_unhealthy_when_signal_database_is_unavailable(
    tmp_path,
):
    health = _make_health(tmp_path)
    current_thread = threading.current_thread()
    health.attach_threads(current_thread, current_thread)
    health.record_database_probe(
        probe_signal_database(tmp_path / "missing.db", timeout_seconds=0.1)
    )

    payload = health.build_payload("127.0.0.1", 8080, 1.0)

    assert payload["status"] == "unhealthy"
    assert payload["healthy"] is False
    assert payload["checks"]["signal_database_available"] is False
