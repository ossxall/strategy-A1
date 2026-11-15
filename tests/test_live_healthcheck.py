import json
import os
import time

from live import healthcheck


class _Response:
    def __init__(self, payload, status=200):
        self.status = status
        self.body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit):
        return self.body


def _healthy_payload():
    return {"status": "healthy", "healthy": True}


def test_bot_healthcheck_requires_a_healthy_application_endpoint(
    tmp_path, monkeypatch
):
    heartbeat = tmp_path / "bot.heartbeat"
    heartbeat.write_text("alive")
    monkeypatch.setenv("BOT_HEARTBEAT", str(heartbeat))
    monkeypatch.setenv("BOT_HTTP_PORT", "8080")
    monkeypatch.setenv("BOT_HEALTHCHECK_MAX_AGE", "180")
    urls = []

    def fake_urlopen(url, timeout):
        urls.append((url, timeout))
        return _Response({"status": "degraded", "healthy": False}, status=503)

    monkeypatch.setattr(healthcheck, "urlopen", fake_urlopen)

    assert healthcheck.check_bot() is False
    assert urls == [
        ("http://127.0.0.1:8080/live/bot/health", 2.0),
    ]


def test_future_heartbeat_is_not_accepted_as_fresh(tmp_path):
    heartbeat = tmp_path / "future.heartbeat"
    heartbeat.write_text("clock skew")
    future = time.time() + 60
    os.utime(heartbeat, (future, future))

    assert healthcheck.check_heartbeat(heartbeat, "180", "bot") is False


def test_executor_healthcheck_probes_status_when_trading_is_disabled(
    tmp_path, monkeypatch
):
    heartbeat = tmp_path / "executor.heartbeat"
    heartbeat.write_text("alive")
    monkeypatch.setenv("BOT_EXECUTOR_HEARTBEAT", str(heartbeat))
    monkeypatch.setenv("BOT_EXECUTOR_HTTP_PORT", "8081")
    monkeypatch.setenv("BOT_BINANCE_TRADING_ENABLED", "false")
    urls = []

    def fake_urlopen(url, timeout):
        urls.append(url)
        return _Response(_healthy_payload())

    monkeypatch.setattr(healthcheck, "urlopen", fake_urlopen)

    assert healthcheck.check_executor() is True
    assert urls == ["http://127.0.0.1:8081/live/bot/execution/status"]


def test_feed_healthcheck_requires_database_and_application_health(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "feed.db"
    db_path.touch()
    now_ms = int(time.time() * 1000)

    class _Cursor:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class _Connection:
        def execute(self, query):
            if "collector_state" in query:
                return _Cursor(("running",))
            return _Cursor((now_ms,))

        def close(self):
            pass

    monkeypatch.setattr(
        healthcheck,
        "connect_sqlite",
        lambda *_a, **_kw: _Connection(),
    )
    monkeypatch.setenv("FEED_DB_PATH", str(db_path))
    monkeypatch.setenv("FEED_STATUS_PORT", "8787")
    urls = []

    def fake_urlopen(url, timeout):
        urls.append(url)
        return _Response(_healthy_payload())

    monkeypatch.setattr(healthcheck, "urlopen", fake_urlopen)

    assert healthcheck.check_feed() is True
    assert urls == ["http://127.0.0.1:8787/live/feed/health"]
