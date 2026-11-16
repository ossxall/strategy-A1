import sqlite3

import pytest

from live.bot.storage import FeedReader, SignalStore
from live.db.sqlite_config import SQLiteSchemaError
from live.feed import database as feed_database


def test_feed_refuses_incompatible_database_without_filling_missing_tables(
    tmp_path, monkeypatch
):
    path = tmp_path / "feed.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE candles (open_time TEXT)")
    connection.execute(
        "CREATE TABLE collector_state "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(feed_database, "DB_PATH", str(path))

    with pytest.raises(SQLiteSchemaError, match="esquema incompatible"):
        feed_database.init_db()

    connection = sqlite3.connect(path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert tables == {"candles", "collector_state"}
    finally:
        connection.close()


def test_bot_feed_reader_refuses_incompatible_feed_schema(tmp_path):
    path = tmp_path / "feed.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE candles (open_time TEXT)")
    connection.execute(
        "CREATE TABLE collector_state "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    connection.commit()
    connection.close()

    reader = FeedReader(path, "BTCUSDT", "1m")
    with pytest.raises(SQLiteSchemaError, match="esquema incompatible"):
        reader.connect()
    assert reader.conn is None


def test_signal_store_refuses_unrecognized_database_without_replacing_it(tmp_path):
    path = tmp_path / "signal.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE signals (legacy_payload BLOB)")
    connection.commit()
    connection.close()

    store = SignalStore(path)
    with pytest.raises(SQLiteSchemaError, match="faltan columnas"):
        store.connect()
    assert store.conn is None

    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall() == [("signals",)]
        assert [
            row[1] for row in connection.execute("PRAGMA table_info(signals)")
        ] == ["legacy_payload"]
    finally:
        connection.close()


def test_signal_store_refuses_misdefined_existing_index(tmp_path):
    path = tmp_path / "signal.db"
    initial = SignalStore(path)
    initial.connect()
    initial.close()

    connection = sqlite3.connect(path)
    connection.execute("DROP INDEX ux_signals_signal_id")
    connection.execute(
        "CREATE INDEX ux_signals_signal_id ON signals(symbol)"
    )
    connection.commit()
    connection.close()

    store = SignalStore(path)
    with pytest.raises(SQLiteSchemaError, match="unique=False"):
        store.connect()
    assert store.conn is None


def test_signal_store_refuses_database_from_newer_schema_version(tmp_path):
    path = tmp_path / "signal.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 999")
    connection.commit()
    connection.close()

    with pytest.raises(SQLiteSchemaError, match="esquema futuro 999"):
        SignalStore(path).connect()
