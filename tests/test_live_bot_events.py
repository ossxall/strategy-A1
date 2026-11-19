import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from live.bot import (
    SignalBot,
    SignalStore,
    build_events,
    build_strategy_config,
    events_to_trades,
    ts_ms,
)
from live.bot import app as bot_app
from live.bot.storage import FeedReader, build_signal_id
from live.feed import database as feed_database
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
        "entry_gaussian_trend_enabled": False,
        "adx_exit_enabled": False,
        "ema_weakening_exit_enabled": False,
        "rsi_divergence_exit_enabled": False,
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


def test_strategy_parameters_ignore_environment_overrides(monkeypatch):
    defaults = Config()
    monkeypatch.setenv("BOT_STRATEGY_TIMEFRAME", "15min")
    monkeypatch.setenv("BOT_STRATEGY_EMA_FAST", "8")
    monkeypatch.setenv("BOT_STRATEGY_EMA_SLOW", "21")
    monkeypatch.setenv("BOT_STRATEGY_INITIAL_CAPITAL", "12345")
    monkeypatch.setenv("BOT_STRATEGY_PARTIAL_TP3_PCT", "10")

    config = build_strategy_config()

    assert config == defaults
    assert config.timeframe == "30min"
    assert config.ema_fast == 55
    assert config.ema_slow == 200
    assert config.initial_capital == defaults.initial_capital
    assert config.partial_tp3_pct == 5.0


