import pandas as pd
import pytest

from strategy.config import Config
from strategy.models import Position
from strategy.strategy import Backtester


def make_engine(side):
    index = pd.date_range("2026-01-01", periods=2, freq="30min", tz="UTC")
    bars = pd.DataFrame(
        {
            "open": [100.0, 100.0],
            "high": [100.0, 126.0 if side == 1 else 101.0],
            "low": [100.0, 99.0 if side == 1 else 74.0],
            "close": [100.0, 100.0],
            "volume": [1.0, 1.0],
        },
        index=index,
    )
    signals = pd.DataFrame(
        {"valid": [True, True], "target": [0, 0], "trend_bias": [0, 0]},
        index=index,
    )
    config = Config(
        timeframe="30min",
        entry_adx_enabled=False,
        entry_atr_enabled=False,
        entry_rsi_divergence_enabled=False,
        adx_exit_enabled=False,
        exit_rsi_divergence_enabled=False,
        streak_trend_only=False,
        partial_tp4_r=0,
        partial_tp4_pct=0,
        partial_tp5_r=0,
        partial_tp5_pct=0,
        partial_tp6_r=0,
        partial_tp6_pct=0,
        partial_tp7_r=0,
        partial_tp7_pct=0,
        partial_tp8_r=0,
        partial_tp8_pct=0,
    )
    engine = Backtester(bars, config, signals=signals)
    engine.position = Position(
        position_id=1,
        side=side,
        quantity=1.0,
        initial_quantity=1.0,
        entry_price=100.0,
        entry_time=index[0],
        entry_fee=0.0,
        stop_pct=0.1,
        risk_amount=10.0,
    )
    return engine


@pytest.mark.parametrize("side", [1, -1])
def test_tps_through_tp3_execute_in_order_for_both_sides(side):
    engine = make_engine(side)

    engine._manage(1)

    assert [trade.reason for trade in engine.trades] == [
        "PARTIAL_TP_0.5R",
        "PARTIAL_TP2_1R",
        "PARTIAL_TP3_2R",
    ]
    assert [trade.exit_price for trade in engine.trades] == pytest.approx(
        [100.0 + side * price for price in (5.0, 10.0, 20.0)]
    )
    assert [trade.quantity for trade in engine.trades] == pytest.approx(
        [0.05, 0.0475, 0.045125]
    )
    assert engine.position is not None
    assert engine.position.partial3_done
    assert engine.position.partial2_done
    assert engine.position.partial_done
    assert engine.position.quantity == pytest.approx(0.857375)


def test_tp3_defaults_and_ordering_validation():
    config = Config()
    assert config.partial_tp3_r == 2.0
    assert config.partial_tp3_pct == 5.0
    assert Config(partial_tp2_r=0, partial_tp2_pct=0).partial_tp3_r == 2.0

    with pytest.raises(ValueError, match="partial take-profit 3"):
        Config(partial_tp3_r=0, partial_tp3_pct=5.0)
    with pytest.raises(ValueError, match="greater than TP2"):
        Config(partial_tp3_r=1.0)
