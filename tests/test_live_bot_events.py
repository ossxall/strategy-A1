import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from live import bot as bot_module
from live.bot import (
    SignalBot,
    SignalStore,
    build_events,
    build_strategy_config,
    events_to_trades,
    ts_ms,
)
from strategy.config import Config


def make_bars(periods, *, first_price=100.0):
    index = pd.date_range("2026-01-01", periods=periods, freq="30min", tz="UTC")
    close = np.full(periods, first_price, dtype=float)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.1,
            "low": close - 0.1,
            "close": close,
            "volume": 1.0,
        },
        index=index,
    )


def make_signals(bars, targets):
    return pd.DataFrame(
        {
            "valid": np.ones(len(bars), dtype=bool),
            "target": targets,
            "trend_bias": np.zeros(len(bars), dtype=int),
            "ema_fast": np.full(len(bars), 100.0),
            "ema_slow": np.full(len(bars), 99.0),
            "adx_entry": np.full(len(bars), 30.0),
        },
        index=bars.index,
    )


def make_config(**overrides):
    values = {
        "timeframe": "30min",
        "entry_adx_enabled": False,
        "entry_atr_enabled": False,
        "entry_rsi_divergence_enabled": False,
        "entry_mfi_risk_scaling_enabled": False,
        "entry_er_risk_scaling_enabled": False,
        "adx_exit_enabled": False,
        "ema_weakening_exit_enabled": False,
        "rsi_divergence_exit_enabled": False,
        "ema55_touch_exit_enabled": False,
        "volume_exit_enabled": False,
        "partial_tp_enabled": False,
        "stop_loss_pct": 0.1,
        "monthly_loss_limit_pct": 0,
        "streak_trend_only": False,
        "force_close_at_end": False,
    }
    values.update(overrides)
    return Config(**values)