def test_backtest_mode_generates_report_without_publishing_signals(
    tmp_path, monkeypatch
):
    raw_index = pd.date_range("2026-01-01", periods=90, freq="1min", tz="UTC")
    bars = pd.DataFrame(
        {
            "open": 100.0,
            "high": 100.1,
            "low": 99.9,
            "close": 100.0,
            "volume": 1.0,
        },
        index=raw_index,
    )
    strategy_bars = bot_app.resample(bars, "30min", 1.0)

    class Feed:
        def load_closed(self):
            return bars

    class Store:
        def insert(self, events):
            raise AssertionError("el backtest no debe publicar eventos live")

    class Engine:
        def __init__(self, *args, **kwargs):
            pass

        def run(self):
            return pd.DataFrame(), pd.DataFrame(
                {
                    "timestamp": strategy_bars.index,
                    "equity": [100.0] * len(strategy_bars),
                }
            )

    calls = []
    monkeypatch.setattr(bot_app, "Backtester", Engine)
    monkeypatch.setattr(
        "strategy.report.generate_report",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    bot = SignalBot(
        make_config(),
        feed=Feed(),
        store=Store(),
        symbol="BTCUSDT",
        interval="1m",
        min_bars=2,
        backtest_report_dir=tmp_path / "backtest_out",
        mode="backtest",
    )
    monkeypatch.setattr(
        bot.strategy,
        "generate_signals",
        lambda frame, raw: make_signals(frame, [0] * len(frame)),
    )
    monkeypatch.setattr(
        bot.strategy,
        "prepare_exits",
        lambda raw, frame: (None, None, None),
    )

    assert bot.process_once() == 0
    assert bot._backtest_complete is True
    assert len(calls) == 1
    assert calls[0][1]["output_dir"] == tmp_path / "backtest_out"
    assert calls[0][0][2] is bot.cfg
    assert calls[0][0][4] == "BTCUSDT 1m feed.db"


def test_live_process_passes_closed_one_minute_feed_to_intrabar_replay(monkeypatch):
    index = pd.date_range("2026-01-01", periods=90, freq="1min", tz="UTC")
    raw = pd.DataFrame(
        {
            "open": np.full(len(index), 100.0),
            "high": np.full(len(index), 100.1),
            "low": np.full(len(index), 99.9),
            "close": np.full(len(index), 100.0),
            "volume": np.ones(len(index)),
        },
        index=index,
    ).drop(index[-1])

    class Feed:
        def load_closed(self):
            return raw

    class Store:
        def insert(self, events):
            return len(events)

    bot = SignalBot(
        make_config(),
        Feed(),
        Store(),
        "BTCUSDT",
        "1m",
        min_bars=2,
    )
    monkeypatch.setattr(
        bot.strategy,
        "generate_signals",
        lambda frame, source: make_signals(frame, [0] * len(frame)),
    )
    monkeypatch.setattr(
        bot.strategy,
        "prepare_exits",
        lambda source, frame: (None, None, None),
    )
    calls = []
    monkeypatch.setattr(
        bot_app,
        "build_events_and_result",
        lambda *args, **kwargs: (
            calls.append(args)
            or ([], pd.DataFrame(), pd.DataFrame())
        ),
    )

    bot.process_once()

    assert calls[0][-2] is raw


def test_live_tail_uses_only_closed_feed_db_minutes(tmp_path, monkeypatch):
    db_path = tmp_path / "feed.db"
    monkeypatch.setattr(feed_database, "DB_PATH", str(db_path))
    feed_database.init_db()

    minute_index = pd.date_range(
        "2026-01-01", periods=61, freq="1min", tz="UTC"
    )
    connection = sqlite3.connect(db_path)
    try:
        connection.executemany(
            """
            INSERT INTO candles (
                symbol, interval, open_time, close_time,
                open, high, low, close, volume, is_closed, source, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "BTCUSDT",
                    "1m",
                    timestamp.value // 1_000_000,
                    timestamp.value // 1_000_000 + 59_999,
                    100.0,
                    100.1,
                    99.9,
                    100.0,
                    1.0,
                    int(index < 60),
                    "test",
                    "test",
                )
                for index, timestamp in enumerate(minute_index)
            ],
        )
        connection.commit()
    finally:
        connection.close()

    reader = FeedReader(db_path, "BTCUSDT", "1m")

    class Store:
        def insert(self, events):
            return len(events)

    bot = SignalBot(
        make_config(timeframe="30min"),
        reader,
        Store(),
        "BTCUSDT",
        "1m",
        min_bars=2,
        mode="live",
    )
    monkeypatch.setattr(
        bot.strategy,
        "generate_signals",
        lambda bars, raw: make_signals(bars, [0] * len(bars)),
    )
    monkeypatch.setattr(
        bot.strategy,
        "prepare_exits",
        lambda raw, bars: (None, None, None),
    )
    calls = []
    monkeypatch.setattr(
        bot_app,
        "build_events_and_result",
        lambda *args, **kwargs: (
            calls.append(args)
            or ([], pd.DataFrame(), pd.DataFrame())
        ),
    )

    try:
        bot.process_once()
    finally:
        reader.close()

    intrabar_bars = calls[0][8]
    assert len(intrabar_bars) == 60
    assert intrabar_bars.index[-1] == minute_index[59]
    assert minute_index[60] not in intrabar_bars.index


def test_bot_rejects_unknown_mode():
    try:
        SignalBot(
            make_config(),
            feed=None,
            store=None,
            symbol="BTCUSDT",
            interval="1m",
            mode="replay",
        )
    except ValueError as exc:
        assert "BOT_MODE" in str(exc)
    else:
        raise AssertionError("el modo desconocido debe rechazarse")


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
    assert entry["schema_version"] == 4
    assert entry["action"] == "OPEN"
    assert entry["position_id"] == payload["position_id"]
    assert entry["scheduled"] is True
    assert entry["signal_id"].startswith("sig_v2_")
    assert payload["pending"] is True
    assert payload["action"] == "OPEN"
    assert payload["reason_code"] == "ENTRY_CROSS"
    assert "max_leverage" not in payload["sizing"]


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
    assert json.loads(pending[0]["payload"])["reason_code"] == "EMA_CROSS_EXIT"
    assert json.loads(pending[0]["payload"])["stop_action"] == "CANCEL"
    assert json.loads(pending[1]["payload"])["action"] == "OPEN"
    assert json.loads(pending[1]["payload"])["reason_code"] == "ENTRY_CROSS"


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
    payload = json.loads(pending_exit["payload"])
    assert payload["close_pct"] == 50.0
    assert payload["reason_code"] == "EMA_WEAKENING"
    assert payload["stop_action"] == "KEEP"
    assert pending_exit["price"] is None


def test_ema_slow_touch_exit_is_published_as_close_confirmed_not_intrabar():
    bars = make_bars(3)
    bars.loc[bars.index[1], ["low", "close"]] = [74.0, 75.0]
    bars.loc[bars.index[2], ["low", "close"]] = [79.0, 81.0]
    signals = make_signals(bars, [-1, 0, 0])
    signals["ema_slow"] = 80.0
    config = make_config(
        partial_tp_enabled=True,
        partial_tp_r=1.0,
        partial_tp_pct=10.0,
        partial_tp2_r=0.0,
        partial_tp2_pct=0.0,
        partial_tp3_r=2.5,
        partial_tp3_pct=10.0,
        partial_tp4_r=0.0,
        partial_tp4_pct=0.0,
        partial_tp5_r=0.0,
        partial_tp5_pct=0.0,
        partial_tp6_r=0.0,
        partial_tp6_pct=0.0,
        partial_tp7_r=0.0,
        partial_tp7_pct=0.0,
        tp3_ema_slow_penetration_pct=0.0,
    )

    events = build_events(
        bars, signals, None, None, None, config, "BTCUSDT", "1m"
    )
    slow_exit = next(
        event for event in events
        if event["event"] == "EXIT"
        and event["reason"] == "EMA_SLOW_TOUCH_SHORT_AFTER_TP3"
    )
    payload = json.loads(slow_exit["payload"])

    assert slow_exit["bar_time"] == ts_ms(bars.index[2])
    assert slow_exit["signal_time"] == ts_ms(
        bars.index[2] + pd.Timedelta("30min")
    )
    assert slow_exit["price"] == bars.close.iloc[2]
    assert "fill_price_is_simulated" not in payload
    assert "simulated_exit_price" not in payload
    assert "pnl_is_simulated" not in payload


@pytest.mark.parametrize(
    ("take_profit_pct", "expected_action", "expected_stop_action"),
    [
        (50.0, "PARTIAL_CLOSE", "MOVE_TO_BREAKEVEN"),
        (100.0, "CLOSE", "CANCEL"),
    ],
)
def test_generated_tp1_signal_has_explicit_stop_policy(
    take_profit_pct, expected_action, expected_stop_action
):
    bars = make_bars(3)
    bars.iloc[2, bars.columns.get_loc("high")] = 101.2
    bars.iloc[2, bars.columns.get_loc("low")] = 101.05
    bars.iloc[2, bars.columns.get_loc("close")] = 101.1
    config = make_config(
        stop_loss_pct=0.01,
        partial_tp_enabled=True,
        partial_tp_r=1.0,
        partial_tp_pct=take_profit_pct,
        partial_tp2_r=0.0,
        partial_tp2_pct=0.0,
        partial_tp3_r=0.0,
        partial_tp3_pct=0.0,
        partial_tp4_r=0.0,
        partial_tp4_pct=0.0,
        partial_tp5_r=0.0,
        partial_tp5_pct=0.0,
        partial_tp6_r=0.0,
        partial_tp6_pct=0.0,
        partial_tp7_r=0.0,
        partial_tp7_pct=0.0,
        move_stop_to_breakeven=True,
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
    entry = next(event for event in events if event["event"] == "ENTRY")
    exit_event = next(event for event in events if event["event"] == "EXIT")
    payload = json.loads(exit_event["payload"])

    assert entry["schema_version"] == 4
    assert json.loads(entry["payload"])["reason_code"] == "ENTRY_CROSS"
    assert exit_event["reason"] == "PARTIAL_TP_1R"
    assert exit_event["action"] == expected_action
    assert payload["reason_code"] == "TAKE_PROFIT_1"
    assert payload["stop_action"] == expected_stop_action


def test_monthly_limit_event_uses_the_bar_that_triggered_it():
    bars = make_bars(3)
    bars.iloc[1, bars.columns.get_loc("close")] = 90.0
    bars.iloc[1, bars.columns.get_loc("low")] = 90.0
    config = make_config(
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
    assert monthly_exit["action"] == "CLOSE"
    monthly_payload = json.loads(monthly_exit["payload"])
    assert monthly_payload["reason_code"] == "MONTHLY_LOSS_LIMIT"
    assert monthly_payload["stop_action"] == "CANCEL"


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
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == 4
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
        upgraded = {
            **event,
            "payload": json.dumps(
                {
                    "action": "OPEN",
                    "position_id": 1,
                    "pending": True,
                    "sizing": {
                        "risk_pct": 0.01,
                        "stop_loss_pct": 0.01,
                    },
                }
            ),
        }
        assert store.insert([upgraded]) == 1
        upgraded_row = store.conn.execute(
            "SELECT schema_version, payload FROM signals"
        ).fetchone()
        assert upgraded_row[0] == 4
        assert "sizing" in json.loads(upgraded_row[1])
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
        assert row == (build_signal_id(event), 1, "OPEN", 3, 1)
    finally:
        store.close()


def test_signal_store_migrates_previous_execution_ledger_columns(tmp_path):
    path = tmp_path / "legacy-execution.db"
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
        CREATE TABLE signal_execution (
            account_id TEXT NOT NULL,
            signal_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'READY',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            lease_owner TEXT,
            lease_until_ms INTEGER,
            client_order_id TEXT,
            exchange_order_id TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(account_id, signal_id)
        )
        """
    )
    connection.commit()
    connection.close()

    store = SignalStore(path)
    store.connect()
    try:
        columns = {
            row[1]
            for row in store.conn.execute("PRAGMA table_info(signal_execution)")
        }
        assert {
            "filled_qty",
            "average_fill_price",
            "order_status",
            "result_json",
            "last_reconciled_at_ms",
        } <= columns
        assert store.conn.execute(
            "SELECT COUNT(*) FROM binance_account_snapshot"
        ).fetchone()[0] == 0
        assert store.conn.execute(
            "SELECT COUNT(*) FROM signal_consumer_state"
        ).fetchone()[0] == 0
    finally:
        store.close()


def test_signal_migration_preserves_parent_unique_index_for_foreign_keys(tmp_path):
    path = tmp_path / "legacy-v2-with-ledger.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id TEXT NOT NULL,
            schema_version INTEGER NOT NULL DEFAULT 2,
            symbol TEXT NOT NULL,
            interval TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            bar_time INTEGER NOT NULL,
            signal_time INTEGER NOT NULL,
            event TEXT NOT NULL,
            action TEXT NOT NULL,
            signal TEXT NOT NULL,
            side TEXT,
            position_id INTEGER,
            scheduled INTEGER NOT NULL DEFAULT 0,
            reason TEXT NOT NULL,
            price REAL,
            ema_fast REAL,
            ema_slow REAL,
            adx_entry REAL,
            executed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}'
        );
        CREATE UNIQUE INDEX ux_signals_signal_id ON signals(signal_id);
        CREATE TABLE signal_execution (
            account_id TEXT NOT NULL,
            signal_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'READY',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            lease_owner TEXT,
            lease_until_ms INTEGER,
            client_order_id TEXT,
            exchange_order_id TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(account_id, signal_id),
            FOREIGN KEY(signal_id) REFERENCES signals(signal_id)
                ON UPDATE CASCADE ON DELETE RESTRICT
        );
        PRAGMA user_version = 2;
        """
    )
    connection.execute(
        """
        INSERT INTO signals(
            signal_id, symbol, interval, timeframe, bar_time, signal_time,
            event, action, signal, side, position_id, reason, created_at, payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "sig_v1_legacy",
            "BTCUSDT",
            "1m",
            "30min",
            100,
            100,
            "ENTRY",
            "OPEN",
            "LONG",
            "LONG",
            7,
            "EMA_CROSS",
            "2026-01-01T00:00:00+00:00",
            '{"action":"OPEN","position_id":7}',
        ),
    )
    connection.execute(
        "INSERT INTO signal_execution(account_id, signal_id) VALUES (?, ?)",
        ("test-account", "sig_v1_legacy"),
    )
    connection.commit()
    connection.close()

    store = SignalStore(path)
    store.connect()
    try:
        signal_id = store.conn.execute(
            "SELECT signal_id FROM signals WHERE id = 1"
        ).fetchone()[0]
        assert signal_id.startswith("sig_v2_")
        assert store.conn.execute(
            "SELECT signal_id FROM signal_execution WHERE account_id = ?",
            ("test-account",),
        ).fetchone()[0] == signal_id
        assert store.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        store.close()


