import numpy as np
import pandas as pd
import pytest

import strategy.strategy as strategy_module
from strategy.config import Config
from strategy.indicators import atr_volatility_filter
from strategy.strategy import Backtester, EMACrossStrategy


def make_bars(ranges):
    index = pd.date_range("2026-01-01", periods=len(ranges), freq="30min", tz="UTC")
    close = np.full(len(ranges), 100.0)
    half_range = np.asarray(ranges, dtype=float) / 2
    return pd.DataFrame(
        {
            "open": close,
            "high": close + half_range,
            "low": close - half_range,
            "close": close,
            "volume": 1.0,
        },
        index=index,
    )


def test_atr_filter_blocks_compression_and_an_extreme_spike_until_it_normalizes():
    bars = make_bars([10, 10, 10, 2, 10, 30, 10, 10])

    result = atr_volatility_filter(
        bars, length=1, lookback=2, min_ratio=0.5, max_ratio=2.5
    )

    assert result.atr_filter_ready.tolist() == [False, False, True, True, True, True, True, True]
    assert result.atr_filter_allowed.tolist() == [True, True, True, False, True, False, True, True]
    assert result.atr_ratio.iloc[3] < 0.5
    assert result.atr_ratio.iloc[5] > 2.5


def test_atr_filter_is_applied_to_both_long_and_short_entries(monkeypatch):
    bars = make_bars([1, 1, 1, 1])
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
        lambda *args, **kwargs: (np.array([0.0, 1.0, 1.0, -1.0]),
                                 np.zeros(4), np.zeros(4)),
    )
    allowed = [True, False, True, True]
    monkeypatch.setattr(
        strategy_module,
        "atr_volatility_filter",
        lambda *args, **kwargs: pd.DataFrame(
            {
                "atr": np.ones(4),
                "atr_mean": np.ones(4),
                "atr_ratio": np.ones(4),
                "atr_filter_ready": np.ones(4, dtype=bool),
                "atr_filter_allowed": allowed,
            },
            index=bars.index,
        ),
    )
    cfg = Config(
        timeframe="30min",
        macd_timeframe=None,
        confirm_bars=0,
        entry_adx_enabled=False,
        entry_mfi_risk_scaling_enabled=False,
        entry_er_risk_scaling_enabled=False,
        streak_trend_only=False,
    )

    signals = EMACrossStrategy(cfg).generate_signals(bars)

    assert signals.target.tolist() == [0, 0, 0, -1]
    assert signals.atr_filter_allowed.tolist() == [True, False, True, True]

    allowed[:] = [True, True, True, False]
    signals = EMACrossStrategy(cfg).generate_signals(bars)
    assert signals.target.tolist() == [0, 1, 0, 0]


def test_atr_filter_defaults_and_validation():
    cfg = Config()
    assert cfg.entry_atr_enabled
    assert cfg.entry_atr_len == 14
    assert cfg.entry_atr_lookback == 50
    assert cfg.entry_atr_min_ratio == 0.5
    assert cfg.entry_atr_max_ratio == 2.5

    with pytest.raises(ValueError, match="ATR"):
        Config(entry_atr_min_ratio=3.0, entry_atr_max_ratio=2.0)


def test_backtester_also_filters_custom_entry_signals():
    bars = make_bars([10, 10, 10, 2, 10, 10])
    signals = pd.DataFrame(
        {
            "valid": True,
            "target": [0, 0, 0, 1, 0, 0],
            "trend_bias": 0,
        },
        index=bars.index,
    )
    cfg = Config(
        timeframe="30min",
        entry_atr_len=1,
        entry_atr_lookback=2,
        entry_adx_enabled=False,
        entry_mfi_risk_scaling_enabled=False,
        entry_er_risk_scaling_enabled=False,
        adx_exit_enabled=False,
        ema_weakening_exit_enabled=False,
        rsi_divergence_exit_enabled=False,
        ema55_touch_exit_enabled=False,
        streak_trend_only=False,
        monthly_loss_limit_pct=0,
        force_close_at_end=False,
    )

    trades, _ = Backtester(bars, cfg, signals=signals).run()

    assert trades.empty