def test_bot_rejects_terminal_arguments_instead_of_parsing_cli():
    result = subprocess.run(
        [sys.executable, str(Path(bot_module.__file__)), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "no acepta argumentos de terminal" in result.stderr


def test_strategy_parameters_continue_to_load_from_environment(monkeypatch):
    monkeypatch.setenv("STRATEGY_TIMEFRAME", "15min")
    monkeypatch.setenv("STRATEGY_EMA_FAST", "8")
    monkeypatch.setenv("STRATEGY_EMA_SLOW", "21")

    config = build_strategy_config()

    assert config.timeframe == "15min"
    assert config.ema_fast == 8
    assert config.ema_slow == 21


def test_last_closed_bar_entry_is_published_for_next_open_without_waiting_next_close():
    bars = make_bars(2)
    events = build_events(
        bars,
        make_signals(bars, [0, 1]),
        None,
        None,
        None,
        make_config(),
        "BTCUSDT",
        "1m",
    )

    entry = next(event for event in events if event["event"] == "ENTRY")
    payload = json.loads(entry["payload"])

    assert entry["bar_time"] == ts_ms(bars.index[-1])
    assert entry["signal_time"] == ts_ms(bars.index[-1] + pd.Timedelta("30min"))
    assert entry["price"] is None
    assert payload["pending"] is True
    assert payload["action"] == "OPEN"


def test_pending_reversal_publishes_exit_before_entry_at_next_open():
    bars = make_bars(3)
    events = build_events(
        bars,
        make_signals(bars, [1, 0, -1]),
        None,
        None,
        None,
        make_config(),
        "BTCUSDT",
        "1m",
    )
    pending = [event for event in events if json.loads(event["payload"]).get("pending")]

    assert [event["event"] for event in pending] == ["EXIT", "ENTRY"]
    assert {event["signal_time"] for event in pending} == {
        ts_ms(bars.index[-1] + pd.Timedelta("30min"))
    }
    assert json.loads(pending[0]["payload"])["close_pct"] == 100.0
    assert json.loads(pending[1]["payload"])["action"] == "OPEN"


def test_pending_partial_exit_is_published_for_next_open():
    bars = make_bars(3)
    events = build_events(
        bars,
        make_signals(bars, [1, 0, 0]),
        None,
        np.array([False, False, True]),
        None,
        make_config(ema_weakening_exit_pct=50.0),
        "BTCUSDT",
        "1m",
    )

    pending_exit = next(
        event for event in events
        if event["event"] == "EXIT" and json.loads(event["payload"]).get("pending")
    )

    assert pending_exit["bar_time"] == ts_ms(bars.index[-1])
    assert pending_exit["signal_time"] == ts_ms(
        bars.index[-1] + pd.Timedelta("30min")
    )
    assert json.loads(pending_exit["payload"])["close_pct"] == 50.0
    assert pending_exit["price"] is None


def test_monthly_limit_event_uses_the_bar_that_triggered_it():
    bars = make_bars(3)
    bars.iloc[1, bars.columns.get_loc("close")] = 90.0
    bars.iloc[1, bars.columns.get_loc("low")] = 90.0
    config = make_config(
        max_leverage=1.0,
        stop_loss_pct=0.0,
        monthly_loss_limit_pct=5.0,
    )
    events = build_events(
        bars,
        make_signals(bars, [1, 0, 0]),
        None,
        None,
        None,
        config,
        "BTCUSDT",
        "1m",
    )

    monthly_exit = next(event for event in events if event["reason"].startswith("MONTHLY"))

    assert monthly_exit["bar_time"] == ts_ms(bars.index[1])
    assert monthly_exit["signal_time"] == ts_ms(
        bars.index[1] + pd.Timedelta("30min")
    )


def test_signal_store_deduplicates_pending_events_across_restarts(tmp_path):
    event = {
        "symbol": "BTCUSDT",
        "interval": "1m",
        "timeframe": "30min",
        "bar_time": 1_767_225_600_000,
        "signal_time": 1_767_227_400_000,
        "event": "ENTRY",
        "signal": "LONG",
        "side": "LONG",
        "reason": "EMA_CROSS",
        "price": None,
        "ema_fast": 100.0,
        "ema_slow": 99.0,
        "adx_entry": 30.0,
        "executed": 0,
        "created_at": "2026-01-01T00:00:00+00:00",
        "payload": '{"action":"OPEN","pending":true}',
    }
    path = tmp_path / "signal.db"
    store = SignalStore(path)
    store.connect()
    assert store.insert([event]) == 1
    store.close()

    restarted = SignalStore(path)
    restarted.connect()
    try:
        assert restarted.insert([event]) == 0
        row_count = restarted.conn.execute(
            "SELECT COUNT(*) FROM signals"
        ).fetchone()[0]
        assert row_count == 1
    finally:
        restarted.close()


def test_events_to_trades_orders_same_open_by_source_bar_then_exit_priority():
    events = [
        {
            "event": "EXIT",
            "side": "LONG",
            "reason": "STOP_LOSS",
            "signal_time": 200,
            "bar_time": 200,
            "price": 99.0,
            "payload": "{}",
        },
        {
            "event": "ENTRY",
            "side": "LONG",
            "reason": "EMA_CROSS",
            "signal_time": 200,
            "bar_time": 100,
            "price": 100.0,
            "payload": "{}",
        },
        {
            "event": "ENTRY",
            "side": "SHORT",
            "reason": "EMA_CROSS",
            "signal_time": 300,
            "bar_time": 200,
            "price": 100.0,
            "payload": "{}",
        },
        {
            "event": "EXIT",
            "side": "LONG",
            "reason": "EMA_CROSS_EXIT",
            "signal_time": 300,
            "bar_time": 200,
            "price": 101.0,
            "payload": "{}",
        },
    ]

    trades = events_to_trades(events)

    assert [(trade["time"], trade["event"]) for trade in trades] == [
        (200, "ENTRY"),
        (200, "EXIT"),
        (300, "EXIT"),
        (300, "ENTRY"),
    ]


def test_live_only_includes_event_actionable_at_start_even_if_signal_bar_started_before(
    monkeypatch,
):
    bars = make_bars(2)
    boundary = ts_ms(bars.index[-1] + pd.Timedelta("30min"))
    event = {
        "symbol": "BTCUSDT",
        "interval": "1m",
        "timeframe": "30min",
        "bar_time": ts_ms(bars.index[-1]),
        "signal_time": boundary,
        "event": "ENTRY",
        "signal": "LONG",
        "side": "LONG",
        "reason": "EMA_CROSS",
        "price": None,
        "ema_fast": 100.0,
        "ema_slow": 99.0,
        "adx_entry": 30.0,
        "executed": 0,
        "created_at": "2026-01-01T00:00:00+00:00",
        "payload": "{}",
    }

    class Feed:
        def load_closed(self):
            return bars

    class Store:
        inserted = None

        def set_state(self, key, value):
            self.state = (key, value)

        def insert(self, events):
            self.inserted = events
            return len(events)

    store = Store()
    signal_bot = SignalBot(
        make_config(),
        Feed(),
        store,
        "BTCUSDT",
        "1m",
        min_bars=1,
        live_only=True,
        live_since_ms=boundary,
    )
    monkeypatch.setattr(
        signal_bot.strategy,
        "generate_signals",
        lambda frame, raw: make_signals(frame, [0, 1]),
    )
    monkeypatch.setattr(
        signal_bot.strategy,
        "prepare_exits",
        lambda raw, frame: (None, None, None),
    )
    monkeypatch.setattr(bot_module, "build_events", lambda *args: [event])

    assert signal_bot.process_once() == 1
    assert store.inserted == [event]
