import pandas as pd
import pytest

from live.bot import build_events, ts_ms
from strategy.config import Config
from strategy.models import Position
from strategy.strategy import Backtester


def setup_entry():
    index = pd.date_range("2026-01-01", periods=3, freq="30min", tz="UTC")
    bars = pd.DataFrame(
        {
            "open": [100.0, 100.0, 104.0],
            "high": [100.5, 104.5, 104.5],
            "low": [99.5, 99.5, 103.5],
            "close": [100.0, 104.0, 104.0],
            "volume": [1.0, 1.0, 1.0],
        },
        index=index,
    )
    signals = pd.DataFrame(
        {
            "valid": [True, True, True],
            "target": [1, 0, 0],
            "trend_bias": [1, 1, 1],
            "ema_fast": [100.0, 100.0, 100.0],
            "ema_slow": [99.0, 99.0, 99.0],
            "adx_entry": [30.0, 30.0, 30.0],
        },
        index=index,
    )
    config = Config(
        timeframe="30min",
        stop_loss_pct=0.10,
        entry_partial_enabled=True,
        entry_partial_pct=50.0,
        entry_atr_enabled=False,
        entry_rsi_divergence_enabled=False,
        partial_tp_enabled=False,
        partial_tp2_r=0.0,
        partial_tp2_pct=0.0,
        ema55_touch_exit_enabled=False,
        ema_weakening_exit_enabled=False,
        adx_exit_enabled=False,
        rsi_divergence_exit_enabled=False,
        streak_trend_only=False,
        monthly_loss_limit_pct=0.0,
        force_close_at_end=False,
    )
    return index, bars, signals, config


def test_entry_is_split_between_next_open_and_same_candle_close():
    index, bars, signals, config = setup_entry()
    engine = Backtester(bars, config, signals=signals)
    engine.run()

    assert len(engine.opened) == 1
    assert engine.opened[0]["time"] == index[1]
    assert engine.opened[0]["entry_pct"] == 50.0
    assert len(engine.adds) == 1
    assert engine.adds[0]["time"] == index[2]
    assert engine.adds[0]["price"] == bars.iloc[1]["close"]
    assert engine.position is not None
    assert engine.position.quantity == engine.position.target_quantity
    assert engine.position.entry_price == 102.0
    assert engine.position.first_entry_price == 100.0
    assert engine.position.entry_signal_high == bars.iloc[0]["high"]
    assert engine.position.entry_signal_low == bars.iloc[0]["low"]


@pytest.mark.parametrize(
    ("side", "following_close", "should_exit"),
    [
        (1, 99.0, True),   # LONG: cierre por debajo de EMA rápida
        (1, 100.0, False), # LONG: igualdad no cuenta como estar por debajo
        (-1, 101.0, True), # SHORT: cierre por encima de EMA rápida
        (-1, 100.0, False),# SHORT: igualdad no cuenta como estar por encima
    ],
)
def test_first_candle_after_entry_closes_against_fast_ema(
    side, following_close, should_exit
):
    index, bars, signals, config = setup_entry()
    config.rsi_short_exit_enabled = False
    signals.loc[index[0], "target"] = side

    # La entrada se ejecuta en la apertura de index[1]. La condición se
    # comprueba al cierre de index[2] y, si aplica, se ejecuta en index[3].
    bars.loc[index[2], ["open", "high", "low", "close"]] = [
        100.0, max(100.5, following_close), min(99.5, following_close), following_close
    ]
    next_index = index[-1] + pd.Timedelta("30min")
    bars.loc[next_index] = [100.0, 100.5, 99.5, 100.0, 1.0]
    signals.loc[next_index] = signals.iloc[-1]

    engine = Backtester(bars, config, signals=signals)
    engine.run()

    if should_exit:
        assert engine.position is None
        assert engine.trades[-1].reason == "FIRST_CANDLE_EMA_FAST_EXIT"
        assert engine.trades[-1].exit_time == next_index
    else:
        assert engine.position is not None
        assert not engine.trades


@pytest.mark.parametrize(
    ("side", "close", "expected_add"),
    [
        (1, 101.0, True),
        (1, 100.5, False),
        (1, 100.25, False),
        (1, 99.0, False),
        (-1, 99.0, True),
        (-1, 99.5, False),
        (-1, 99.75, False),
        (-1, 101.0, False),
    ],
)
def test_entry_add_requires_close_breakout_of_signal_candle(side, close, expected_add):
    index, bars, signals, config = setup_entry()
    config.rsi_short_exit_enabled = False
    signals.loc[index[0], "target"] = side
    bars.loc[index[1], ["open", "high", "low", "close"]] = [
        100.0, max(100.5, close), min(99.5, close), close
    ]
    bars.loc[index[2], ["open", "high", "low", "close"]] = [
        100.0, 100.4, 99.6, 100.0
    ]

    engine = Backtester(bars, config, signals=signals)
    engine.run()

    assert bool(engine.adds) is expected_add
    assert engine.position is not None
    if expected_add:
        assert engine.position.partial_entry_done
        assert engine.position.quantity == engine.position.target_quantity
    else:
        assert not engine.position.partial_entry_done
        assert engine.position.quantity < engine.position.target_quantity
        assert engine.position.entry_remaining_quantity > 0.0


