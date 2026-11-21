import pandas as pd
import pytest

from strategy.config import Config
from strategy.models import Position
from strategy.strategy import Backtester


def make_engine(*, touches_ema_before_tp3=False, slow_touch_high=85.0,
                slow_touch_close=84.0, penetration_pct=0.1,
                slow_ema_exit_enabled=True):
    index = pd.date_range("2026-01-01", periods=3, freq="30min", tz="UTC")
    if touches_ema_before_tp3:
        bars = pd.DataFrame(
            {
                "open": [100.0, 85.0, 84.0],
                "high": [100.0, 91.0, 85.0],
                "low": [100.0, 84.0, 83.0],
                "close": [100.0, 85.0, 84.0],
                "volume": 1.0,
            },
            index=index,
        )
    else:
        bars = pd.DataFrame(
            {
                "open": [100.0, 100.0, 80.0],
                "high": [100.0, 101.0, slow_touch_high],
                "low": [100.0, 74.0, 75.0],
                "close": [100.0, 75.0, slow_touch_close],
                "volume": 1.0,
            },
            index=index,
        )
    signals = pd.DataFrame(
        {"valid": True, "target": 0, "trend_bias": 0}, index=index
    )
    config = Config(
        timeframe="30min",
        ema_fast=1,
        ema_slow=2,
        stop_loss_pct=0.1,
        partial_tp_enabled=True,
        partial_tp_r=1.0,
        partial_tp_pct=10.0,
        partial_tp2_r=1.75,
        partial_tp2_pct=10.0,
        partial_tp3_r=2.5,
        partial_tp3_pct=10.0,
        partial_tp4_r=0.0,
        partial_tp4_pct=0.0,
        tp3_ema_slow_exit_enabled=slow_ema_exit_enabled,
        tp3_ema_slow_penetration_pct=penetration_pct,
        entry_adx_enabled=False,
        entry_atr_enabled=False,
        entry_rsi_divergence_enabled=False,
        adx_exit_enabled=False,
        ema_weakening_exit_enabled=False,
        exit_rsi_divergence_enabled=False,
        streak_trend_only=False,
        monthly_loss_limit_pct=0,
        force_close_at_end=False,
    )
    engine = Backtester(bars, config, signals=signals)
    engine.position = Position(
        position_id=1,
        side=-1,
        quantity=1.0,
        initial_quantity=1.0,
        entry_price=100.0,
        entry_time=index[0],
        entry_fee=0.0,
        stop_pct=0.1,
        risk_amount=10.0,
    )
    return engine


def test_short_exits_remaining_position_at_slow_ema_after_tp3():
    engine = make_engine()

    trades, _ = engine.run()

    assert trades.reason.tolist() == [
        "PARTIAL_TP3_2.5R",
        "EMA_SLOW_TOUCH_SHORT_AFTER_TP3",
    ]
    assert trades.exit_time.iloc[1] == (
        engine.bars.index[2] + pd.Timedelta("30min")
    )
    assert trades.exit_price.iloc[1] == engine.bars.close.iloc[2]
    assert trades.quantity.iloc[1] == 0.9
    assert engine.position is None


def test_intrabar_wick_without_close_above_trigger_does_not_exit():
    engine = make_engine(slow_touch_high=82.0, slow_touch_close=80.0)

    trades, _ = engine.run()

    trigger = engine.ema_slow_line[2] * 1.001
    assert engine.bars.high.iloc[2] > trigger
    assert engine.bars.close.iloc[2] < trigger
    assert "EMA_SLOW_TOUCH_SHORT_AFTER_TP3" not in trades.reason.tolist()
    assert engine.position is not None


def test_close_above_ema_but_below_configured_penetration_does_not_exit():
    engine = make_engine(slow_touch_high=85.0, slow_touch_close=83.5)

    trades, _ = engine.run()

    assert engine.bars.close.iloc[2] > engine.ema_slow_line[2]
    assert engine.bars.close.iloc[2] < engine.ema_slow_line[2] * 1.001
    assert "EMA_SLOW_TOUCH_SHORT_AFTER_TP3" not in trades.reason.tolist()
    assert engine.position is not None


def test_custom_penetration_percentage_is_respected():
    engine = make_engine(
        slow_touch_high=85.0, slow_touch_close=83.67, penetration_pct=0.5
    )

    trades, _ = engine.run()

    assert engine.bars.high.iloc[2] > engine.ema_slow_line[2] * 1.001
    assert engine.bars.close.iloc[2] > engine.ema_slow_line[2] * 1.001
    assert engine.bars.close.iloc[2] < engine.ema_slow_line[2] * 1.005
    assert "EMA_SLOW_TOUCH_SHORT_AFTER_TP3" not in trades.reason.tolist()
    assert engine.position is not None


def test_slow_ema_exit_can_be_disabled():
    engine = make_engine(slow_ema_exit_enabled=False)

    trades, _ = engine.run()

    assert "EMA_SLOW_TOUCH_SHORT_AFTER_TP3" not in trades.reason.tolist()
    assert engine.position is not None


def test_slow_ema_touch_before_tp3_does_not_exit_short():
    engine = make_engine(touches_ema_before_tp3=True)

    trades, _ = engine.run()

    assert "EMA_SLOW_TOUCH_SHORT_AFTER_TP3" not in trades.reason.tolist()
    assert engine.position is not None
    assert not engine.position.partial3_done


def test_ema_penetration_parameter_must_be_nonnegative():
    with pytest.raises(ValueError, match="cannot be negative"):
        Config(tp3_ema_slow_penetration_pct=-0.01)
