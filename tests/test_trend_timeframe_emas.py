import numpy as np
import pandas as pd
import pytest

from strategy.config import Config
from strategy.strategy import EMACrossStrategy


def _bars(periods=4 * 48, freq="30min"):
    index = pd.date_range("2026-01-01", periods=periods, freq=freq, tz="UTC")
    close = np.arange(periods, dtype=float) + 100.0
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.1,
            "low": close - 0.1,
            "close": close,
            "volume": 1.0,
        },
        index=index,
    )


def test_trend_ema_lengths_default_to_entry_ema_periods():
    config = Config()

    assert config.trend_ema_lengths == (config.ema_fast, config.ema_slow)


def test_trend_bias_uses_configured_ema_periods(monkeypatch):
    bars = _bars()
    config = Config(
        timeframe="30min",
        trend_timeframe="1D",
        ema_fast=3,
        ema_slow=8,
        trend_ema_fast=2,
        trend_ema_slow=5,
    )
    calls = []

    def fake_ema(values, period):
        calls.append(period)
        return pd.Series(
            np.ones(len(values)) if period == 2 else np.zeros(len(values)),
            index=values.index,
        )

    monkeypatch.setattr("strategy.strategy.ema", fake_ema)

    bias = EMACrossStrategy(config).trend_bias(bars)

    assert calls == [2, 5]
    assert (bias[48:] == 1).all()


@pytest.mark.parametrize(
    "overrides",
    [
        {"trend_ema_fast": -1},
        {"trend_ema_slow": -1},
        {"trend_ema_fast": 8, "trend_ema_slow": 5},
        {"ema_fast": 8, "ema_slow": 21, "trend_ema_slow": 5},
    ],
)
def test_trend_ema_periods_are_validated(overrides):
    with pytest.raises(ValueError, match="trend_ema"):
        Config(**overrides)


def test_invalid_trend_ema_periods_are_ignored_when_filter_is_disabled():
    config = Config(streak_trend_only=False, trend_ema_fast=-1)

    assert config.trend_ema_fast == -1
