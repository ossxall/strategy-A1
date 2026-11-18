import os

import pytest

from live.bot.env import load_direct_profile


def test_direct_executor_loads_dev_yaml_and_only_its_private_settings(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("BOT_MODE", raising=False)
    monkeypatch.delenv("BOT_PROFILE_VALUE", raising=False)
    monkeypatch.delenv("BOT_COMMON_VALUE", raising=False)
    monkeypatch.delenv("BOT_BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("MONITOR_PASSWORD", raising=False)
    (tmp_path / "config.dev.yaml").write_text(
        'BOT_MODE: "backtest"\nBOT_PROFILE_VALUE: "yaml"\n',
        encoding="utf-8",
    )
    (tmp_path / ".env.dev").write_text(
        "BOT_MODE=legacy\n"
        "BOT_COMMON_VALUE=legacy\n"
        "BOT_BINANCE_API_KEY=dev-key\n"
        "MONITOR_PASSWORD=monitor-password\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("BOT_COMMON_VALUE=common\n", encoding="utf-8")
    monkeypatch.setenv("BOT_PROFILE_VALUE", "shell")

    load_direct_profile(tmp_path, service="executor")

    assert os.environ["BOT_MODE"] == "backtest"
    assert os.environ["BOT_PROFILE_VALUE"] == "shell"
    assert "BOT_COMMON_VALUE" not in os.environ
    assert os.environ["BOT_BINANCE_API_KEY"] == "dev-key"
    assert "MONITOR_PASSWORD" not in os.environ


def test_direct_executor_uses_yaml_selected_by_app_env_and_loads_only_secrets(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.delenv("BOT_MODE", raising=False)
    monkeypatch.delenv("BOT_BASE_ONLY", raising=False)
    monkeypatch.delenv("BOT_PROD_ONLY", raising=False)
    monkeypatch.delenv("BOT_BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("MONITOR_PASSWORD", raising=False)
    (tmp_path / "config.dev.yaml").write_text(
        'BOT_MODE: "development"\nBOT_BASE_ONLY: "from-dev"\n',
        encoding="utf-8",
    )
    (tmp_path / "config.prod.yaml").write_text(
        'BOT_MODE: "production"\nBOT_PROD_ONLY: "from-prod"\n',
        encoding="utf-8",
    )
    (tmp_path / ".env.prod").write_text(
        "BOT_MODE=legacy\n"
        "BOT_BINANCE_API_KEY=prod-key\n"
        "MONITOR_PASSWORD=monitor-password\n",
        encoding="utf-8",
    )

    load_direct_profile(tmp_path, service="executor")

    assert os.environ["BOT_MODE"] == "production"
    assert os.environ["BOT_BASE_ONLY"] == "from-dev"
    assert os.environ["BOT_PROD_ONLY"] == "from-prod"
    assert os.environ["BOT_BINANCE_API_KEY"] == "prod-key"
    assert "MONITOR_PASSWORD" not in os.environ


def test_profile_yaml_cannot_contain_private_keys(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.delenv("APP_ENV", raising=False)
    (tmp_path / "config.dev.yaml").write_text(
        'BOT_BINANCE_API_SECRET: "must-not-be-here"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="privada"):
        load_direct_profile(tmp_path, service="bot")


def test_profile_yaml_cannot_configure_strategy(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.delenv("APP_ENV", raising=False)
    (tmp_path / "config.dev.yaml").write_text(
        'BOT_STRATEGY_RISK_PCT: "0.5"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="strategy/config.py"):
        load_direct_profile(tmp_path, service="bot")
