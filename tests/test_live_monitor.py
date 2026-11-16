import io
from pathlib import Path

from live.monitor import bot, config

MONITOR_CONFIG = Path(__file__).resolve().parents[1] / "live/monitor/config.yaml"


def test_monitor_config_loads_health_endpoints(monkeypatch):
    monkeypatch.setenv("MONITOR_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("MONITOR_PASSWORD", "test-password")

    loaded = config.load_config(MONITOR_CONFIG)

    assert loaded.token == "test-token"
    assert loaded.password == "test-password"
    assert set(loaded.services) == {"feed", "bot", "executor"}
    assert all(
        service.url.startswith(("http://", "https://"))
        for service in loaded.services.values()
    )


def test_monitor_config_requires_token_and_password_but_not_user_allowlist(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("MONITOR_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("MONITOR_PASSWORD", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "services:\n  feed:\n    url: http://localhost:8787/health\n",
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


def test_monitor_url_environment_variable_overrides_yaml(monkeypatch):
    monkeypatch.setenv("MONITOR_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("MONITOR_PASSWORD", "test-password")
    monkeypatch.setenv("MONITOR_FEED_URL", "http://feed:8787/custom/health")

    loaded = config.load_config(MONITOR_CONFIG)

    assert loaded.services["feed"].url == "http://feed:8787/custom/health"


def test_auth_password_comparison_supports_unicode_and_rejects_mismatch():
    assert bot.verify_password("clave-segura-ñ", "clave-segura-ñ")
    assert not bot.verify_password("clave-incorrecta", "clave-segura-ñ")


def test_status_template_adapts_to_arbitrary_nested_json():
    payload = {
        "status": "healthy",
        "new_field": {"changed_shape": [1, "two", None]},
        "empty_object": {},
        "empty_array": [],
    }
    result = bot.CheckResult(status_code=200, payload=payload)

    message = bot.format_result("feed", "http://feed:8787/health", result)

    assert "✅ Monitor · feed" in message
    assert "HTTP: 200 OK" in message
    assert "📦 Respuesta JSON · objeto (4 campos)" in message
    assert '"status": "healthy"' in message
    assert '"new_field" · objeto (1 campo)' in message
    assert '"changed_shape" · lista (3 elementos)' in message
    assert "[0]: 1" in message
    assert '[1]: "two"' in message
    assert "[2]: null" in message
    assert '"empty_object" · objeto vacío {}' in message
    assert '"empty_array" · lista vacía []' in message


def test_status_template_paginates_long_messages_without_dropping_content():
    result = bot.CheckResult(status_code=503, payload={"detail": "x" * 1000})

    message = bot.format_result("feed", "http://feed:8787/health", result)
    pages = bot.paginate_message(message, max_chars=256)

    assert len(pages) > 1
    assert all(bot._utf16_length(page) <= 256 for page in pages)
    rebuilt = "".join(page.split("\n", maxsplit=1)[1] for page in pages)
    assert rebuilt == message
    assert "❌ Monitor · feed" in rebuilt
    assert "HTTP: 503 Service Unavailable" in message
    assert "x" * 1000 in rebuilt


def test_pagination_preserves_unicode_values_across_message_boundaries():
    message = bot.render_json_tree({"emoji": "🙂" * 400})

    pages = bot.paginate_message(message, max_chars=256)
    rebuilt = "".join(page.split("\n", maxsplit=1)[1] for page in pages)

    assert rebuilt == message
    assert all(bot._utf16_length(page) <= 256 for page in pages)


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


def test_fetch_public_ip_returns_validated_ip(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def getcode(self):
            return 200

        def read(self, amount):
            assert amount == 129
            return b"203.0.113.10\n"

    def fake_urlopen(request, timeout):
        assert request.full_url == bot.PUBLIC_IP_URL
        assert timeout == 2
        return FakeResponse()

    monkeypatch.setattr(bot, "urlopen", fake_urlopen)

    assert bot.fetch_public_ip(2) == "203.0.113.10"


def test_fetch_public_ip_rejects_invalid_response(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def getcode(self):
            return 200

        def read(self, _amount):
            return b"not-an-ip"

    monkeypatch.setattr(bot, "urlopen", lambda *_args, **_kwargs: FakeResponse())

    try:
        bot.fetch_public_ip(1)
    except RuntimeError as exc:
        assert "no devolvió una dirección IP válida" in str(exc)
    else:
        raise AssertionError("se esperaba rechazar una respuesta sin IP válida")
