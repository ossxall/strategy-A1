import numpy as np
import pandas as pd
import pytest

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
        "entry_gaussian_trend_enabled": False,
        "adx_exit_enabled": False,
        "ema_weakening_exit_enabled": False,
        "exit_rsi_divergence_enabled": False,
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
    )

    trades, equity = engine.run()

    assert trades.pnl_net.tolist() == [-10.0]
    assert engine.streak_in_loss
    assert engine.position is None
    assert equity.position_side.tolist() == ["LONG", "FLAT"]
    assert "filtro de tendencia" in equity.execution_rejection.iloc[1]
    assert equity.execution_rejection_time.iloc[1] == bars.index[0]


def test_streak_filter_does_not_block_closing_open_position_on_reversal():
    bars = _bars(2, "30min")
    bars.iloc[1, bars.columns.get_loc("open")] = 90.0
    bars.iloc[1, bars.columns.get_loc("high")] = 90.1
    bars.iloc[1, bars.columns.get_loc("low")] = 89.9
    bars.iloc[1, bars.columns.get_loc("close")] = 90.0
    signals = pd.DataFrame(
        {
            "valid": [True, False],
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
    )
    engine.streak_in_loss = True

    trades, equity = engine.run()

    assert trades.pnl_net.tolist() == [-10.0]
    assert engine.position is None
    assert engine.streak_in_loss
    assert "filtro de tendencia" in equity.execution_rejection.iloc[1]
    assert equity.execution_rejection_time.iloc[1] == bars.index[0]


@pytest.mark.parametrize(
    "position_side,signal_target,disabled_side",
    [(-1, 1, "allow_long"), (1, -1, "allow_short")],
)
def test_disallowed_reversal_does_not_close_existing_position(
    position_side, signal_target, disabled_side
):
    bars = _bars(2, "30min")
    signals = pd.DataFrame(
        {
            "valid": [True, False],
            "target": [signal_target, 0],
            "trend_bias": [0, 0],
        },
        index=bars.index,
    )
    engine = Backtester(
        bars,
        _quiet_config(**{disabled_side: False}),
        signals=signals,
    )
    engine.position = Position(
        position_id=1,
        side=position_side,
        quantity=1.0,
        initial_quantity=1.0,
        entry_price=100.0,
        entry_time=bars.index[0],
        entry_fee=0.0,
        stop_pct=0.0,
        risk_amount=0.0,
    )

    trades, _ = engine.run()

    assert trades.empty
    assert engine.position is not None
    assert engine.position.side == position_side
    assert engine.opened == []


def test_entry_filter_rejection_does_not_close_opposite_position():
    bars = _bars(2, "30min")
    signals = pd.DataFrame(
        {
            "valid": [True, True],
            "target": [0, 0],  # The entry filter rejected the new LONG.
            "trend_bias": [0, 0],
            "cross_up_candidate": [True, False],
        },
        index=bars.index,
    )
    engine = Backtester(
        bars,
        _quiet_config(),
        signals=signals,
    )
    engine.position = Position(
        position_id=1,
        side=-1,
        quantity=1.0,
        initial_quantity=1.0,
        entry_price=100.0,
        entry_time=bars.index[0],
        entry_fee=0.0,
        stop_pct=0.0,
        risk_amount=0.0,
    )

    trades, _ = engine.run()

    assert trades.empty
    assert engine.position is not None
    assert engine.position.side == -1
    assert engine.opened == []


@pytest.mark.parametrize("stop_loss_pct", [1.0, 1.5])
def test_stop_loss_at_or_above_100_percent_is_rejected_when_longs_enabled(
    stop_loss_pct,
):
    with pytest.raises(ValueError, match="stop_loss_pct"):
        _quiet_config(stop_loss_pct=stop_loss_pct)


def test_stop_loss_at_or_above_100_percent_is_valid_for_short_only_config():
    config = _quiet_config(stop_loss_pct=1.0, allow_long=False)

    assert config.stop_loss_pct == 1.0


def test_higher_timeframe_rsi_exit_is_aligned_as_a_single_event(monkeypatch):
    bars = _bars(4 * 48, "30min")
    raw = bars.copy()
    config = _quiet_config(
        timeframe="30min",
        exit_rsi_divergence_enabled=True,
        exit_rsi_divergence_timeframe="1D",
        exit_rsi_divergence_pct=50.0,
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


def test_bearish_rsi_divergence_wins_when_timeframes_conflict(monkeypatch):
    bars = _bars(4, "30min")
    config = _quiet_config(
        timeframe="30min",
        exit_rsi_divergence_enabled=True,
        exit_rsi_divergence_timeframe=("1h", "2h"),
        rsi_divergence_pivot=1,
    )

    def fake_flags(raw, bars, cfg, timeframe):
        flags = np.zeros(len(bars), dtype=int)
        flags[1] = 1 if timeframe == "1h" else -1
        return flags

    monkeypatch.setattr("strategy.indicators._rsi_divergence_flags", fake_flags)

    exit_flags = build_rsi_divergence_exit(bars, bars, config)

    assert exit_flags is not None
    assert exit_flags.tolist() == [0, -1, 0, 0]