@pytest.mark.parametrize(
    ("side", "entry_close", "confirmation_close"),
    [(1, 100.25, 101.0), (-1, 99.75, 99.0)],
)
def test_entry_add_waits_for_breakout_on_a_later_candle(side, entry_close, confirmation_close):
    index, bars, signals, config = setup_entry()
    config.rsi_short_exit_enabled = False
    signals.loc[index[0], "target"] = side
    bars.loc[index[1], ["open", "high", "low", "close"]] = [
        100.0, 101.0, 99.0, entry_close
    ]
    bars.loc[index[2], ["open", "high", "low", "close"]] = [
        100.0,
        max(confirmation_close, 100.5),
        min(confirmation_close, 99.5),
        confirmation_close,
    ]

    engine = Backtester(bars, config, signals=signals)
    engine.run()

    assert len(engine.adds) == 1
    assert engine.adds[0]["time"] == index[2] + pd.Timedelta("30min")
    assert engine.position is not None
    assert engine.position.partial_entry_done
    assert engine.position.quantity == engine.position.target_quantity


def test_stop_loss_closes_first_tranche_before_pending_add_can_break_out():
    index, bars, signals, config = setup_entry()
    signals.loc[index[0], "target"] = 1
    bars.loc[index[1], ["open", "high", "low", "close"]] = [100.0, 100.4, 99.6, 100.0]
    bars.loc[index[2], ["open", "high", "low", "close"]] = [100.0, 101.0, 89.0, 101.0]

    engine = Backtester(bars, config, signals=signals)
    engine.run()

    assert not engine.adds
    assert engine.position is None
    assert engine.trades[-1].reason.startswith("STOP_LOSS")


def test_pending_add_executes_on_breakout_even_after_tp_moves_stop_to_breakeven():
    index, bars, signals, config = setup_entry()
    config.stop_loss_pct = 0.01
    config.partial_tp_enabled = True
    config.partial_tp_r = 0.9
    config.partial_tp_pct = 25.0
    config.rsi_short_exit_enabled = False
    signals.loc[index[0], "target"] = -1
    bars.loc[index[1], ["open", "high", "low", "close"]] = [100.0, 100.5, 98.9, 99.0]
    bars.loc[index[2], ["open", "high", "low", "close"]] = [99.0, 99.5, 98.5, 99.0]

    engine = Backtester(bars, config, signals=signals)
    engine.run()

    assert any(trade.reason == "PARTIAL_TP_0.9R" for trade in engine.trades)
    assert len(engine.adds) == 1
    assert engine.adds[0]["time"] == index[2]
    assert engine.position is not None
    assert engine.position.breakeven
    assert engine.position.partial_entry_done


def test_live_event_reports_add_at_close_of_entry_candle():
    index, bars, signals, config = setup_entry()
    events = build_events(
        bars, signals, None, None, None, config, "BTCUSDT", "30m"
    )

    entry = next(event for event in events if event["event"] == "ENTRY")
    add = next(event for event in events if event["event"] == "ENTRY_ADD")
    assert entry["reason"] == "EMA_PULLBACK"
    assert entry["signal_time"] == ts_ms(index[1])
    assert entry["price"] == bars.iloc[1]["open"]
    assert entry["payload"]
    assert add["signal_time"] == ts_ms(index[2])
    assert add["bar_time"] == ts_ms(index[1])
    assert add["price"] == bars.iloc[1]["close"]
    assert '"add_pct": 50.0' in add["payload"]


def test_tp1_on_entry_candle_keeps_pending_add_when_signal_breakout_confirms():
    index, bars, signals, config = setup_entry()
    bars.loc[index[1], "high"] = 102.0
    config.stop_loss_pct = 0.01
    config.partial_tp_enabled = True
    config.partial_tp_r = 1.0
    config.partial_tp_pct = 50.0

    events = build_events(
        bars, signals, None, None, None, config, "BTCUSDT", "30m"
    )
    exit_event = next(event for event in events if event["event"] == "EXIT")

    assert '"close_pct": 50.0' in exit_event["payload"]
    assert exit_event["signal_time"] == ts_ms(index[1])
    add_event = next(event for event in events if event["event"] == "ENTRY_ADD")
    assert add_event["signal_time"] == ts_ms(index[2])


@pytest.mark.parametrize(
    ("side", "average_entry", "bar1", "bar2"),
    [
        (1, 102.0, (101.0, 103.0, 100.5, 102.0), (101.0, 102.0, 99.0, 100.0)),
        (-1, 98.0, (99.0, 99.5, 97.0, 98.0), (99.0, 101.0, 98.0, 100.0)),
    ],
)
def test_breakeven_stop_uses_first_fill_after_entry_add(side, average_entry, bar1, bar2):
    index, bars, signals, config = setup_entry()
    bars.loc[index[1], ["open", "high", "low", "close"]] = bar1
    bars.loc[index[2], ["open", "high", "low", "close"]] = bar2
    engine = Backtester(bars, config, signals=signals)
    engine.position = Position(
        position_id=1,
        side=side,
        quantity=1.0,
        initial_quantity=1.0,
        entry_price=average_entry,
        entry_time=index[0],
        entry_fee=0.0,
        stop_pct=0.01,
        risk_amount=1.0,
        breakeven=True,
        partial_entry_done=True,
        first_entry_price=100.0,
    )

    assert not engine._stop(1)
    assert engine.position is not None
    assert engine._stop(2)
    assert engine.trades[-1].reason == "BREAKEVEN"
    assert engine.trades[-1].exit_price == pytest.approx(100.0)
