import os
import sys
import types
from pathlib import Path

from live.bot.env import load_direct_profile


def test_direct_executor_loads_dev_profile_before_common_env(monkeypatch, tmp_path):
    monkeypatch.delenv("APP_ENV", raising=False)
    (tmp_path / ".env.dev").touch()
    calls = []
    dotenv = types.ModuleType("dotenv")

    def fake_load_dotenv(dotenv_path=None, *, override):
        calls.append(
            (Path(dotenv_path) if dotenv_path is not None else None, override)
        )

    dotenv.load_dotenv = fake_load_dotenv
    monkeypatch.setitem(sys.modules, "dotenv", dotenv)

    load_direct_profile(tmp_path)

    assert calls == [
        (tmp_path / ".env.dev", False),
        (tmp_path / ".env", False),
        (None, False),
    ]


def test_direct_executor_uses_profile_selected_by_app_env(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_ENV", "prod")
    (tmp_path / ".env.prod").touch()
    calls = []
    dotenv = types.ModuleType("dotenv")

    def fake_load_dotenv(dotenv_path=None, *, override):
        calls.append(Path(dotenv_path) if dotenv_path is not None else None)

    dotenv.load_dotenv = fake_load_dotenv
    monkeypatch.setitem(sys.modules, "dotenv", dotenv)

    load_direct_profile(tmp_path)

    assert calls[0] == tmp_path / ".env.prod"


def test_direct_profile_falls_back_to_simple_env_files_without_dotenv(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("BOT_MODE", raising=False)
    monkeypatch.setenv("BOT_PROFILE_VALUE", "shell")
    monkeypatch.setitem(sys.modules, "dotenv", None)
    (tmp_path / ".env.dev").write_text(
        "BOT_MODE=backtest\nBOT_PROFILE_VALUE=profile\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "BOT_MODE=live\nBOT_COMMON_VALUE=common\n",
        encoding="utf-8",
    )

    load_direct_profile(tmp_path)

    assert os.environ["BOT_MODE"] == "backtest"
    assert os.environ["BOT_PROFILE_VALUE"] == "shell"
    assert os.environ["BOT_COMMON_VALUE"] == "common"
