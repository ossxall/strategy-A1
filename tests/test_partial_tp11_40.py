import pandas as pd
import pytest

from strategy.config import Config
from strategy.models import Position
from strategy.strategy import Backtester


def assert_tps_through_level_execute_in_order(engine, level, side):
    pos = engine.position
    assert pos is not None
    risk_unit = engine._risk_unit(pos)
    remaining = pos.initial_quantity
    assert len(engine.trades) == level

    for tp_level, trade in enumerate(engine.trades, start=1):
        tp_r = (
            engine.cfg.partial_tp_r
            if tp_level == 1
            else getattr(engine.cfg, f"partial_tp{tp_level}_r")
        )
        reason = (
            f"PARTIAL_TP_{tp_r:g}R"
            if tp_level == 1
            else f"PARTIAL_TP{tp_level}_{tp_r:g}R"
        )
        tp_pct = (
            engine.cfg.partial_tp_pct
            if tp_level == 1
            else getattr(engine.cfg, f"partial_tp{tp_level}_pct")
        )
        assert trade.reason == reason
        assert trade.exit_price == pytest.approx(
            pos.entry_price + side * tp_r * risk_unit
        )
        assert trade.quantity == pytest.approx(remaining * tp_pct / 100)
        remaining -= trade.quantity
        done_attr = "partial_done" if tp_level == 1 else f"partial{tp_level}_done"
        assert getattr(pos, done_attr)

    assert pos.quantity == pytest.approx(remaining)


@pytest.mark.parametrize("level", range(11, 41))
@pytest.mark.parametrize("side", [1, -1])
def test_tp11_to_tp40_execute_in_order_for_long_and_short(level, side):
    config = Config(
        timeframe="30min",
        entry_adx_enabled=False,
        entry_atr_enabled=False,
        entry_rsi_divergence_enabled=False,
        adx_exit_enabled=False,
        exit_rsi_divergence_enabled=False,
        streak_trend_only=False,
    )
    index = pd.date_range("2026-01-01", periods=2, freq="30min", tz="UTC")
    target_r = float(level - 1)
    target_price = 1000.0 + side * target_r * 10.0
    bars = pd.DataFrame(
        {
            "open": [1000.0, 1000.0],
            "high": [1000.0, target_price + (1.0 if side == 1 else 0.0)],
            "low": [1000.0, target_price - (1.0 if side == -1 else 0.0)],
            "close": [1000.0, 1000.0],
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
        side=side,
        quantity=1.0,
        initial_quantity=1.0,
        entry_price=1000.0,
        entry_time=index[0],
        entry_fee=0.0,
        stop_pct=0.01,
        risk_amount=10.0,
    )

    engine._manage(1)

    assert_tps_through_level_execute_in_order(engine, level, side)
    assert engine.trades[-1].reason == f"PARTIAL_TP{level}_{target_r:g}R"
    assert engine.trades[-1].exit_price == pytest.approx(target_price)


@pytest.mark.parametrize("level", range(11, 41))
def test_tp11_to_tp40_defaults_and_validation(level):
    config = Config()
    assert getattr(config, f"partial_tp{level}_r") == float(level - 1)
    assert getattr(config, f"partial_tp{level}_pct") == 5.0
    disabled = Config(**{
        f"partial_tp{level}_r": 0.0,
        f"partial_tp{level}_pct": 0.0,
    })
    assert getattr(disabled, f"partial_tp{level}_r") == 0.0

    with pytest.raises(ValueError, match=f"partial take-profit {level}"):
        Config(**{f"partial_tp{level}_pct": 0.0})
    with pytest.raises(ValueError, match=f"greater than TP{level - 1}"):
        Config(**{f"partial_tp{level}_r": float(level - 3)})


def test_tp_validation_skips_disabled_levels_when_checking_order():
    with pytest.raises(ValueError, match="partial_tp11_r must be greater than TP9"):
        Config(
            partial_tp10_r=0.0,
            partial_tp10_pct=0.0,
            partial_tp11_r=6.0,
        )

    config = Config(
        partial_tp10_r=0.0,
        partial_tp10_pct=0.0,
        partial_tp11_r=9.0,
    )
    assert config.partial_tp11_r == 9.0
