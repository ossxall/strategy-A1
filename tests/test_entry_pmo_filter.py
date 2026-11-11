import numpy as np
import pandas as pd
import pytest

import strategy.strategy as strategy_module
from strategy.config import Config
from strategy.indicators import pure_momentum_oscillator
from strategy.strategy import Backtester, EMACrossStrategy


def make_bars(close):
    close = np.asarray(close, dtype=float)
    return pd.DataFrame(
        {
            "open": close - 0.25,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1.0,
        },
        index=pd.date_range("2026-01-01", periods=len(close), freq="30min", tz="UTC"),
    )


def test_pmo_matches_bullish_and_bearish_momentum_directions():
    rising = make_bars(100.0 + 2.0 * np.arange(80))
    falling = make_bars(260.0 - 2.0 * np.arange(80))

    bullish = pure_momentum_oscillator(rising, length=5)
    bearish = pure_momentum_oscillator(falling, length=5)

    assert bullish.momentum.iloc[:5].isna().all()
    assert bullish.bullish.iloc[5:].all()
    assert not bullish.bearish.iloc[5:].any()
    assert bullish.trend.iloc[-1] == 1
    assert bearish.bearish.iloc[5:].all()
    assert not bearish.bullish.iloc[5:].any()
    assert bearish.trend.iloc[-1] == -1


def test_pmo_momentum_is_an_additional_directional_entry_condition(monkeypatch):
    bars = make_bars([100, 101, 102, 103])
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
    pmo = pd.DataFrame(
        {
            "momentum": [0.0, 0.1, 0.0, -0.1],
            "bullish": [False, True, False, False],
            "bearish": [False, False, False, True],
            "trend": [0, 1, 1, -1],
        },
        index=bars.index,
    )
    monkeypatch.setattr(strategy_module, "pure_momentum_oscillator", lambda *args: pmo)
    empty = np.zeros(4, dtype=bool)
    monkeypatch.setattr(
        EMACrossStrategy,
        "_candle_confirmations",
        lambda self, bars, raw=None: (empty, empty, empty),
    )
    cfg = Config(
        timeframe="30min",
        macd_timeframe=None,
        confirm_bars=0,
        entry_adx_enabled=False,
        entry_pmo_enabled=True,
        entry_atr_enabled=False,
        entry_rsi_divergence_enabled=False,
        entry_dmi_risk_scaling_enabled=False,
        entry_mfi_risk_scaling_enabled=False,
        entry_er_risk_scaling_enabled=False,
        streak_trend_only=False,
    )

    signals = EMACrossStrategy(cfg).generate_signals(bars)

    assert signals.target.tolist() == [0, 1, 0, -1]
    assert signals.entry_pmo.tolist() == [0.0, 0.1, 0.0, -0.1]
    assert signals.entry_pmo_trend.tolist() == [0, 1, 1, -1]


def test_pmo_parameters_default_to_the_pine_values():
    cfg = Config()

    assert cfg.entry_pmo_enabled
    assert cfg.entry_pmo_len == 45
    assert cfg.entry_pmo_long_threshold == 0.05
    assert cfg.entry_pmo_short_threshold == -0.05


def test_pmo_parameters_are_configurable_from_the_cli(monkeypatch):
    import sys

    from strategy.strategy import config_from_args, parse_args

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "strategy",
            "dataset.parquet",
            "--entry-pmo-len",
            "34",
            "--entry-pmo-long-threshold",
            "0.08",
            "--entry-pmo-short-threshold",
            "-0.07",
        ],
    )

    cfg = config_from_args(parse_args())

    assert cfg.entry_pmo_len == 34
    assert cfg.entry_pmo_long_threshold == 0.08
    assert cfg.entry_pmo_short_threshold == -0.07


def test_pmo_length_and_threshold_order_are_validated():
    with pytest.raises(ValueError, match="PMO"):
        Config(entry_pmo_len=1)
    with pytest.raises(ValueError, match="PMO"):
        Config(entry_pmo_long_threshold=-0.1, entry_pmo_short_threshold=0.1)


def test_backtester_applies_pmo_to_custom_signals_too():
    bars = make_bars(100.0 + 2.0 * np.arange(8))
    signals = pd.DataFrame(
        {
            "valid": True,
            "target": [0, 1, 0, 0, 1, 0, 0, 0],
            "trend_bias": 0,
        },
        index=bars.index,
    )
    cfg = Config(
        timeframe="30min",
        entry_adx_enabled=False,
        entry_atr_enabled=False,
        entry_rsi_divergence_enabled=False,
        entry_pmo_enabled=True,
        entry_pmo_len=3,
        entry_dmi_risk_scaling_enabled=False,
        entry_mfi_risk_scaling_enabled=False,
        entry_er_risk_scaling_enabled=False,
        entry_partial_enabled=False,
        stop_loss_pct=0.0,
        partial_tp_enabled=False,
        partial_tp2_r=0.0,
        partial_tp2_pct=0.0,
        monthly_loss_limit_pct=0.0,
        streak_trend_only=False,
    )

    trades, _ = Backtester(bars, cfg, signals=signals).run()

    # La primera señal llega antes del warmup PMO; la segunda sí tiene momentum alcista.
    assert trades.side.tolist() == ["LONG"]
    assert trades.entry_time.iloc[0] == bars.index[5]
