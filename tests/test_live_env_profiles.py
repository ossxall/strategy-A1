import pytest

from live.env_profiles import validate_prod_environment


def test_prod_validation_is_skipped_outside_prod(monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    validate_prod_environment("feed")


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
