import runpy
import sys
import types
from pathlib import Path


def test_feed_config_loads_live_env_without_overriding_process_environment(
    monkeypatch,
):
    monkeypatch.delenv("FEED_START_DATE", raising=False)
    calls = []
    dotenv = types.ModuleType("dotenv")

    def fake_load_dotenv(dotenv_path, *, override):
        calls.append((Path(dotenv_path), override))

    dotenv.load_dotenv = fake_load_dotenv
    monkeypatch.setitem(sys.modules, "dotenv", dotenv)

    config_path = Path(__file__).resolve().parents[1] / "live/feed/config.py"
    config = runpy.run_path(str(config_path))

    expected_env = config_path.resolve().parents[1] / ".env"
    assert calls == [(expected_env, False)]
    example_path = expected_env.with_name(".env.example")
    example_start_date = next(
        line.partition("=")[2]
        for line in example_path.read_text().splitlines()
        if line.startswith("FEED_START_DATE=")
    )
    assert config["START_DATE"] == example_start_date
