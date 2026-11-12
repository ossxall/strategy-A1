import numpy as np
import pandas as pd
import pytest

import strategy.strategy as strategy_module
from strategy.config import Config
from strategy.strategy import EMACrossStrategy


def make_bars(periods=4):
    index = pd.date_range("2026-01-01", periods=periods, freq="30min", tz="UTC")
    close = np.full(periods, 100.0)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1.0,
        },
        index=index,
    )


def configure_crosses(monkeypatch, macd_line, signal_line, histogram):
    fast = np.array([1.0, 3.0, 3.0, 1.0])
    slow = np.array([2.0, 2.0, 2.0, 2.0])
    ema_values = {55: fast, 200: slow}
    monkeypatch.setattr(
        strategy_module,
        "ema",
        lambda values, period: pd.Series(ema_values[period], index=values.index),
    )
    monkeypatch.setattr(
        strategy_module.talib,
        "MACD",
        lambda *args, **kwargs: (macd_line, signal_line, histogram),
    )
def make_config():
    return Config(
        timeframe="30min",
        macd_timeframe=None,
        confirm_bars=0,
        entry_adx_enabled=False,
        entry_mfi_risk_scaling_enabled=False,
        entry_er_risk_scaling_enabled=False,
        entry_price_ema_filter_enabled=False,
        streak_trend_only=False,
    )


def test_entries_use_macd_main_line_zero_side(monkeypatch):
    # EMA 55 crosses up at index 1 and down at index 3. Signal/histogram have
    # the opposite sign to prove the filter reads the main MACD line only.
    configure_crosses(
        monkeypatch,
        macd_line=np.array([0.0, 1.0, 1.0, -1.0]),
        signal_line=np.array([0.0, -1.0, -1.0, 1.0]),
        histogram=np.array([0.0, -2.0, -2.0, 2.0]),
    )

    signals = EMACrossStrategy(make_config()).generate_signals(make_bars())

    assert signals.target.tolist() == [0, 1, 0, -1]
    assert signals.macd_line.tolist() == [0.0, 1.0, 1.0, -1.0]


def test_crosses_are_rejected_when_macd_main_line_is_on_wrong_side_or_zero(monkeypatch):
    configure_crosses(
        monkeypatch,
        macd_line=np.array([0.0, -1.0, 1.0, 0.0]),
        signal_line=np.array([0.0, 1.0, 1.0, -1.0]),
        histogram=np.array([0.0, 2.0, 0.0, -1.0]),
    )

    signals = EMACrossStrategy(make_config()).generate_signals(make_bars())

    assert signals.target.tolist() == [0, 0, 0, 0]
    assert not signals.cross_up.any()
    assert not signals.cross_down.any()


def test_macd_periods_are_configurable_and_passed_to_talib(monkeypatch):
    configure_crosses(
        monkeypatch,
        macd_line=np.ones(4),
        signal_line=np.zeros(4),
        histogram=np.zeros(4),
    )
    received = {}

    def macd(*args, **kwargs):
        received.update(kwargs)
        return np.ones(4), np.zeros(4), np.zeros(4)

    monkeypatch.setattr(strategy_module.talib, "MACD", macd)

    cfg = Config(
        **{
            **make_config().__dict__,
            "macd_fast": 8,
            "macd_slow": 21,
            "macd_signal": 5,
        }
    )
    EMACrossStrategy(cfg).generate_signals(make_bars())

    assert received == {"fastperiod": 8, "slowperiod": 21, "signalperiod": 5}


def test_macd_can_use_higher_timeframe_and_only_closed_values(monkeypatch):
    configure_crosses(
        monkeypatch,
        macd_line=np.ones(4),
        signal_line=np.zeros(4),
        histogram=np.zeros(4),
    )
    received = {}

    def macd(close, **kwargs):
        received["bars"] = len(close)
        return np.array([1.0, -1.0]), np.zeros(2), np.zeros(2)

    monkeypatch.setattr(strategy_module.talib, "MACD", macd)
    cfg = Config(**{
        **make_config().__dict__,
        "macd_timeframe": "1h",
    })

    signals = EMACrossStrategy(cfg).generate_signals(make_bars())

    # Cuatro velas de 30m producen dos velas horarias. El primer cruce se
    # confirma al cierre horario y el segundo usa la siguiente vela cerrada.
    assert received["bars"] == 2
    assert signals.target.tolist() == [0, 1, 0, -1]


def test_macd_can_use_lower_timeframe_when_raw_bars_are_provided(monkeypatch):
    configure_crosses(
        monkeypatch,
        macd_line=np.ones(4),
        signal_line=np.zeros(4),
        histogram=np.zeros(4),
    )
    received = {}

    def macd(close, **kwargs):
        received["bars"] = len(close)
        line = np.array([1.0] * 7 + [-1.0])
        return line, np.zeros(8), np.zeros(8)

    monkeypatch.setattr(strategy_module.talib, "MACD", macd)
    cfg = Config(**{
        **make_config().__dict__,
        "macd_timeframe": "15min",
    })
    raw_index = pd.date_range("2026-01-01", periods=24, freq="5min", tz="UTC")
    raw = make_bars(24)
    raw.index = raw_index

    signals = EMACrossStrategy(cfg).generate_signals(make_bars(), raw=raw)

    assert received["bars"] == 8
    assert signals.target.tolist() == [0, 1, 0, -1]


def test_lower_macd_timeframe_requires_sufficient_raw_data():
    cfg = Config(**{
        **make_config().__dict__,
        "macd_timeframe": "15min",
    })

    with pytest.raises(ValueError, match="raw"):
        EMACrossStrategy(cfg).generate_signals(make_bars())


@pytest.mark.parametrize(
    "periods",
    [
        {"macd_fast": 0},
        {"macd_slow": 0},
        {"macd_signal": 0},
        {"macd_timeframe": "0min"},
        {"macd_fast": 26, "macd_slow": 26},
        {"macd_fast": 30, "macd_slow": 26},
    ],
)
def test_macd_periods_must_be_positive_and_fast_less_than_slow(periods):
    with pytest.raises(ValueError, match="macd_"):
        Config(**periods)
