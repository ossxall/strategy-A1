import numpy as np
import pandas as pd

from strategy.config import Config
from strategy.indicators import ema_band_touch_flags
from strategy.models import Position
from strategy.strategy import Backtester


def bars_from_closes(closes):
    index = pd.date_range("2026-01-01", periods=len(closes), freq="30min", tz="UTC")
    close = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": 1.0,
        },
        index=index,
    )


def make_engine(closes, touches, *, side=1, timeframe="30min", entry_bar=0):
    bars = bars_from_closes(closes)
    config = Config(
        timeframe=timeframe,
        streak_trend_only=False,
        entry_partial_enabled=False,
        partial_tp_enabled=False,
        partial_tp2_r=0,
        partial_tp2_pct=0,
        stop_loss_pct=0,
        monthly_loss_limit_pct=0,
        force_close_at_end=False,
        candle_pattern_exit_enabled=False,
        ema55_touch_exit_enabled=True,
        ema55_touch_lookback_bars=5,
        ema55_touch_min_touches=3,
        ema55_touch_consecutive=2,
        ema55_touch_breakout_buffer_pct=0.1,
    )
    signals = pd.DataFrame(
        {"valid": True, "target": 0, "trend_bias": 0}, index=bars.index
    )
    engine = Backtester(bars, config, signals=signals)
    engine.ema55_line = np.full(len(bars), 100.0)
    engine.ema55_touches = np.asarray(touches, dtype=bool)
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
        entry_bar_index=entry_bar,
        partial_entry_done=True,
    )
    return engine


def test_ema_band_touch_detects_crosses_and_nearby_brushes():
    close = np.full(55, 100.0)
    bars = pd.DataFrame(
        {
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
        }
    )
    # Exact touch, within the 0.1% band, and outside the band on either side.
    bars.loc[10, ["high", "low"]] = [100.0, 100.0]
    bars.loc[11, ["high", "low"]] = [100.05, 100.02]
    bars.loc[12, ["high", "low"]] = [100.3, 100.2]
    bars.loc[13, ["high", "low"]] = [99.8, 99.7]

    line, touches = ema_band_touch_flags(bars, period=55, tolerance_pct=0.1)

    assert line[10:14].tolist() == [100.0] * 4
    assert touches[10:14].tolist() == [True, True, False, False]


def test_two_consecutive_ema55_touches_exit_long_on_next_open():
    engine = make_engine(
        [100.2, 99.3, 100.0], [True, True, False]
    )

    trades, _ = engine.run()

    assert trades.reason.tolist() == ["EMA55_REPEATED_TOUCH_WEAKNESS"]
    assert trades.exit_time.iloc[0] == engine.bars.index[2]
    assert trades.exit_price.iloc[0] == engine.bars.open.iloc[2]


def test_three_repeated_touches_trigger_when_not_consecutive():
    engine = make_engine(
        [100.2, 100.2, 100.2, 100.2, 99.3, 100.0],
        [True, False, True, False, True, False],
    )

    trades, _ = engine.run()

    assert trades.reason.tolist() == ["EMA55_REPEATED_TOUCH_WEAKNESS"]
    assert trades.exit_time.iloc[0] == engine.bars.index[5]


def test_repeated_touches_do_not_exit_if_close_confirms_break_above_ema55():
    engine = make_engine(
        [100.2, 100.2, 100.2, 100.2], [True, True, True, True]
    )

    trades, _ = engine.run()

    assert trades.empty
    assert engine.position is not None


def test_repeated_touches_without_ema55_loss_threshold_do_not_exit():
    engine = make_engine(
        [100.2, 99.9, 100.0], [True, True, False]
    )

    trades, _ = engine.run()

    assert trades.empty
    assert engine.position is not None


def test_two_consecutive_ema55_touches_exit_short_on_failed_downside_break():
    short = make_engine([100.2, 100.9, 100.0], [True, True, False], side=-1)

    trades, _ = short.run()

    assert trades.reason.tolist() == ["EMA55_REPEATED_TOUCH_WEAKNESS_SHORT"]
    assert trades.exit_time.iloc[0] == short.bars.index[2]


def test_short_touch_exit_uses_its_stricter_loss_threshold():
    short = make_engine([100.2, 100.7, 100.0], [True, True, False], side=-1)

    trades, _ = short.run()

    assert trades.empty
    assert short.position is not None


def test_ema55_touch_exit_is_disabled_outside_30m():
    other_timeframe = make_engine(
        [100.2, 99.3, 100.0], [True, True, False], timeframe="15min"
    )

    other_trades, _ = other_timeframe.run()

    assert other_trades.empty
    assert other_timeframe.position is not None


def test_touches_before_long_entry_do_not_count():
    engine = make_engine(
        [100.2, 100.2, 100.2, 100.2, 100.05],
        [True, True, True, False, True],
        entry_bar=3,
    )

    trades, _ = engine.run()

    assert trades.empty
    assert engine.position is not None
