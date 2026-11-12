import numpy as np
import pandas as pd

from strategy.config import Config
from strategy.models import Position
from strategy.strategy import Backtester


def test_rsi_divergence_exit_closes_configured_percent():
    index = pd.date_range("2026-01-01", periods=2, freq="30min", tz="UTC")
    prices = np.array([100.0, 101.0])
    bars = pd.DataFrame(
        {
            "open": prices,
            "high": prices + 0.5,
            "low": prices - 0.5,
            "close": prices,
            "volume": 1.0,
        },
        index=index,
    )
    config = Config(
        rsi_divergence_exit_pct=50.0,
        partial_tp_enabled=False,
        partial_tp2_r=0,
        partial_tp2_pct=0,
        stop_loss_pct=0,
        monthly_loss_limit_pct=0,
        streak_trend_only=False,
        force_close_at_end=False,
    )
    signals = pd.DataFrame(
        {"valid": True, "target": 0, "trend_bias": 0}, index=index
    )
    engine = Backtester(
        bars,
        config,
        signals=signals,
        rsi_div_exit=np.array([-1, 0]),
    )
    engine.position = Position(
        position_id=1,
        side=1,
        quantity=1.0,
        initial_quantity=1.0,
        entry_price=100.0,
        entry_time=index[0],
        entry_fee=0.0,
        stop_pct=0.0,
        risk_amount=0.0,
        entry_bar_index=0,
    )

    trades, _ = engine.run()

    assert trades.reason.tolist() == ["RSI_DIVERGENCE_BEAR_50%"]
    assert trades.quantity.iloc[0] == 0.5
    assert engine.position is not None
    assert engine.position.quantity == 0.5


def test_rsi_divergence_exit_percent_must_be_between_zero_and_100():
    for pct in (0, 100.1):
        try:
            Config(rsi_divergence_exit_pct=pct)
        except ValueError as exc:
            assert "rsi_divergence_exit_pct" in str(exc)
        else:
            raise AssertionError(f"Expected invalid exit percent {pct} to fail")
