import numpy as np
import pandas as pd
import pytest

import strategy.strategy as strategy_module
from strategy.config import Config
from strategy.indicators import spread_flags
from strategy.strategy import Backtester, EMACrossStrategy


def make_bars(size):
    index = pd.date_range("2026-01-01", periods=size, freq="30min", tz="UTC")
    close = np.full(size, 100.0)
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


def test_spread_flags_keeps_divergence_active_for_exact_lookback():
    blocked = spread_flags(np.array([False, True, False, False, False]), window=3)

    assert blocked.tolist() == [False, True, True, True, False]


def test_entries_are_filtered_by_recent_divergence_in_the_same_direction(monkeypatch):
    bars = make_bars(4)
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
    monkeypatch.setattr(
        strategy_module,
        "build_rsi_divergence_flags",
        lambda raw, source_bars, cfg: np.array([1, 0, -1, 0]),
    )
    cfg = Config(
        timeframe="30min",
        macd_timeframe=None,
        confirm_bars=0,
        entry_adx_enabled=False,
        entry_atr_enabled=False,
        entry_mfi_risk_scaling_enabled=False,
        entry_er_risk_scaling_enabled=False,
        entry_rsi_divergence_lookback=2,
        entry_price_ema_filter_enabled=False,
        streak_trend_only=False,
    )

    signals = EMACrossStrategy(cfg).generate_signals(bars)

    assert signals.entry_rsi_divergence_blocked.tolist() == [True, True, False, False]
    assert signals.entry_rsi_bear_divergence_blocked.tolist() == [False, False, True, True]
    assert signals.target.tolist() == [0, 0, 0, 0]


def test_backtester_applies_recent_divergence_to_custom_long_signals_only():
    bars = make_bars(7)
    signals = pd.DataFrame(
        {
            "valid": True,
            "target": [0, 0, 1, 0, -1, 1, 0],
            "trend_bias": 0,
            "entry_rsi_divergence_blocked": [False, True, True, False, False, False, False],
            "entry_rsi_bear_divergence_blocked": [False, False, False, True, True, False, False],
        },
        index=bars.index,
    )
    cfg = Config(
        timeframe="30min",
        entry_adx_enabled=False,
        entry_atr_enabled=False,
        entry_mfi_risk_scaling_enabled=False,
        entry_er_risk_scaling_enabled=False,
        streak_trend_only=False,
        monthly_loss_limit_pct=0,
        force_close_at_end=True,
    )

    trades, _ = Backtester(bars, cfg, signals=signals).run()

    assert len(trades) == 1
    assert trades.side.tolist() == ["LONG"]


def test_rsi_entry_lookback_must_be_positive():
    with pytest.raises(ValueError, match="entry_rsi_divergence_lookback"):
        Config(entry_rsi_divergence_lookback=0)
