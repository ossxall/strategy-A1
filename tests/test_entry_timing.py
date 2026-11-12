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


def test_entry_opens_full_position_at_next_open_without_later_add():
    index, bars, signals, config = setup_entry()
    engine = Backtester(bars, config, signals=signals)
    engine.run()

    assert len(engine.opened) == 1
    assert engine.opened[0]["time"] == index[1]
    assert engine.opened[0]["price"] == bars.iloc[1]["open"]
    assert engine.position is not None
    assert engine.position.entry_price == bars.iloc[1]["open"]
    assert engine.position.quantity == engine.position.initial_quantity

    events = build_events(bars, signals, None, None, None, config, "BTCUSDT", "30m")
    assert [event["event"] for event in events].count("ENTRY") == 1
    assert not any(event["event"] == "ENTRY_ADD" for event in events)


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


def test_stop_loss_closes_position():
    index, bars, signals, config = setup_entry()
    signals.loc[index[0], "target"] = 1
    bars.loc[index[1], ["open", "high", "low", "close"]] = [100.0, 100.4, 99.6, 100.0]
    bars.loc[index[2], ["open", "high", "low", "close"]] = [100.0, 101.0, 89.0, 101.0]

    engine = Backtester(bars, config, signals=signals)
    engine.run()

    assert engine.position is None
    assert engine.trades[-1].reason.startswith("STOP_LOSS")


def test_live_entry_event_has_no_add_event():
    index, bars, signals, config = setup_entry()
    events = build_events(bars, signals, None, None, None, config, "BTCUSDT", "30m")

    entry = next(event for event in events if event["event"] == "ENTRY")
    assert entry["reason"] == "EMA_PULLBACK"
    assert entry["signal_time"] == ts_ms(index[1])
    assert entry["price"] == bars.iloc[1]["open"]
    assert '"action": "OPEN"' in entry["payload"]
    assert not any(event["event"] == "ENTRY_ADD" for event in events)


def test_partial_take_profit_still_works_on_full_entry():
    index, bars, signals, config = setup_entry()
    bars.loc[index[1], "high"] = 102.0
    config.stop_loss_pct = 0.01
    config.partial_tp_enabled = True
    config.partial_tp_r = 1.0
    config.partial_tp_pct = 50.0

    events = build_events(bars, signals, None, None, None, config, "BTCUSDT", "30m")
    exit_event = next(event for event in events if event["event"] == "EXIT")

    assert '"close_pct": 50.0' in exit_event["payload"]
    assert exit_event["signal_time"] == ts_ms(index[1])


@pytest.mark.parametrize(
    ("side", "bar1", "bar2"),
    [
        (1, (101.0, 103.0, 100.5, 102.0), (101.0, 102.0, 99.0, 100.0)),
        (-1, (99.0, 99.5, 97.0, 98.0), (99.0, 101.0, 98.0, 100.0)),
    ],
)
def test_breakeven_stop_uses_entry_price(side, bar1, bar2):
    index, bars, signals, config = setup_entry()
    bars.loc[index[1], ["open", "high", "low", "close"]] = bar1
    bars.loc[index[2], ["open", "high", "low", "close"]] = bar2
    engine = Backtester(bars, config, signals=signals)
    engine.position = Position(
        position_id=1,
        side=side,
        quantity=1.0,
        initial_quantity=1.0,
        entry_price=100.0,
        entry_time=index[0],
        entry_fee=0.0,
        stop_pct=0.01,
        risk_amount=1.0,
        breakeven=True,
    )

    assert not engine._stop(1)
    assert engine.position is not None
    assert engine._stop(2)
    assert engine.trades[-1].reason == "BREAKEVEN"
    assert engine.trades[-1].exit_price == pytest.approx(100.0)
