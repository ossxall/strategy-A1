import numpy as np
import pandas as pd

from strategy.config import Config
from strategy.indicators import build_rsi_divergence_exit
from strategy.models import Position
from strategy.strategy import Backtester


def _bars(periods, freq):
    index = pd.date_range("2026-01-01", periods=periods, freq=freq, tz="UTC")
    close = np.full(periods, 100.0)
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


def _quiet_config(**overrides):
    values = {
        "entry_adx_enabled": False,
        "entry_atr_enabled": False,
        "entry_rsi_divergence_enabled": False,
        "entry_mfi_risk_scaling_enabled": False,
        "entry_er_risk_scaling_enabled": False,
        "adx_exit_enabled": False,
        "ema_weakening_exit_enabled": False,
        "rsi_divergence_exit_enabled": False,
        "ema55_touch_exit_enabled": False,
        "partial_tp_enabled": False,
        "partial_tp2_r": 0,
        "partial_tp2_pct": 0,
        "stop_loss_pct": 0,
        "monthly_loss_limit_pct": 0,
        "force_close_at_end": False,
    }
    values.update(overrides)
    return Config(**values)


def test_losing_reversal_obeys_streak_trend_filter():
    bars = _bars(2, "30min")
    bars.iloc[1, bars.columns.get_loc("open")] = 90.0
    bars.iloc[1, bars.columns.get_loc("high")] = 90.1
    bars.iloc[1, bars.columns.get_loc("low")] = 89.9
    bars.iloc[1, bars.columns.get_loc("close")] = 90.0
    signals = pd.DataFrame(
        {
            "valid": [True, True],
            "target": [-1, 0],
            "trend_bias": [1, 1],
        },
        index=bars.index,
    )
    engine = Backtester(
        bars,
        _quiet_config(streak_trend_only=True),
        signals=signals,
    )
    engine.position = Position(
        position_id=1,
        side=1,
        quantity=1.0,
        initial_quantity=1.0,
        entry_price=100.0,
        entry_time=bars.index[0],
        entry_fee=0.0,
        stop_pct=0.0,
        risk_amount=0.0,
        entry_bar_index=0,
    )

    trades, _ = engine.run()

    assert trades.pnl_net.tolist() == [-10.0]
    assert engine.streak_in_loss
    assert engine.position is None


def test_higher_timeframe_rsi_exit_is_aligned_as_a_single_event(monkeypatch):
    bars = _bars(4 * 48, "30min")
    raw = bars.copy()
    config = _quiet_config(
        timeframe="30min",
        rsi_divergence_exit_enabled=True,
        rsi_divergence_timeframe="1D",
        rsi_divergence_exit_pct=50.0,
        rsi_divergence_pivot=1,
    )

    def fake_divergence_flags(high, low, rsi_values, pivot, lookback):
        flags = np.zeros(len(high), dtype=int)
        flags[1] = -1
        return flags

    monkeypatch.setattr(
        "strategy.indicators.rsi_divergence_flags", fake_divergence_flags
    )

    exit_flags = build_rsi_divergence_exit(raw, bars, config)

    assert exit_flags is not None
    assert np.flatnonzero(exit_flags).tolist() == [2 * 48 - 1]
