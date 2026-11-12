import pandas as pd

from strategy.config import Config
from strategy.models import Position
from strategy.strategy import Backtester


def make_engine(*, touches_ema_before_tp3=False):
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
                "high": [100.0, 101.0, 82.0],
                "low": [100.0, 74.0, 75.0],
                "close": [100.0, 75.0, 80.0],
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
        entry_adx_enabled=False,
        entry_atr_enabled=False,
        entry_rsi_divergence_enabled=False,
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
        entry_bar_index=0,
    )
    return engine


def test_short_exits_remaining_position_at_slow_ema_after_tp3():
    engine = make_engine()

    trades, _ = engine.run()

    assert trades.reason.tolist() == [
        "PARTIAL_TP3_2.5R",
        "EMA_SLOW_TOUCH_SHORT_AFTER_TP3",
    ]
    assert trades.exit_time.iloc[1] == engine.bars.index[2]
    assert trades.exit_price.iloc[1] == engine.ema_slow_line[2]
    assert trades.quantity.iloc[1] == 0.9
    assert engine.position is None


def test_slow_ema_touch_before_tp3_does_not_exit_short():
    engine = make_engine(touches_ema_before_tp3=True)

    trades, _ = engine.run()

    assert "EMA_SLOW_TOUCH_SHORT_AFTER_TP3" not in trades.reason.tolist()
    assert engine.position is not None
    assert not engine.position.partial3_done