def test_signal_store_opens_existing_database_without_losing_live_state(tmp_path):
    """A database from the deployed v3 schema must survive an application upgrade."""
    path = tmp_path / "deployed-signal.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id TEXT NOT NULL,
            schema_version INTEGER NOT NULL DEFAULT 3,
            symbol TEXT NOT NULL,
            interval TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            bar_time INTEGER NOT NULL,
            signal_time INTEGER NOT NULL,
            event TEXT NOT NULL,
            action TEXT NOT NULL CHECK (action IN ('OPEN', 'CLOSE', 'PARTIAL_CLOSE')),
            signal TEXT NOT NULL,
            side TEXT,
            position_id INTEGER,
            scheduled INTEGER NOT NULL DEFAULT 0 CHECK (scheduled IN (0, 1)),
            reason TEXT NOT NULL,
            price REAL,
            ema_fast REAL,
            ema_slow REAL,
            adx_entry REAL,
            executed INTEGER NOT NULL DEFAULT 0 CHECK (executed IN (0, 1)),
            created_at TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}'
        );
        CREATE UNIQUE INDEX ux_signals_signal_id ON signals(signal_id);
        CREATE TABLE signal_execution (
            account_id TEXT NOT NULL,
            signal_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'READY',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            lease_owner TEXT,
            lease_until_ms INTEGER,
            client_order_id TEXT,
            exchange_order_id TEXT,
            filled_qty REAL NOT NULL DEFAULT 0,
            average_fill_price REAL,
            order_status TEXT,
            result_json TEXT NOT NULL DEFAULT '{}',
            last_reconciled_at_ms INTEGER,
            last_error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (account_id, signal_id),
            FOREIGN KEY (signal_id) REFERENCES signals(signal_id)
                ON UPDATE CASCADE ON DELETE RESTRICT
        );
        CREATE TABLE bot_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE signal_consumer_state (
            account_id TEXT PRIMARY KEY,
            activation_cutoff_ms INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE binance_account_snapshot (
            account_id TEXT PRIMARY KEY,
            environment TEXT NOT NULL,
            observed_at_ms INTEGER NOT NULL,
            wallet_balance REAL,
            available_balance REAL,
            positions_json TEXT NOT NULL DEFAULT '[]',
            open_orders_json TEXT NOT NULL DEFAULT '[]',
            last_error TEXT
        );
        CREATE TABLE execution_adjustments (
            account_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            position_id INTEGER NOT NULL,
            adjustment_id TEXT NOT NULL,
            quantity REAL NOT NULL CHECK (quantity >= 0),
            price REAL,
            reason TEXT NOT NULL,
            observed_at_ms INTEGER NOT NULL,
            PRIMARY KEY (account_id, symbol, position_id, adjustment_id)
        );
        PRAGMA user_version = 3;
        """
    )
    connection.execute(
        """
        INSERT INTO signals (
            signal_id, schema_version, symbol, interval, timeframe, bar_time,
            signal_time, event, action, signal, side, position_id, scheduled,
            reason, executed, created_at, payload
        ) VALUES (
            'persisted-signal', 3, 'BTCUSDT', '1m', '30min', 1000, 1000,
            'ENTRY', 'OPEN', 'LONG', 'LONG', 17, 0, 'EMA_CROSS', 0,
            '2026-01-01T00:00:00+00:00', '{"action":"OPEN","position_id":17}'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO signal_execution (
            account_id, signal_id, status, attempt_count, filled_qty,
            average_fill_price, order_status, result_json
        ) VALUES (
            'production', 'persisted-signal', 'FILLED', 1, 2.5, 100.25,
            'FILLED', '{"exchange":"binance"}'
        )
        """
    )
    connection.execute(
        "INSERT INTO bot_state(key, value) VALUES ('live_since_ms', '123456')"
    )
    connection.execute(
        """
        INSERT INTO signal_consumer_state(account_id, activation_cutoff_ms)
        VALUES ('production', 900)
        """
    )
    connection.execute(
        """
        INSERT INTO binance_account_snapshot(
            account_id, environment, observed_at_ms, wallet_balance,
            available_balance, positions_json, open_orders_json
        ) VALUES ('production', 'live', 1200, 500, 450, '[{"symbol":"BTCUSDT"}]', '[]')
        """
    )
    connection.execute(
        """
        INSERT INTO execution_adjustments(
            account_id, symbol, position_id, adjustment_id, quantity,
            price, reason, observed_at_ms
        ) VALUES ('production', 'BTCUSDT', 17, 'adjustment-1', 0.25, 100,
                  'manual-close', 1300)
        """
    )
    connection.commit()
    connection.close()

    store = SignalStore(path)
    store.connect()
    try:
        signal = store.list_due_signals(now_ms=2_000)
        assert len(signal) == 1
        assert signal[0]["signal_id"] == "persisted-signal"
        assert signal[0]["position_id"] == 17

        assert store.get_execution_state(
            "production", "persisted-signal"
        ) == {
            "status": "FILLED",
            "client_order_id": None,
            "order_status": "FILLED",
            "result": {"exchange": "binance"},
            "filled_qty": 2.5,
            "average_fill_price": 100.25,
            "error": None,
        }
        assert store.get_state("live_since_ms") == "123456"
        assert store.initialize_signal_consumer(
            "production", now_ms=100_000, initial_backfill_ms=50_000
        ) == 900
        assert store.get_active_execution_positions(
            "production", "BTCUSDT"
        ) == [
            {
                "position_id": 17,
                "side": "LONG",
                "entered_qty": 2.5,
                "exited_qty": 0.0,
                "adjusted_qty": 0.25,
                "open_qty": 2.25,
                "has_entry": True,
            }
        ]
        assert store.conn.execute(
            "SELECT wallet_balance FROM binance_account_snapshot "
            "WHERE account_id='production'"
        ).fetchone()[0] == 500
        assert store.conn.execute(
            "SELECT COUNT(*) FROM execution_adjustments"
        ).fetchone()[0] == 1
        assert store.conn.execute("PRAGMA user_version").fetchone()[0] == 4
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
    raw_index = pd.date_range("2026-01-01", periods=60, freq="1min", tz="UTC")
    raw = pd.DataFrame(
        {
            "open": 100.0,
            "high": 100.1,
            "low": 99.9,
            "close": 100.0,
            "volume": 1.0,
        },
        index=raw_index,
    )
    bars = bot_app.resample(raw, "30min", 1.0)
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
            return raw

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
        lambda *args, **kwargs: ([event], pd.DataFrame(), pd.DataFrame()),
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
