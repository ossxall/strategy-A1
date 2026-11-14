import numpy as np
import pandas as pd
import pytest

from strategy.config import Config
from strategy.indicators import volume_decline_flags
from strategy.models import Position
from strategy.strategy import Backtester


def make_engine(volumes, *, side=1):
    index = pd.date_range("2026-01-01", periods=len(volumes), freq="30min", tz="UTC")
    close = np.full(len(volumes), 100.0)
    bars = pd.DataFrame(
        {
            "open": close,
            "high": close + 0.1,
            "low": close - 0.1,
            "close": close,
            "volume": volumes,
        },
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
        partial_tp_enabled=False,
        partial_tp2_r=0,
        partial_tp2_pct=0,
        stop_loss_pct=0,
        monthly_loss_limit_pct=0,
        streak_trend_only=False,
        force_close_at_end=False,
        volume_exit_bars=10,
        volume_exit_pct=25.0,
        volume_exit_min_drop_pct=20.0,
    )
    signals = pd.DataFrame(
        {"valid": True, "target": 0, "trend_bias": 0}, index=index
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
        stop_pct=0.0,
        risk_amount=0.0,
    )
    return engine


def test_volume_decline_flags_detects_falling_volume_over_n_bars():
    volume = np.array([100.0] * 5 + [80.0] * 5)

    flags = volume_decline_flags(volume, bars=10, min_drop_pct=20.0)

    assert not flags[:9].any()
    assert flags[9]


def test_volume_decline_flags_ignores_rising_or_flat_volume():
    rising = np.array([80.0] * 5 + [100.0] * 5)
    flat = np.full(10, 100.0)

    assert not volume_decline_flags(rising, bars=10, min_drop_pct=20.0).any()
    assert not volume_decline_flags(flat, bars=10, min_drop_pct=20.0).any()


def test_volume_decline_flags_requires_minimum_drop():
    volume = np.array([100.0] * 5 + [90.0] * 5)

    assert not volume_decline_flags(volume, bars=10, min_drop_pct=20.0).any()
    assert volume_decline_flags(volume, bars=10, min_drop_pct=10.0)[-1]


@pytest.mark.parametrize("side", [1, -1])
def test_volume_decline_closes_configured_percent_once(side):
    engine = make_engine([100.0] * 12, side=side)
    engine.volume_exit = np.array(
        [False, True, True, True] + [False] * 8, dtype=bool
    )

    trades, _ = engine.run()

    assert trades.reason.tolist() == ["VOLUME_DECLINE_10B_25%"]
    assert trades.exit_time.iloc[0] == engine.bars.index[2]
    assert trades.quantity.iloc[0] == pytest.approx(0.25)
    assert engine.position is not None
    assert engine.position.quantity == pytest.approx(0.75)
    assert engine.position.volume_exit_done


def test_backtester_computes_volume_exit_from_bars():
    engine = make_engine([100.0] * 5 + [80.0] * 6)

    trades, _ = engine.run()

    assert engine.volume_exit[9]
    assert trades.reason.tolist() == ["VOLUME_DECLINE_10B_25%"]
    assert trades.exit_time.iloc[0] == engine.bars.index[10]


def test_volume_decline_exit_is_ignored_when_disabled():
    engine = make_engine([100.0] * 12)
    engine.cfg.volume_exit_enabled = False
    engine.volume_exit = np.ones(len(engine.bars), dtype=bool)

    trades, _ = engine.run()

    assert trades.empty
    assert engine.position is not None


def test_volume_exit_validation():
    assert Config().volume_exit_bars == 20
    assert Config().volume_exit_pct == 20.0
    assert Config(volume_exit_enabled=False, volume_exit_bars=1).volume_exit_bars == 1

    with pytest.raises(ValueError, match="volume_exit_bars"):
        Config(volume_exit_bars=1)
    with pytest.raises(ValueError, match="volume_exit_pct"):
        Config(volume_exit_pct=0)
    with pytest.raises(ValueError, match="volume_exit_min_drop_pct"):
        Config(volume_exit_min_drop_pct=100)
