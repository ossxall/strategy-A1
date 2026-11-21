import numpy as np
import pandas as pd
import pytest

from live.bot.app import SignalBot
from strategy.config import Config
from strategy.strategy import Backtester, EMACrossStrategy
from strategy.utils import to_timeframe


def make_bars(periods, interval):
    index = pd.date_range(
        "2026-01-01", periods=periods, freq=interval, tz="UTC"
    )
    return pd.DataFrame(
        {
            "open": np.full(periods, 100.0),
            "high": np.full(periods, 101.0),
            "low": np.full(periods, 99.0),
            "close": np.full(periods, 100.0),
            "volume": np.ones(periods),
        },
        index=index,
    )


def test_to_timeframe_rejects_non_minute_raw_data():
    cfg = Config(timeframe="30min")

    with pytest.raises(ValueError, match="raw dataset.*resolución de 1m"):
        to_timeframe(make_bars(10, "5min"), cfg)


def test_to_timeframe_rejects_unsorted_raw_timestamps():
    cfg = Config(timeframe="1min")
    raw = make_bars(3, "5min").iloc[::-1]

    with pytest.raises(ValueError, match="raw dataset.*orden cronológico"):
        to_timeframe(raw, cfg)


def test_to_timeframe_rejects_duplicate_raw_timestamps():
    cfg = Config(timeframe="1min")
    raw = make_bars(3, "1min")
    raw = pd.concat([raw, raw.iloc[[1]]]).sort_index()

    with pytest.raises(ValueError, match="raw dataset.*timestamps duplicados"):
        to_timeframe(raw, cfg)


def test_signal_generation_rejects_non_minute_raw_data():
    cfg = Config(timeframe="30min")

    with pytest.raises(ValueError, match="raw dataset.*resolución de 1m"):
        EMACrossStrategy(cfg).generate_signals(
            make_bars(10, "30min"),
            raw=make_bars(10, "5min"),
        )


def test_backtester_rejects_non_minute_raw_even_with_custom_signals():
    bars = make_bars(2, "30min")
    signals = pd.DataFrame(
        {
            "valid": np.ones(len(bars), dtype=bool),
            "target": np.zeros(len(bars), dtype=int),
            "trend_bias": np.zeros(len(bars), dtype=int),
        },
        index=bars.index,
    )

    with pytest.raises(ValueError, match="raw dataset.*resolución de 1m"):
        Backtester(
            bars,
            Config(timeframe="30min"),
            signals=signals,
            raw=make_bars(10, "5min"),
        )


def test_live_bot_rejects_non_minute_feed_interval():
    with pytest.raises(ValueError, match="feed raw.*intervalo de 1m"):
        SignalBot(
            Config(),
            feed=None,
            store=None,
            symbol="BTCUSDT",
            interval="5m",
        )
