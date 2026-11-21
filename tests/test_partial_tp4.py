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
        182.0 if reach_tp10
        else 172.0 if reach_tp9
        else 162.0 if reach_tp8
        else 152.0 if reach_tp7
        else 142.0 if reach_tp6
        else 131.0 if reach_tp5
        else 126.0
    )
    low = (
        18.0 if reach_tp10
        else 28.0 if reach_tp9
        else 38.0 if reach_tp8
        else 48.0 if reach_tp7
        else 58.0 if reach_tp6
        else 69.0 if reach_tp5
        else 74.0
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


@pytest.mark.parametrize("side", [1, -1])
def test_tp4_closes_configured_percent_before_nearer_targets(side):
    engine = make_engine(side)

    engine._manage(1)

    trade = engine.trades[0]
    assert trade.reason == "PARTIAL_TP4_2R"
    assert trade.exit_price == (120.0 if side == 1 else 80.0)
    assert trade.quantity == pytest.approx(0.05)
    assert engine.position is not None
    assert engine.position.partial4_done
    assert not engine.position.partial3_done


def test_tp4_defaults_and_ordering_validation():
    config = Config()
    assert config.partial_tp4_r == 2.0
    assert config.partial_tp4_pct == 5.0
    assert Config(partial_tp4_r=0, partial_tp4_pct=0).partial_tp4_r == 0

    with pytest.raises(ValueError, match="partial take-profit 4"):
        Config(partial_tp4_r=0, partial_tp4_pct=5.0)
    with pytest.raises(ValueError, match="greater than TP3"):
        Config(partial_tp4_r=1.5)


@pytest.mark.parametrize("side", [1, -1])
def test_tp5_closes_configured_percent_before_nearer_targets(side):
    engine = make_engine(side, reach_tp5=True)

    engine._manage(1)

    trade = engine.trades[0]
    assert trade.reason == "PARTIAL_TP5_3R"
    assert trade.exit_price == (130.0 if side == 1 else 70.0)
    assert trade.quantity == pytest.approx(0.05)
    assert engine.position is not None
    assert engine.position.partial5_done
    assert not engine.position.partial4_done


def test_tp5_defaults_and_ordering_validation():
    config = Config()
    assert config.partial_tp5_r == 3.0
    assert config.partial_tp5_pct == 5.0
    assert Config(partial_tp5_r=0, partial_tp5_pct=0).partial_tp5_r == 0

    with pytest.raises(ValueError, match="partial take-profit 5"):
        Config(partial_tp5_r=0, partial_tp5_pct=5.0)
    with pytest.raises(ValueError, match="greater than TP4"):
        Config(partial_tp5_r=2.0)


@pytest.mark.parametrize("side", [1, -1])
def test_tp6_closes_configured_percent_before_nearer_targets(side):
    engine = make_engine(side, reach_tp6=True)

    engine._manage(1)

    trade = engine.trades[0]
    assert trade.reason == "PARTIAL_TP6_4R"
    assert trade.exit_price == (140.0 if side == 1 else 60.0)
    assert trade.quantity == pytest.approx(0.05)
    assert engine.position is not None
    assert engine.position.partial6_done
    assert not engine.position.partial5_done


def test_tp6_defaults_and_ordering_validation():
    config = Config()
    assert config.partial_tp6_r == 4.0
    assert config.partial_tp6_pct == 5.0
    assert Config(partial_tp6_r=0, partial_tp6_pct=0).partial_tp6_r == 0

    with pytest.raises(ValueError, match="partial take-profit 6"):
        Config(partial_tp6_r=0, partial_tp6_pct=5.0)
    with pytest.raises(ValueError, match="greater than TP5"):
        Config(partial_tp6_r=3.0)


@pytest.mark.parametrize("side", [1, -1])
def test_tp7_closes_configured_percent_before_nearer_targets(side):
    engine = make_engine(side, reach_tp7=True)

    engine._manage(1)

    trade = engine.trades[0]
    assert trade.reason == "PARTIAL_TP7_5R"
    assert trade.exit_price == (150.0 if side == 1 else 50.0)
    assert trade.quantity == pytest.approx(0.05)
    assert engine.position is not None
    assert engine.position.partial7_done
    assert not engine.position.partial6_done


def test_tp7_defaults_and_ordering_validation():
    config = Config()
    assert config.partial_tp7_r == 5.0
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
        Config(partial_tp7_r=3.5)


@pytest.mark.parametrize("side", [1, -1])
def test_tp8_closes_configured_percent_before_nearer_targets(side):
    engine = make_engine(side, reach_tp8=True)

    engine._manage(1)

    trade = engine.trades[0]
    assert trade.reason == "PARTIAL_TP8_6R"
    assert trade.exit_price == (160.0 if side == 1 else 40.0)
    assert trade.quantity == pytest.approx(0.05)
    assert engine.position is not None
    assert engine.position.partial8_done
    assert not engine.position.partial7_done


def test_tp8_defaults_and_ordering_validation():
    config = Config()
    assert config.partial_tp8_r == 6.0
    assert config.partial_tp8_pct == 5.0
    assert Config(partial_tp8_r=0, partial_tp8_pct=0).partial_tp8_r == 0

    with pytest.raises(ValueError, match="partial take-profit 8"):
        Config(partial_tp8_r=0, partial_tp8_pct=5.0)
    with pytest.raises(ValueError, match="greater than TP7"):
        Config(partial_tp8_r=4.0)


@pytest.mark.parametrize("side", [1, -1])
def test_tp9_closes_configured_percent_before_nearer_targets(side):
    engine = make_engine(side, reach_tp9=True)

    engine._manage(1)

    trade = engine.trades[0]
    assert trade.reason == "PARTIAL_TP9_7R"
    assert trade.exit_price == (170.0 if side == 1 else 30.0)
    assert trade.quantity == pytest.approx(0.05)
    assert engine.position is not None
    assert engine.position.partial9_done
    assert not engine.position.partial8_done


def test_tp9_defaults_and_ordering_validation():
    config = Config()
    assert config.partial_tp9_r == 7.0
    assert config.partial_tp9_pct == 5.0
    assert Config(partial_tp9_r=0, partial_tp9_pct=0).partial_tp9_r == 0

    with pytest.raises(ValueError, match="partial take-profit 9"):
        Config(partial_tp9_r=0, partial_tp9_pct=5.0)
    with pytest.raises(ValueError, match="greater than TP8"):
        Config(partial_tp9_r=6.0)


@pytest.mark.parametrize("side", [1, -1])
def test_tp10_closes_configured_percent_before_nearer_targets(side):
    engine = make_engine(side, reach_tp10=True)

    engine._manage(1)

    trade = engine.trades[0]
    assert trade.reason == "PARTIAL_TP10_8R"
    assert trade.exit_price == (180.0 if side == 1 else 20.0)
    assert trade.quantity == pytest.approx(0.05)
    assert engine.position is not None
    assert engine.position.partial10_done
    assert not engine.position.partial9_done


def test_tp10_defaults_and_ordering_validation():
    config = Config()
    assert config.partial_tp10_r == 8.0
    assert config.partial_tp10_pct == 5.0
    assert Config(partial_tp10_r=0, partial_tp10_pct=0).partial_tp10_r == 0

    with pytest.raises(ValueError, match="partial take-profit 10"):
        Config(partial_tp10_r=0, partial_tp10_pct=5.0)
    with pytest.raises(ValueError, match="greater than TP9"):
        Config(partial_tp10_r=7.0)
