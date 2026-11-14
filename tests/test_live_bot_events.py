import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from live.bot import (
    SignalBot,
    SignalStore,
    build_events,
    build_strategy_config,
    events_to_trades,
    ts_ms,
)
from live.bot import app as bot_app
from live.bot.storage import build_signal_id
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
        [sys.executable, "-m", "bot", "--help"],
        cwd=Path(bot_app.__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "no acepta argumentos" in result.stderr


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
    assert entry["schema_version"] == 2
    assert entry["action"] == "OPEN"
    assert entry["position_id"] == payload["position_id"]
    assert entry["scheduled"] is True
    assert entry["signal_id"].startswith("sig_v2_")
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
        "position_id": 1,
        "reason": "EMA_CROSS",
        "price": None,
        "ema_fast": 100.0,
        "ema_slow": 99.0,
        "adx_entry": 30.0,
        "executed": 0,
        "created_at": "2026-01-01T00:00:00+00:00",
        "payload": '{"action":"OPEN","position_id":1,"pending":true}',
    }
    path = tmp_path / "signal.db"
    store = SignalStore(path)
    store.connect()
    assert store.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store.conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert store.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000
    assert store.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert store.conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 1_000
    assert store.conn.execute("PRAGMA journal_size_limit").fetchone()[0] == 67_108_864
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert store.insert([event]) == 1
    signal_id, action, position_id, scheduled = store.conn.execute(
        "SELECT signal_id, action, position_id, scheduled FROM signals"
    ).fetchone()
    assert signal_id == build_signal_id(event)
    assert (action, position_id, scheduled) == ("OPEN", 1, 1)
    assert store.conn.execute(
        "SELECT COUNT(*) FROM signal_execution"
    ).fetchone()[0] == 0
    for account_id in ("paper-a", "paper-b"):
        store.conn.execute(
            """
            INSERT INTO signal_execution(account_id, signal_id)
            VALUES (?, ?)
            """,
            (account_id, signal_id),
        )
    store.conn.commit()
    assert store.conn.execute(
        "SELECT COUNT(*) FROM signal_execution WHERE signal_id = ?",
        (signal_id,),
    ).fetchone()[0] == 2
    resolved = {
        **event,
        "scheduled": False,
        "price": 100.25,
        "payload": '{"action":"OPEN","position_id":1}',
    }
    assert store.insert([resolved]) == 0
    stored_price, stored_schedule, stored_payload = store.conn.execute(
        "SELECT price, scheduled, payload FROM signals WHERE signal_id = ?",
        (signal_id,),
    ).fetchone()
    assert stored_price is None
    assert stored_schedule == 1
    assert json.loads(stored_payload)["pending"] is True
    store.close()

    restarted = SignalStore(path)
    restarted.connect()
    try:
        assert restarted.insert([event]) == 0
        row_count, restarted_signal_id = restarted.conn.execute(
            "SELECT COUNT(*), MIN(signal_id) FROM signals"
        ).fetchone()
        assert row_count == 1
        assert restarted_signal_id == signal_id
    finally:
        restarted.close()


