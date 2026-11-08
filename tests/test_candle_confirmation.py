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


def make_bars(index):
    close = np.arange(len(index), dtype=float) + 100.0
    return pd.DataFrame(
        {
            "open": close - 0.25,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1.0,
        },
        index=index,
    )


def patch_patterns(monkeypatch, handlers=None):
    handlers = handlers or {}

    def zero_pattern(open_, high, low, close):
        return np.zeros(len(open_), dtype=np.int32)

    for name in set(
        LONG_CANDLE_PATTERNS
        + SHORT_CANDLE_PATTERNS
        + SIGNED_CANDLE_PATTERNS
        + NEUTRAL_CANDLE_PATTERNS
    ):
        monkeypatch.setattr(talib, name, handlers.get(name, zero_pattern))


def test_catalog_covers_requested_pattern_counts():
    assert len(LONG_CANDLE_PATTERNS) == 15
    assert len(SHORT_CANDLE_PATTERNS) == 14
    assert len(SIGNED_CANDLE_PATTERNS) == 27
    assert len(NEUTRAL_CANDLE_PATTERNS) == 5


def test_signed_and_one_sided_patterns_confirm_matching_direction(monkeypatch):
    def pattern(values):
        return lambda open_, high, low, close: np.asarray(values, dtype=np.int32)

    patch_patterns(
        monkeypatch,
        {
            "CDLHAMMER": pattern([0, 100, 0]),
            "CDLENGULFING": pattern([0, 0, -100]),
            "CDLDOJI": pattern([0, 100, 0]),
        },
    )
    index = pd.date_range("2026-01-01", periods=3, freq="30min", tz="UTC")
    strategy = EMACrossStrategy(Config(timeframe="30min", streak_trend_only=False))

    long_signal, short_signal, neutral = strategy._candle_confirmations(make_bars(index))

    assert long_signal.tolist() == [False, True, False]
    assert short_signal.tolist() == [False, False, True]
    assert neutral.tolist() == [False, True, False]


def test_higher_timeframe_pattern_overrides_lower_timeframe_conflict(monkeypatch):
    def raw_hammer(open_, high, low, close):
        values = np.zeros(len(open_), dtype=np.int32)
        if len(values) == 12:
            values[2] = 100  # 00:15, dentro de la primera vela de 30m
        return values

    def strategy_hanging_man(open_, high, low, close):
        values = np.zeros(len(open_), dtype=np.int32)
        if len(values) == 2:
            values[0] = -100  # cierre 00:30: prevalece sobre el martillo de 5m
        return values

    patch_patterns(
        monkeypatch,
        {"CDLHAMMER": raw_hammer, "CDLHANGINGMAN": strategy_hanging_man},
    )
    raw_index = pd.date_range("2026-01-01", periods=12, freq="5min", tz="UTC")
    strategy_index = pd.date_range("2026-01-01", periods=2, freq="30min", tz="UTC")
    raw = make_bars(raw_index)
    bars = make_bars(strategy_index)
    strategy = EMACrossStrategy(Config(timeframe="30min", streak_trend_only=False))

    long_signal, short_signal, _ = strategy._candle_confirmations(bars, raw)

    assert long_signal.tolist() == [False, False]
    assert short_signal.tolist() == [True, False]


def test_doji_is_neutral_and_never_directional(monkeypatch):
    def doji(open_, high, low, close):
        values = np.zeros(len(open_), dtype=np.int32)
        values[0] = 100
        return values

    patch_patterns(monkeypatch, {"CDLDOJI": doji})
    index = pd.date_range("2026-01-01", periods=2, freq="30min", tz="UTC")
    strategy = EMACrossStrategy(Config(timeframe="30min", streak_trend_only=False))

    long_signal, short_signal, neutral = strategy._candle_confirmations(make_bars(index))

    assert not long_signal.any()
    assert not short_signal.any()
    assert neutral.tolist() == [True, False]


@pytest.mark.parametrize(
    ("side", "column", "entry_price"),
    [(1, "long_candle_confirmation", 100.0),
     (-1, "short_candle_confirmation", 102.0)],
)
def test_matching_candle_pattern_confirms_scale_in_for_both_sides(
    side, column, entry_price
):
    index = pd.date_range("2026-01-01", periods=2, freq="30min", tz="UTC")
    bars = make_bars(index)
    signals = pd.DataFrame(
        {"long_candle_confirmation": [False, column.startswith("long")],
         "short_candle_confirmation": [False, column.startswith("short")]},
        index=index,
    )
    config = Config(
        timeframe="30min",
        streak_trend_only=False,
        entry_confirm_max_move_pct=0.0,
    )
    backtester = Backtester(bars, config, signals=signals)
    backtester.position = Position(
        position_id=1,
        side=side,
        quantity=0.5,
        initial_quantity=0.5,
        entry_price=entry_price,
        entry_time=index[0],
        entry_fee=0.0,
        stop_pct=config.stop_loss_pct,
        risk_amount=1.0,
        target_quantity=1.0,
        entry_bar_index=0,
    )

    assert backtester._should_add_partial(1)


def test_pattern_does_not_confirm_when_close_is_against_entry():
    index = pd.date_range("2026-01-01", periods=2, freq="30min", tz="UTC")
    bars = make_bars(index)
    bars.loc[index[1], "close"] = 99.0
    signals = pd.DataFrame(
        {"long_candle_confirmation": [False, True],
         "short_candle_confirmation": [False, False]},
        index=index,
    )
    config = Config(
        timeframe="30min",
        streak_trend_only=False,
        entry_confirm_max_move_pct=0.0,
    )
    backtester = Backtester(bars, config, signals=signals)
    backtester.position = Position(
        position_id=1,
        side=1,
        quantity=0.5,
        initial_quantity=0.5,
        entry_price=100.0,
        entry_time=index[0],
        entry_fee=0.0,
        stop_pct=config.stop_loss_pct,
        risk_amount=1.0,
        target_quantity=1.0,
        entry_bar_index=0,
    )

    assert not backtester._should_add_partial(1)
