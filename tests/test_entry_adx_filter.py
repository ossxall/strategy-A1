import numpy as np
import pandas as pd

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


def configure_crosses(monkeypatch, adx_values):
    bars = make_bars()
    fast = np.array([1.0, 3.0, 3.0, 1.0])
    slow = np.array([2.0, 2.0, 2.0, 2.0])
    monkeypatch.setattr(
        strategy_module,
        "ema",
        lambda values, period: pd.Series({55: fast, 200: slow}[period], index=values.index),
    )
    monkeypatch.setattr(
        strategy_module.talib,
        "MACD",
        lambda *args, **kwargs: (
            np.array([0.0, 1.0, 1.0, -1.0]),
            np.zeros(4),
            np.zeros(4),
        ),
    )
    monkeypatch.setattr(
        strategy_module,
        "adx",
        lambda *args, **kwargs: pd.DataFrame(
            {
                "adx": adx_values,
                "plus_di": np.zeros(4),
                "minus_di": np.zeros(4),
            },
            index=bars.index,
        ),
    )
    return bars


def make_config(adx_min=20.0, adx_timeframe="30min"):
    return Config(
        timeframe="30min",
        macd_timeframe=None,
        confirm_bars=0,
        entry_adx_enabled=True,
        entry_adx_len=14,
        entry_adx_min=adx_min,
        entry_adx_timeframe=adx_timeframe,
        entry_atr_enabled=False,
        entry_mfi_risk_scaling_enabled=False,
        entry_er_risk_scaling_enabled=False,
        streak_trend_only=False,
    )


def test_adx_filter_requires_a_value_strictly_above_20(monkeypatch):
    bars = configure_crosses(monkeypatch, np.array([40.0, 20.01, 20.0, 20.0]))

    signals = EMACrossStrategy(make_config()).generate_signals(bars)

    # ADX 20.01 permite el cruce alcista; ADX exactamente 20 bloquea el bajista.
    assert signals.target.tolist() == [0, 1, 0, 0]


def test_adx_filter_allows_long_and_short_crosses_above_the_configured_threshold(monkeypatch):
    bars = configure_crosses(monkeypatch, np.array([40.0, 25.01, 40.0, 25.01]))

    signals = EMACrossStrategy(make_config(adx_min=25.0)).generate_signals(bars)

    assert signals.target.tolist() == [0, 1, 0, -1]
    assert signals.adx_entry.tolist() == [40.0, 25.01, 40.0, 25.01]


def test_entry_adx_defaults_to_enabled_period_14_and_threshold_20():
    cfg = Config()

    assert cfg.timeframe == "30min"
    assert cfg.entry_adx_enabled
    assert cfg.entry_adx_len == 14
    assert cfg.entry_adx_min == 20.0
    assert cfg.entry_adx_timeframes == ("30min",)


def test_multiple_entry_adx_timeframes_all_must_be_above_the_threshold(monkeypatch):
    bars = configure_crosses(monkeypatch, np.array([40.0, 25.01, 40.0, 25.01]))
    hourly = bars.iloc[[0, 2]].copy()
    monkeypatch.setattr(
        strategy_module,
        "resample_ohlcv",
        lambda data, timeframe: hourly,
    )

    def adx_by_timeframe(data, *args):
        values = (
            np.array([40.0, 25.01, 40.0, 25.01])
            if data is bars
            else np.array([10.0, 40.0])
        )
        return pd.DataFrame(
            {"adx": values, "plus_di": np.zeros(len(data)), "minus_di": np.zeros(len(data))},
            index=data.index,
        )

    monkeypatch.setattr(strategy_module, "adx", adx_by_timeframe)

    signals = EMACrossStrategy(
        make_config(adx_timeframe=("30min", "1h"))
    ).generate_signals(bars)

    # La vela 1h de las 00:00 cierra a las 01:00: bloquea el cruce de 00:30.
    # La vela 1h siguiente solo queda disponible al cierre de la barra 01:30.
    assert np.isnan(signals["adx_entry_1h"].iloc[0])
    assert signals["adx_entry_1h"].iloc[1:].tolist() == [10.0, 10.0, 40.0]
    assert signals.target.tolist() == [0, 0, 0, -1]


def test_entry_adx_timeframes_accept_comma_separated_cli_values(monkeypatch):
    import sys

    from strategy.strategy import config_from_args, parse_args

    monkeypatch.setattr(
        sys,
        "argv",
        ["strategy", "dataset.parquet", "--entry-adx-timeframe", "30min,1h"],
    )

    cfg = config_from_args(parse_args())

    assert cfg.entry_adx_timeframes == ("30min", "1h")
