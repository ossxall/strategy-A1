import pandas as pd
import pytest

from strategy.config import Config
from strategy.models import Position
from strategy.strategy import Backtester


@pytest.mark.parametrize("level", range(11, 21))
@pytest.mark.parametrize("side", [1, -1])
def test_tp11_to_tp20_execute_for_long_and_short(level, side):
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
    target_r = float(level - 2)
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

    assert len(engine.trades) == 1
    trade = engine.trades[0]
    assert trade.reason == f"PARTIAL_TP{level}_{target_r:g}R"
    assert trade.exit_price == pytest.approx(target_price)
    assert trade.quantity == pytest.approx(0.05)
    assert engine.position is not None
    assert getattr(engine.position, f"partial{level}_done")


@pytest.mark.parametrize("level", range(11, 21))
def test_tp11_to_tp20_defaults_and_validation(level):
    config = Config()
    assert getattr(config, f"partial_tp{level}_r") == float(level - 2)
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
        partial_tp11_r=8.0,
    )
    assert config.partial_tp11_r == 8.0
