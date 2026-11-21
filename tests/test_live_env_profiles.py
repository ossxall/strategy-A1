from pathlib import Path

import pytest
import yaml

from live.env_profiles import validate_prod_environment


def test_prod_validation_is_skipped_outside_prod(monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    validate_prod_environment("feed")


def test_compose_passes_app_env_to_each_profiled_service():
    compose_path = Path(__file__).resolve().parents[1] / "live/docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

    for service in ("feed", "bot", "executor", "webssh", "sqlweb", "monitor"):
        assert compose["services"][service]["environment"]["APP_ENV"] == (
            "${APP_ENV:-dev}"
        )


def test_dev_compose_does_not_enable_production_integrations_by_default():
    compose_path = Path(__file__).resolve().parents[1] / "live/docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

    for service in ("feed", "bot", "executor"):
        assert "profiles" not in compose["services"][service]

    production_services = (
        "authelia",
        "dozzle",
        "glances",
        "nginx",
        "redis",
        "sqlweb",
        "webssh",
    )
    for service in production_services:
        assert "production" in compose["services"][service]["profiles"]
    assert "production" in compose["services"]["monitor"]["profiles"]
    assert "monitor" in compose["services"]["monitor"]["profiles"]


def test_dev_profile_keeps_binance_trading_disabled():
    config_path = Path(__file__).resolve().parents[1] / "live/config.dev.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert config["BOT_BINANCE_TRADING_ENABLED"] == "false"


def test_prod_bot_does_not_require_strategy_environment_variables(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("BOT_SYMBOL", "BTCUSDT")
    monkeypatch.setenv("BOT_INTERVAL", "1m")
    monkeypatch.setenv("BOT_LIVE_ONLY", "true")
    for name in (
        "BOT_STRATEGY_TIMEFRAME",
        "BOT_STRATEGY_RISK_PCT",
        "BOT_STRATEGY_STOP_LOSS_PCT",
    ):
        monkeypatch.delenv(name, raising=False)

    validate_prod_environment("bot")


def test_feed_reports_missing_prod_variables_with_red_x(monkeypatch, capsys):
    monkeypatch.setenv("APP_ENV", "prod")
    for name in ("FEED_SYMBOL", "FEED_INTERVAL", "FEED_START_DATE", "FEED_DB_PATH"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(SystemExit) as error:
        validate_prod_environment("feed")

    assert error.value.code == 2
    message = capsys.readouterr().err
    assert "❌ APP_ENV=prod" in message
    assert "FEED_SYMBOL" in message


def test_executor_requires_credentials_and_live_opt_in_when_trading(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("BOT_BINANCE_ENVIRONMENT", "production")
    monkeypatch.setenv("BOT_BINANCE_TRADING_ENABLED", "true")
    monkeypatch.setenv("BOT_BINANCE_ACCOUNT_ID", "prod-account")
    monkeypatch.setenv("BOT_SIGNAL_DB", "/data/signal.db")
    for name in (
        "BOT_BINANCE_API_KEY",
        "BOT_BINANCE_API_SECRET",
        "BOT_BINANCE_ALLOW_LIVE_TRADING",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(SystemExit) as error:
        validate_prod_environment("executor")

    assert error.value.code == 2


def test_executor_can_stay_disabled_without_exchange_credentials(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("BOT_BINANCE_ENVIRONMENT", "production")
    monkeypatch.setenv("BOT_BINANCE_TRADING_ENABLED", "false")
    monkeypatch.setenv("BOT_BINANCE_ACCOUNT_ID", "prod-account")
    monkeypatch.setenv("BOT_SIGNAL_DB", "/data/signal.db")
    monkeypatch.delenv("BOT_BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BOT_BINANCE_API_SECRET", raising=False)

    validate_prod_environment("executor")
