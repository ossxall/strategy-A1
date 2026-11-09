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
from strategy.utils import resample_ohlcv


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
):
    bars = make_bars(side)
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
def test_reversal_pattern_exits_on_its_candle_close_and_next_open(side, reason, reversal):
    kwargs = {
        "reversal_bear": [False, True, False] if reversal == "bear" else None,
        "reversal_bull": [False, True, False] if reversal == "bull" else None,
        "doji": [False, True, False],
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


def test_directional_pattern_exits_without_doji_or_open_loss():
    engine = prepared_engine(1, reversal_bear=[False, True, False])
    engine.c = engine.c.copy()
    engine.c[1] = 100.05

    trades, _ = engine.run()

    assert trades.reason.tolist() == ["CANDLE_PATTERN_BEAR"]
    assert trades.exit_time.iloc[0] == engine.bars.index[2]


def test_signed_pattern_exits_without_doji_or_loss_threshold():
    engine = prepared_engine(
        1,
        signed_bear=[False, True, False],
    )
    engine.c = engine.c.copy()
    engine.c[1] = 100.05

    trades, _ = engine.run()

    assert trades.reason.tolist() == ["CANDLE_SIGNED_BEAR"]


def test_exit_percentage_scales_out_once_per_position():
    engine = prepared_engine(
        1,
        reversal_bear=[False, True, False],
        doji=[False, True, False],
    )
    engine.cfg.candle_pattern_exit_pct = 50.0

    trades, _ = engine.run()

    assert trades.quantity.iloc[0] == pytest.approx(0.5)
    assert engine.position is not None
    assert engine.position.quantity == pytest.approx(0.5)
    assert engine.position.candle_exit_done


def test_pattern_can_exit_on_first_bar_after_initial_entry():
    engine = prepared_engine(
        1,
        reversal_bear=[True, False, False],
    )

    trades, _ = engine.run()

    assert trades.reason.tolist() == ["CANDLE_PATTERN_BEAR"]
    assert trades.exit_time.iloc[0] == engine.bars.index[1]
    assert trades.exit_price.iloc[0] == engine.bars.open.iloc[1]


def test_pattern_before_position_entry_does_not_trigger_later():
    engine = prepared_engine(
        1,
        reversal_bear=[True, False, False],
        doji=[True, False, False],
    )
    engine.position.entry_bar_index = 1

    trades, _ = engine.run()

    assert trades.empty
    assert engine.position is not None


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
        EMACrossStrategy(Config(
            timeframe="30min",
            candle_pattern_exit_timeframe=("30min",),
            streak_trend_only=False,
        ))
        .prepare_candle_exits(bars)
    )

    assert reversal_bull.tolist() == [True, False, False]
    assert reversal_bear.tolist() == [False, True, False]
    assert signed_bull.tolist() == [False, False, True]
    assert signed_bear.tolist() == [False, True, False]
    assert doji.tolist() == [False, False, True]


def test_candle_exit_patterns_align_a_configured_higher_timeframe(monkeypatch):
    patterns = (
        LONG_CANDLE_PATTERNS + SHORT_CANDLE_PATTERNS
        + SIGNED_CANDLE_PATTERNS + NEUTRAL_CANDLE_PATTERNS
    )

    def zero_pattern(open_, high, low, close):
        return np.zeros(len(close), dtype=np.int32)

    for name in set(patterns):
        monkeypatch.setattr(talib, name, zero_pattern)

    def higher_timeframe_pattern(open_, high, low, close):
        values = np.zeros(len(close), dtype=np.int32)
        if len(values) == 2:
            values[-1] = -100
        return values

    def higher_timeframe_doji(open_, high, low, close):
        values = np.zeros(len(close), dtype=np.int32)
        if len(values) == 2:
            values[-1] = 100
        return values

    monkeypatch.setattr(talib, "CDLHANGINGMAN", higher_timeframe_pattern)
    monkeypatch.setattr(talib, "CDLDOJI", higher_timeframe_doji)

    index = pd.date_range("2026-01-01", periods=8 * 60, freq="min", tz="UTC")
    raw = pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1.0},
        index=index,
    )
    bars = resample_ohlcv(raw, "30min")
    cfg = Config(
        timeframe="30min",
        candle_pattern_exit_timeframe=("4h",),
        streak_trend_only=False,
    )

    _, reversal_bear, _, _, doji = EMACrossStrategy(cfg).prepare_candle_exits(bars, raw)

    assert np.flatnonzero(reversal_bear).tolist() == [15]
    assert np.flatnonzero(doji).tolist() == [15]


def test_candle_pattern_exit_timeframe_defaults_to_30m_and_4h():
    cfg = Config()

    assert cfg.candle_pattern_exit_timeframes == ("15min", "30min", "1h", "4h", "1D")