def test_signal_store_migrates_existing_rows_to_signal_id(tmp_path):
    event = {
        "symbol": "BTCUSDT",
        "interval": "1m",
        "timeframe": "30min",
        "bar_time": 1_767_225_600_000,
        "signal_time": 1_767_227_400_000,
        "event": "ENTRY",
        "signal": "LONG",
        "side": "LONG",
        "position_id": 1,
        "reason": "EMA_CROSS",
        "price": None,
        "ema_fast": 100.0,
        "ema_slow": 99.0,
        "adx_entry": 30.0,
        "executed": 0,
        "created_at": "2026-01-01T00:00:00+00:00",
        "payload": '{"action":"OPEN","position_id":1,"pending":true}',
    }
    path = tmp_path / "legacy-signal.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            interval TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            bar_time INTEGER NOT NULL,
            signal_time INTEGER NOT NULL,
            event TEXT NOT NULL,
            signal TEXT NOT NULL,
            side TEXT,
            reason TEXT NOT NULL,
            price REAL,
            ema_fast REAL,
            ema_slow REAL,
            adx_entry REAL,
            executed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX ux_signals_event
        ON signals(symbol, interval, timeframe, event, signal, reason, bar_time)
        """
    )
    legacy_event = {key: value for key, value in event.items() if key != "position_id"}
    columns = ", ".join(legacy_event)
    placeholders = ", ".join(f":{column}" for column in legacy_event)
    connection.execute(
        f"INSERT INTO signals ({columns}) VALUES ({placeholders})", legacy_event
    )
    connection.commit()
    connection.close()

    store = SignalStore(path)
    store.connect()
    try:
        row = store.conn.execute(
            "SELECT signal_id, action, position_id, scheduled FROM signals"
        ).fetchone()
        assert row == (build_signal_id(event), "OPEN", 1, 1)
        assert store.insert([event]) == 0
        another_position = {
            **event,
            "position_id": 2,
            "payload": '{"action":"OPEN","position_id":2,"pending":true}',
        }
        assert store.insert([another_position]) == 1
    finally:
        store.close()


def test_signal_store_rekeys_previous_signal_id_schema(tmp_path):
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
        "payload": '{"action":"OPEN","position_id":3,"pending":true}',
    }
    path = tmp_path / "previous-signal.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            interval TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            bar_time INTEGER NOT NULL,
            signal_time INTEGER NOT NULL,
            event TEXT NOT NULL,
            signal TEXT NOT NULL,
            side TEXT,
            reason TEXT NOT NULL,
            price REAL,
            ema_fast REAL,
            ema_slow REAL,
            adx_entry REAL,
            executed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX ux_signals_event
        ON signals(symbol, interval, timeframe, event, signal, reason, bar_time)
        """
    )
    connection.execute(
        "CREATE UNIQUE INDEX ux_signals_signal_id ON signals(signal_id)"
    )
    old_event = {"signal_id": "sig_legacy", **event}
    columns = ", ".join(old_event)
    placeholders = ", ".join(f":{column}" for column in old_event)
    connection.execute(
        f"INSERT INTO signals ({columns}) VALUES ({placeholders})", old_event
    )
    connection.commit()
    connection.close()

    store = SignalStore(path)
    store.connect()
    try:
        row = store.conn.execute(
            """
            SELECT signal_id, schema_version, action, position_id, scheduled
            FROM signals
            """
        ).fetchone()
        assert row == (build_signal_id(event), 2, "OPEN", 3, 1)
    finally:
        store.close()


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
    monkeypatch.setattr(
        bot_app,
        "build_events_and_result",
        lambda *args: ([event], pd.DataFrame(), pd.DataFrame()),
    )

    assert signal_bot.process_once() == 1
    assert store.inserted == [event]


def test_live_backtest_files_refresh_when_bars_change_or_output_is_removed(
    tmp_path, monkeypatch
):
    bars = make_bars(2)
    bot = SignalBot(
        make_config(),
        feed=None,
        store=None,
        symbol="BTCUSDT",
        interval="1m",
        backtest_report_dir=tmp_path,
    )
    calls = []

    def write_report(*args, **kwargs):
        calls.append((args, kwargs))
        html_dir = kwargs["output_dir"] / "html"
        page_dir = html_dir / "data" / "pages"
        page_dir.mkdir(parents=True, exist_ok=True)
        (html_dir / "backtest.html").write_text("visor", encoding="utf-8")
        (page_dir / "2026-01.json").write_text("{}", encoding="utf-8")
        (html_dir / "data" / "manifest.json").write_text(
            '{"pages":[{"file":"pages/2026-01.json"}]}',
            encoding="utf-8",
        )

    monkeypatch.setattr("strategy.report.build_monthly_backtest_htmls", write_report)

    bot._publish_backtest_files(
        bars, bars, make_signals(bars, [0, 0]), None, None, None,
        pd.DataFrame(), pd.DataFrame(),
    )
    bot._publish_backtest_files(
        bars, bars, make_signals(bars, [0, 0]), None, None, None,
        pd.DataFrame(), pd.DataFrame(),
    )
    assert len(calls) == 1
    assert calls[0][1]["output_dir"] == tmp_path

    shutil.rmtree(tmp_path / "html")
    bot._publish_backtest_files(
        bars, bars, make_signals(bars, [0, 0]), None, None, None,
        pd.DataFrame(), pd.DataFrame(),
    )
    assert len(calls) == 2
    assert (tmp_path / "html" / "backtest.html").is_file()

    updated = bars.copy()
    updated.iloc[-1, updated.columns.get_loc("close")] += 1
    bot._publish_backtest_files(
        updated, updated, make_signals(updated, [0, 0]), None, None, None,
        pd.DataFrame(), pd.DataFrame(),
    )
    assert len(calls) == 3
