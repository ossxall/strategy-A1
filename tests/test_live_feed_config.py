import os
import runpy
from pathlib import Path

import yaml

from live.env_profiles import load_direct_profile


def test_feed_config_uses_dev_yaml_without_overriding_process_environment(
    monkeypatch,
):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.delenv("FEED_START_DATE", raising=False)

    config_path = Path(__file__).resolve().parents[1] / "live/feed/config.py"
    live_dir = config_path.resolve().parents[1]
    monkeypatch.syspath_prepend(str(live_dir))
    load_direct_profile(live_dir, service="feed")
    config = runpy.run_path(str(config_path))

    profile = yaml.safe_load((live_dir / "config.dev.yaml").read_text())
    assert config["START_DATE"] == profile["FEED_START_DATE"]
