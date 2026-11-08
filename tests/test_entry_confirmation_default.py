import sys

from strategy.config import Config
from strategy.strategy import parse_args


def test_entry_confirmation_defaults_to_pullback_retest():
    assert Config().entry_confirm_mode == "pullback_retest"


def test_cli_entry_confirmation_defaults_to_pullback_retest(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["strategy.py", "sample.parquet"])

    assert parse_args().entry_confirm_mode == "pullback_retest"
