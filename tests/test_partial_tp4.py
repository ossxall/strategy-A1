import pandas as pd
import pytest

from strategy.config import Config
from strategy.models import Position
from strategy.strategy import Backtester


def make_engine(
    side,
    *,
    reach_tp5=False,
    reach_tp6=False,
    reach_tp7=False,
    reach_tp8=False,
    reach_tp9=False,
    reach_tp10=False,
):
    high = (
        192.0 if reach_tp10
        else 182.0 if reach_tp9
        else 172.0 if reach_tp8
        else 162.0 if reach_tp7
        else 152.0 if reach_tp6
        else 142.0 if reach_tp5
        else 131.0
    )
    low = (
        8.0 if reach_tp10
        else 18.0 if reach_tp9
        else 28.0 if reach_tp8
        else 38.0 if reach_tp7
        else 48.0 if reach_tp6
        else 58.0 if reach_tp5
        else 69.0
    )
    index = pd.date_range("2026-01-01", periods=2, freq="30min", tz="UTC")
    bars = pd.DataFrame(
        {
            "open": [100.0, 100.0],
            "high": [100.0, high],
            "low": [100.0, low],
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


def assert_tps_executed_in_order(engine, level, side):
    pos = engine.position
    assert pos is not None
    risk_unit = engine._risk_unit(pos)
    remaining = pos.initial_quantity
    assert len(engine.trades) == level

    for index, trade in enumerate(engine.trades, start=1):
        tp_r = (
            engine.cfg.partial_tp_r
            if index == 1
            else getattr(engine.cfg, f"partial_tp{index}_r")
        )
        tp_pct = (
            engine.cfg.partial_tp_pct
            if index == 1
            else getattr(engine.cfg, f"partial_tp{index}_pct")
        )
        reason = (
            f"PARTIAL_TP_{tp_r:g}R"
            if index == 1
            else f"PARTIAL_TP{index}_{tp_r:g}R"
        )
        assert trade.reason == reason
        assert trade.exit_price == pytest.approx(
            pos.entry_price + side * tp_r * risk_unit
        )
        assert trade.quantity == pytest.approx(remaining * tp_pct / 100)
        remaining -= trade.quantity
        done_attr = "partial_done" if index == 1 else f"partial{index}_done"
        assert getattr(pos, done_attr)

    assert pos.quantity == pytest.approx(remaining)


@pytest.mark.parametrize("side", [1, -1])
def test_tps_through_tp4_execute_from_nearest_to_farthest(side):
    engine = make_engine(side)

    engine._manage(1)

    assert_tps_executed_in_order(engine, 4, side)


def test_tp4_defaults_and_ordering_validation():
    config = Config()
    assert config.partial_tp4_r == 3.0
    assert config.partial_tp4_pct == 5.0
    assert Config(partial_tp4_r=0, partial_tp4_pct=0).partial_tp4_r == 0

    with pytest.raises(ValueError, match="partial take-profit 4"):
        Config(partial_tp4_r=0, partial_tp4_pct=5.0)
    with pytest.raises(ValueError, match="greater than TP3"):
        Config(partial_tp4_r=2.0)


@pytest.mark.parametrize("side", [1, -1])
def test_tps_through_tp5_execute_from_nearest_to_farthest(side):
    engine = make_engine(side, reach_tp5=True)

    engine._manage(1)

    assert_tps_executed_in_order(engine, 5, side)


def test_tp5_defaults_and_ordering_validation():
    config = Config()
    assert config.partial_tp5_r == 4.0
    assert config.partial_tp5_pct == 5.0
    assert Config(partial_tp5_r=0, partial_tp5_pct=0).partial_tp5_r == 0

    with pytest.raises(ValueError, match="partial take-profit 5"):
        Config(partial_tp5_r=0, partial_tp5_pct=5.0)
    with pytest.raises(ValueError, match="greater than TP4"):
        Config(partial_tp5_r=3.0)


@pytest.mark.parametrize("side", [1, -1])
def test_tps_through_tp6_execute_from_nearest_to_farthest(side):
    engine = make_engine(side, reach_tp6=True)

    engine._manage(1)

    assert_tps_executed_in_order(engine, 6, side)


def test_tp6_defaults_and_ordering_validation():
    config = Config()
    assert config.partial_tp6_r == 5.0
    assert config.partial_tp6_pct == 5.0
    assert Config(partial_tp6_r=0, partial_tp6_pct=0).partial_tp6_r == 0

    with pytest.raises(ValueError, match="partial take-profit 6"):
        Config(partial_tp6_r=0, partial_tp6_pct=5.0)
    with pytest.raises(ValueError, match="greater than TP5"):
        Config(partial_tp6_r=4.0)


@pytest.mark.parametrize("side", [1, -1])
def test_tps_through_tp7_execute_from_nearest_to_farthest(side):
    engine = make_engine(side, reach_tp7=True)

    engine._manage(1)

    assert_tps_executed_in_order(engine, 7, side)


def test_tp7_defaults_and_ordering_validation():
    config = Config()
    assert config.partial_tp7_r == 6.0
    assert config.partial_tp7_pct == 5.0
    assert Config(
        partial_tp7_r=0, partial_tp7_pct=0, partial_tp8_r=0, partial_tp8_pct=0
    ).partial_tp7_r == 0

    with pytest.raises(ValueError, match="partial take-profit 7"):
        Config(
            partial_tp7_r=0,
            partial_tp7_pct=5.0,
            partial_tp8_r=0,
            partial_tp8_pct=0,
        )
    with pytest.raises(ValueError, match="greater than TP6"):
        Config(partial_tp7_r=4.5)


@pytest.mark.parametrize("side", [1, -1])
def test_tps_through_tp8_execute_from_nearest_to_farthest(side):
    engine = make_engine(side, reach_tp8=True)

    engine._manage(1)

    assert_tps_executed_in_order(engine, 8, side)


def test_tp8_defaults_and_ordering_validation():
    config = Config()
    assert config.partial_tp8_r == 7.0
    assert config.partial_tp8_pct == 5.0
    assert Config(partial_tp8_r=0, partial_tp8_pct=0).partial_tp8_r == 0

    with pytest.raises(ValueError, match="partial take-profit 8"):
        Config(partial_tp8_r=0, partial_tp8_pct=5.0)
    with pytest.raises(ValueError, match="greater than TP7"):
        Config(partial_tp8_r=6.0)


@pytest.mark.parametrize("side", [1, -1])
def test_tps_through_tp9_execute_from_nearest_to_farthest(side):
    engine = make_engine(side, reach_tp9=True)

    engine._manage(1)

    assert_tps_executed_in_order(engine, 9, side)


def test_tp9_defaults_and_ordering_validation():
    config = Config()
    assert config.partial_tp9_r == 8.0
    assert config.partial_tp9_pct == 5.0
    assert Config(partial_tp9_r=0, partial_tp9_pct=0).partial_tp9_r == 0

    with pytest.raises(ValueError, match="partial take-profit 9"):
        Config(partial_tp9_r=0, partial_tp9_pct=5.0)
    with pytest.raises(ValueError, match="greater than TP8"):
        Config(partial_tp9_r=7.0)


@pytest.mark.parametrize("side", [1, -1])
def test_tps_through_tp10_execute_from_nearest_to_farthest(side):
    engine = make_engine(side, reach_tp10=True)

    engine._manage(1)

    assert_tps_executed_in_order(engine, 10, side)


def test_tp10_defaults_and_ordering_validation():
    config = Config()
    assert config.partial_tp10_r == 9.0
    assert config.partial_tp10_pct == 5.0
    assert Config(partial_tp10_r=0, partial_tp10_pct=0).partial_tp10_r == 0

    with pytest.raises(ValueError, match="partial take-profit 10"):
        Config(partial_tp10_r=0, partial_tp10_pct=5.0)
    with pytest.raises(ValueError, match="greater than TP9"):
        Config(partial_tp10_r=8.0)
