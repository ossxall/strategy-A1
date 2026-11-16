import runpy
import sys
import types
from pathlib import Path


def test_legacy_feed_launcher_loads_profile_before_running_feed(monkeypatch):
    live_dir = Path(__file__).resolve().parents[1] / "live"
    calls = []

    profiles = types.ModuleType("env_profiles")

    def fake_load_direct_profile(directory):
        calls.append(("profile", Path(directory)))

    profiles.load_direct_profile = fake_load_direct_profile
    monkeypatch.setitem(sys.modules, "env_profiles", profiles)

    feed_package = types.ModuleType("feed")
    feed_package.__path__ = []
    feed_cli = types.ModuleType("feed.cli")
    feed_cli.run = lambda: calls.append(("run", None))
    monkeypatch.setitem(sys.modules, "feed", feed_package)
    monkeypatch.setitem(sys.modules, "feed.cli", feed_cli)
    monkeypatch.setattr(sys, "path", sys.path.copy())

    runpy.run_path(str(live_dir / "feed.py"), run_name="__main__")

    assert calls == [("profile", live_dir), ("run", None)]
