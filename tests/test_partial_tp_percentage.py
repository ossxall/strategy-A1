import pandas as pd
import pytest

from strategy.config import Config
from strategy.models import Position
from strategy.strategy import Backtester


def test_default_tp_r_and_pct_values_are_unchanged():
    config = Config()
    risk_multiples = [
        config.partial_tp_r,
        *(getattr(config, f"partial_tp{level}_r") for level in range(2, 31)),
    ]
    percentages = [
        config.partial_tp_pct,
        *(getattr(config, f"partial_tp{level}_pct") for level in range(2, 31)),
    ]

    assert risk_multiples == [0.5, *map(float, range(1, 30))]
    assert percentages == [5.0] * 30


def test_partial_tp_percent_is_applied_to_remaining_quantity():
    disabled_levels = {
        key: 0.0
        for level in range(3, 31)
        for key in (f"partial_tp{level}_r", f"partial_tp{level}_pct")
    }
    config = Config(
        timeframe="30min",
        partial_tp_r=0.5,
        partial_tp_pct=10.0,
        partial_tp2_r=1.0,
        partial_tp2_pct=20.0,
        entry_adx_enabled=False,
        entry_atr_enabled=False,
        entry_rsi_divergence_enabled=False,
        adx_exit_enabled=False,
        exit_rsi_divergence_enabled=False,
        streak_trend_only=False,
        **disabled_levels,
    )
    index = pd.date_range("2026-01-01", periods=2, freq="30min", tz="UTC")
    bars = pd.DataFrame(
        {
            "open": [100.0, 100.0],
            "high": [100.0, 111.0],
            "low": [100.0, 99.0],
            "close": [100.0, 105.0],
            "volume": [1.0, 1.0],
        },
        index=index,
    )
    signals = pd.DataFrame(
        {"valid": [True, True], "target": [0, 0], "trend_bias": [0, 0]},
        index=index,
    )
    engine = Backtester(bars, config, signals=signals)
    engine.position = Position(
        position_id=1,
        side=1,
        quantity=1.0,
        initial_quantity=1.0,
        entry_price=100.0,
        entry_time=index[0],
        entry_fee=0.0,
        stop_pct=0.1,
        risk_amount=10.0,
    )

    engine._manage(1)
    engine._manage(1, high=105.0)

    assert [trade.reason for trade in engine.trades] == [
        "PARTIAL_TP_0.5R",
        "PARTIAL_TP2_1R",
    ]
    assert [trade.quantity for trade in engine.trades] == pytest.approx([0.1, 0.18])
    assert engine.position is not None
    assert engine.position.quantity == pytest.approx(0.72)
