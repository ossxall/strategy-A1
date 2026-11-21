import numpy as np
import pandas as pd
import pytest

import strategy.indicators as indicators
from strategy.config import Config
from strategy.indicators import RSIDivergenceSignals
from strategy.utils import resample_ohlcv


def make_raw(periods=900, freq="1min"):
    index = pd.date_range("2026-01-01", periods=periods, freq=freq, tz="UTC")
    close = np.linspace(100.0, 110.0, periods)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 1.0,
        },
        index=index,
    )


def test_lower_rsi_divergence_timeframe_uses_raw_bars_at_requested_resolution(
    monkeypatch,
):
    raw = make_raw()
    bars = resample_ohlcv(raw, "30min")
    observed_lengths = []

    def fake_divergences(high, low, rsi_values, pivot, lookback):
        observed_lengths.append(len(high))
        bullish = np.zeros(len(high), dtype=bool)
        bearish = np.zeros(len(high), dtype=bool)
        bullish[5] = True
        return RSIDivergenceSignals(bullish, bearish)

    monkeypatch.setattr(indicators, "rsi_divergence_signals", fake_divergences)
    config = Config(
        timeframe="30min",
        exit_rsi_divergence_timeframe="5min",
        exit_rsi_divergence_enabled=True,
        entry_rsi_divergence_enabled=False,
        rsi_divergence_pivot=1,
        rsi_divergence_len=2,
        entry_adx_enabled=False,
        entry_atr_enabled=False,
        adx_exit_enabled=False,
        ema_weakening_exit_enabled=False,
        streak_trend_only=False,
    )

    flags = indicators.build_rsi_divergence_exit(raw, bars, config)

    assert observed_lengths == [180]
    assert flags[0] == 1


def test_entry_and_exit_divergence_use_independent_timeframes(monkeypatch):
    raw = make_raw()
    bars = resample_ohlcv(raw, "30min")
    observed_lengths = []

    def fake_divergences(high, low, rsi_values, pivot, lookback):
        observed_lengths.append(len(high))
        zeros = np.zeros(len(high), dtype=bool)
        return RSIDivergenceSignals(zeros, zeros)

    monkeypatch.setattr(indicators, "rsi_divergence_signals", fake_divergences)
    config = Config(
        timeframe="30min",
        entry_rsi_divergence_timeframe="5min",
        exit_rsi_divergence_timeframe="1h",
        exit_rsi_divergence_enabled=True,
        entry_rsi_divergence_enabled=True,
        rsi_divergence_pivot=1,
        entry_adx_enabled=False,
        entry_atr_enabled=False,
        adx_exit_enabled=False,
        ema_weakening_exit_enabled=False,
        streak_trend_only=False,
    )

    entry_flags = indicators.build_rsi_divergence_flags(raw, bars, config)
    exit_flags = indicators.build_rsi_divergence_exit(raw, bars, config)

    assert observed_lengths == [180, 15]
    assert entry_flags.shape == exit_flags.shape == (len(bars),)


def test_lower_rsi_divergence_timeframe_rejects_insufficient_raw_resolution():
    raw = make_raw(periods=30, freq="1h")
    bars = resample_ohlcv(make_raw(periods=60), "30min")
    config = Config(
        timeframe="30min",
        exit_rsi_divergence_timeframe="5min",
        exit_rsi_divergence_enabled=True,
        entry_rsi_divergence_enabled=False,
        entry_adx_enabled=False,
        entry_atr_enabled=False,
        adx_exit_enabled=False,
        ema_weakening_exit_enabled=False,
        streak_trend_only=False,
    )

    with pytest.raises(ValueError, match="resolución suficiente"):
        indicators.build_rsi_divergence_exit(raw, bars, config)


def test_entry_and_exit_rsi_divergence_timeframes_are_independent():
    config = Config(
        entry_rsi_divergence_timeframe=("1h", "2h"),
        exit_rsi_divergence_timeframe=("4h", "1D"),
    )

    assert config.entry_rsi_divergence_timeframes == ("1h", "2h")
    assert config.exit_rsi_divergence_timeframes == ("4h", "1D")
