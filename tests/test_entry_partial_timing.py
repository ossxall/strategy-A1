import pandas as pd

from live.bot import build_events, ts_ms
from strategy.config import Config
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


def test_entry_is_split_between_consecutive_opens():
    index, bars, signals, config = setup_entry()
    engine = Backtester(bars, config, signals=signals)
    engine.run()

    assert len(engine.opened) == 1
    assert engine.opened[0]["time"] == index[1]
    assert engine.opened[0]["entry_pct"] == 50.0
    assert len(engine.adds) == 1
    assert engine.adds[0]["time"] == index[2]
    assert engine.adds[0]["price"] == bars.iloc[2]["open"]
    assert engine.position is not None
    assert engine.position.quantity == engine.position.target_quantity
    assert engine.position.entry_price == 102.0


def test_live_event_reports_add_at_next_candle_open():
    index, bars, signals, config = setup_entry()
    events = build_events(
        bars, signals, None, None, None, config, "BTCUSDT", "30m"
    )

    entry = next(event for event in events if event["event"] == "ENTRY")
    add = next(event for event in events if event["event"] == "ENTRY_ADD")
    assert entry["signal_time"] == ts_ms(index[1])
    assert entry["payload"]
    assert add["signal_time"] == ts_ms(index[2])
    assert add["bar_time"] == ts_ms(index[1])
    assert add["price"] == bars.iloc[2]["open"]
    assert '"add_pct": 50.0' in add["payload"]


def test_live_event_schedules_pending_add_for_next_open():
    index, bars, signals, config = setup_entry()
    events = build_events(
        bars.iloc[:2], signals.iloc[:2], None, None, None, config, "BTCUSDT", "30m"
    )

    add = next(event for event in events if event["event"] == "ENTRY_ADD")
    assert add["signal_time"] == ts_ms(index[2])
    assert add["bar_time"] == ts_ms(index[1])
    assert add["price"] is None


def test_tp1_on_entry_candle_cancels_pending_add_after_moving_stop_to_breakeven():
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
    assert not any(event["event"] == "ENTRY_ADD" for event in events)


def test_red_long_add_candle_reverts_only_the_second_tranche():
    index, bars, signals, config = setup_entry()
    bars.loc[index[2], ["open", "high", "low", "close"]] = [104.0, 104.5, 102.5, 103.0]
    engine = Backtester(bars, config, signals=signals)
    engine.run()

    assert len(engine.trades) == 1
    assert engine.trades[0].reason == "ENTRY_ADD_REVERT_RED_CANDLE"
    assert engine.trades[0].exit_time == index[2] + pd.Timedelta(config.timeframe)
    assert engine.position is not None
    assert engine.position.quantity == engine.position.target_quantity / 2
    assert engine.position.entry_added_quantity == 0.0

    events = build_events(bars, signals, None, None, None, config, "BTCUSDT", "30m")
    reversal = next(
        event for event in events
        if event["reason"] == "ENTRY_ADD_REVERT_RED_CANDLE"
    )
    assert reversal["signal_time"] == ts_ms(index[2] + pd.Timedelta(config.timeframe))
    assert '"close_pct": 50.0' in reversal["payload"]


def test_green_short_add_candle_reverts_only_the_second_tranche():
    index, bars, signals, config = setup_entry()
    signals["target"] = [ -1, 0, 0]
    signals["trend_bias"] = [-1, -1, -1]
    bars.loc[index[2], ["open", "high", "low", "close"]] = [96.0, 97.5, 95.5, 97.0]
    engine = Backtester(bars, config, signals=signals)
    engine.run()

    assert len(engine.trades) == 1
    assert engine.trades[0].reason == "ENTRY_ADD_REVERT_GREEN_CANDLE"
    assert engine.position is not None
    assert engine.position.quantity == engine.position.target_quantity / 2
