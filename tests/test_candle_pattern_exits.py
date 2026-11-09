import numpy as np
import pandas as pd
import pytest

from strategy.config import Config
from strategy.models import Position
from strategy.strategy import (
    LONG_CANDLE_PATTERNS,
    NEUTRAL_CANDLE_PATTERNS,
    SHORT_CANDLE_PATTERNS,
    SIGNED_CANDLE_PATTERNS,
    Backtester,
    EMACrossStrategy,
    talib,
)


def make_bars(side, periods=3):
    index = pd.date_range("2026-01-01", periods=periods, freq="30min", tz="UTC")
    if side == 1:
        values = {
            "open": [100.0, 100.0, 99.3],
            "high": [100.4, 100.1, 99.6],
            "low": [99.8, 99.1, 99.0],
            "close": [100.1, 99.3, 99.3],
        }
    else:
        values = {
            "open": [100.0, 100.0, 100.7],
            "high": [100.2, 100.9, 101.0],
            "low": [99.6, 99.9, 100.4],
            "close": [99.9, 100.7, 100.7],
        }
    return pd.DataFrame({**values, "volume": 1.0}, index=index)


def prepared_engine(
    side,
    *,
    reversal_bull=None,
    reversal_bear=None,
    signed_bull=None,
    signed_bear=None,
    doji=None,
    loss_pct=0.7,
):
    bars = make_bars(side)
    if loss_pct != 0.7:
        bars.loc[bars.index[1], "close"] = 100.0 * (
            1.0 - loss_pct / 100.0 if side == 1 else 1.0 + loss_pct / 100.0
        )
    zeros = np.zeros(len(bars), dtype=bool)
    cfg = Config(
        timeframe="30min",
        streak_trend_only=False,
        entry_partial_enabled=False,
        partial_tp_enabled=False,
        partial_tp2_r=0,
        partial_tp2_pct=0,
        stop_loss_pct=0,
        monthly_loss_limit_pct=0,
        force_close_at_end=False,
        ema55_touch_exit_enabled=False,
    )
    signals = pd.DataFrame(
        {"valid": True, "target": 0, "trend_bias": 0}, index=bars.index
    )
    engine = Backtester(
        bars,
        cfg,
        signals=signals,
        candle_bull_exit=reversal_bull if reversal_bull is not None else zeros.copy(),
        candle_bear_exit=reversal_bear if reversal_bear is not None else zeros.copy(),
        signed_bull_exit=signed_bull if signed_bull is not None else zeros.copy(),
        signed_bear_exit=signed_bear if signed_bear is not None else zeros.copy(),
        doji_exit=doji if doji is not None else zeros.copy(),
    )
    engine.position = Position(
        position_id=1,
        side=side,
        quantity=1.0,
        initial_quantity=1.0,
        entry_price=100.0,
        entry_time=bars.index[0],
        entry_fee=0.0,
        stop_pct=0.0,
        risk_amount=0.0,
        target_quantity=1.0,
        partial_entry_done=True,
        entry_bar_index=0,
    )
    return engine


@pytest.mark.parametrize(
    ("side", "reason", "reversal"),
    [
        (1, "CANDLE_PATTERN_BEAR", "bear"),
        (-1, "CANDLE_PATTERN_BULL", "bull"),
    ],
)
def test_reversal_pattern_requires_doji_break_and_exits_next_open(side, reason, reversal):
    kwargs = {
        "reversal_bear": [True, False, False] if reversal == "bear" else None,
        "reversal_bull": [True, False, False] if reversal == "bull" else None,
        "doji": [True, False, False],
    }
    engine = prepared_engine(side, **kwargs)

    trades, _ = engine.run()

    assert trades.reason.tolist() == [reason]
    assert trades.exit_time.iloc[0] == engine.bars.index[2]
    assert trades.exit_price.iloc[0] == engine.bars.open.iloc[2]


@pytest.mark.parametrize("side", [1, -1])
def test_doji_alone_never_exits(side):
    engine = prepared_engine(side, doji=[True, True, True])

    trades, _ = engine.run()

    assert trades.empty
    assert engine.position is not None


def test_reversal_without_doji_confirmation_does_not_exit():
    engine = prepared_engine(1, reversal_bear=[True, False, False])

    trades, _ = engine.run()

    assert trades.empty
    assert engine.position is not None


def test_reversal_must_break_the_pattern_extreme_while_position_is_losing():
    engine = prepared_engine(
        1,
        reversal_bear=[True, False, False],
        doji=[True, False, False],
        loss_pct=0.5,
    )

    trades, _ = engine.run()

    assert trades.empty
    assert engine.position is not None


def test_signed_patterns_use_stricter_loss_threshold():
    engine = prepared_engine(
        1,
        signed_bear=[True, False, False],
        doji=[True, False, False],
        loss_pct=0.9,
    )

    trades, _ = engine.run()

    assert trades.reason.tolist() == ["CANDLE_SIGNED_BEAR"]


def test_signed_pattern_does_not_exit_before_its_stricter_loss_threshold():
    engine = prepared_engine(
        1,
        signed_bear=[True, False, False],
        doji=[True, False, False],
        loss_pct=0.7,
    )

    trades, _ = engine.run()

    assert trades.empty
    assert engine.position is not None


def test_exit_percentage_scales_out_once_per_position():
    engine = prepared_engine(
        1,
        reversal_bear=[True, False, False],
        doji=[True, False, False],
    )
    engine.cfg.candle_pattern_exit_pct = 50.0

    trades, _ = engine.run()

    assert trades.quantity.iloc[0] == pytest.approx(0.5)
    assert engine.position is not None
    assert engine.position.quantity == pytest.approx(0.5)
    assert engine.position.candle_exit_done


def test_prepare_candle_exits_keeps_all_talib_categories_separate(monkeypatch):
    patterns = (
        LONG_CANDLE_PATTERNS + SHORT_CANDLE_PATTERNS
        + SIGNED_CANDLE_PATTERNS + NEUTRAL_CANDLE_PATTERNS
    )

    def zero_pattern(open_, high, low, close):
        return np.zeros(len(open_), dtype=np.int32)

    for name in set(patterns):
        monkeypatch.setattr(talib, name, zero_pattern)

    def values(result):
        return lambda open_, high, low, close: np.asarray(result, dtype=np.int32)

    monkeypatch.setattr(talib, "CDLHAMMER", values([100, 0, 0]))
    monkeypatch.setattr(talib, "CDLHANGINGMAN", values([0, -100, 0]))
    monkeypatch.setattr(talib, "CDLENGULFING", values([0, -100, 100]))
    monkeypatch.setattr(talib, "CDLDOJI", values([0, 0, 100]))
    bars = make_bars(1)

    reversal_bull, reversal_bear, signed_bull, signed_bear, doji = (
        EMACrossStrategy(Config(timeframe="30min", streak_trend_only=False))
        .prepare_candle_exits(bars)
    )

    assert reversal_bull.tolist() == [True, False, False]
    assert reversal_bear.tolist() == [False, True, False]
    assert signed_bull.tolist() == [False, False, True]
    assert signed_bear.tolist() == [False, True, False]
    assert doji.tolist() == [False, False, True]
