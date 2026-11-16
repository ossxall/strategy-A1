import io
import json
from pathlib import Path

from live.monitor import bot, config

MONITOR_CONFIG = Path(__file__).resolve().parents[1] / "live/monitor/config.yaml"


def test_monitor_config_loads_docker_health_endpoints(monkeypatch):
    monkeypatch.setenv("MONITOR_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("MONITOR_PASSWORD", "test-password")

    loaded = config.load_config(MONITOR_CONFIG)

    assert loaded.token == "test-token"
    assert loaded.password == "test-password"
    assert loaded.services["feed"].url == "http://feed:8787/live/feed/health"
    assert loaded.services["bot"].url == "http://bot:8080/live/bot/health"
    assert (
        loaded.services["executor"].url
        == "http://executor:8081/live/bot/execution/status"
    )


def test_monitor_config_requires_token_and_password_but_not_user_allowlist(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("MONITOR_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("MONITOR_PASSWORD", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "services:\n"
        "  feed:\n"
        "    url: http://localhost:8787/health\n",
        encoding="utf-8",
    )

    try:
        config.load_config(config_path)
    except ValueError as exc:
        assert "MONITOR_TELEGRAM_BOT_TOKEN" in str(exc)
    else:
        raise AssertionError("se esperaba requerir el token de Telegram")

    monkeypatch.setenv("MONITOR_TELEGRAM_BOT_TOKEN", "test-token")
    try:
        config.load_config(config_path)
    except ValueError as exc:
        assert "MONITOR_PASSWORD" in str(exc)
    else:
        raise AssertionError("se esperaba requerir la contraseña del monitor")

    monkeypatch.setenv("MONITOR_PASSWORD", "test-password")
    loaded = config.load_config(config_path)
    assert loaded.password == "test-password"


def test_auth_password_comparison_supports_unicode_and_rejects_mismatch():
    assert bot.verify_password("clave-segura-ñ", "clave-segura-ñ")
    assert not bot.verify_password("clave-incorrecta", "clave-segura-ñ")


def test_status_template_adapts_to_arbitrary_nested_json():
    payload = {
        "status": "healthy",
        "new_field": {"changed_shape": [1, "two", None]},
    }
    result = bot.CheckResult(status_code=200, payload=payload)

    message = bot.format_result("feed", "http://feed:8787/health", result)

    assert "✅ Estado de feed" in message
    assert "HTTP: 200 OK" in message
    assert json.dumps(payload, ensure_ascii=False, indent=2) in message


def test_status_template_marks_http_errors_and_clips_long_messages():
    result = bot.CheckResult(status_code=503, payload={"detail": "x" * 1000})

    message = bot.format_result(
        "feed", "http://feed:8787/health", result, max_chars=256
    )

    assert "❌ Estado de feed" in message
    assert "HTTP: 503 Service Unavailable" in message
    assert "mensaje recortado" in message
    assert len(message) <= 256


def test_fetch_endpoint_decodes_json_and_limits_body(monkeypatch):
    class FakeResponse:
        def __init__(self, body):
            self.stream = io.BytesIO(body)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def getcode(self):
            return 200

        def read(self, amount):
            return self.stream.read(amount)

    monkeypatch.setattr(
        bot, "urlopen", lambda *_args, **_kwargs: FakeResponse(b'{"ok":true}')
    )

    result = bot.fetch_endpoint("http://localhost/health", 1, 100)

    assert result.status_code == 200
    assert result.payload == {"ok": True}
    assert not result.response_truncated

    monkeypatch.setattr(
        bot, "urlopen", lambda *_args, **_kwargs: FakeResponse(b'{"ok":true}')
    )
    result = bot.fetch_endpoint("http://localhost/health", 1, 5)
    assert result.response_truncated
    assert result.raw_body is not None
