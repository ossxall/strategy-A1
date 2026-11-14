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
        entry_mfi_risk_scaling_enabled=False,
        entry_er_risk_scaling_enabled=False,
        adx_exit_enabled=False,
        ema_weakening_exit_enabled=False,
        rsi_divergence_exit_enabled=False,
        ema55_touch_exit_enabled=False,
        streak_trend_only=False,
        partial_tp4_r=0,
        partial_tp4_pct=0,
        partial_tp5_r=0,
        partial_tp5_pct=0,
        partial_tp6_r=0,
        partial_tp6_pct=0,
        partial_tp7_r=0,
        partial_tp7_pct=0,
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
def test_tp3_closes_configured_percent_at_2r_for_both_sides(side):
    engine = make_engine(side)

    engine._manage(1)

    trade = engine.trades[0]
    assert trade.reason == "PARTIAL_TP3_2R"
    assert trade.exit_price == (120.0 if side == 1 else 80.0)
    assert trade.quantity == pytest.approx(0.1)
    assert engine.position is not None
    assert engine.position.partial3_done
    assert not engine.position.partial2_done


def test_tp3_defaults_and_ordering_validation():
    config = Config()
    assert config.partial_tp3_r == 2.0
    assert config.partial_tp3_pct == 10.0
    assert Config(partial_tp2_r=0, partial_tp2_pct=0).partial_tp3_r == 2.0

    with pytest.raises(ValueError, match="partial take-profit 3"):
        Config(partial_tp3_r=0, partial_tp3_pct=10)
    with pytest.raises(ValueError, match="greater than TP2"):
        Config(partial_tp3_r=1.5)
