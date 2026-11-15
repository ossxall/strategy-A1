import sys
import types
from pathlib import Path

from live.bot.executor import _load_direct_profile


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

    _load_direct_profile(tmp_path)

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

    _load_direct_profile(tmp_path)

    assert calls[0] == tmp_path / ".env.prod"
