import sqlite3

from live.feed import database


def test_feed_init_upgrades_existing_database_without_losing_candles_or_state(
    tmp_path, monkeypatch
):
    """A deployed feed.db must remain usable after code/schema changes."""
    db_path = tmp_path / "feed.db"
    open_time = 1_700_000_000_000
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE candles (
            symbol TEXT NOT NULL,
            interval TEXT NOT NULL,
            open_time INTEGER NOT NULL,
            close_time INTEGER NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL,
            quote_volume REAL NOT NULL DEFAULT 0,
            trades INTEGER NOT NULL DEFAULT 0,
            taker_buy_base REAL NOT NULL DEFAULT 0,
            taker_buy_quote REAL NOT NULL DEFAULT 0,
            is_closed INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'rest',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (symbol, interval, open_time)
        );
        CREATE INDEX idx_candles_time
        ON candles(symbol, interval, open_time);
        CREATE TABLE collector_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    connection.execute(
        """
        INSERT INTO candles (
            symbol, interval, open_time, close_time, open, high, low, close,
            volume, quote_volume, trades, taker_buy_base, taker_buy_quote,
            is_closed, source, updated_at
        ) VALUES (
            'BTCUSDT', '1m', ?, ?, 100, 102, 99, 101, 5, 505, 8, 2, 202,
            1, 'rest', 'before-upgrade'
        )
        """,
        (open_time, open_time + 59_999),
    )
    connection.execute(
        """
        INSERT INTO collector_state(key, value, updated_at)
        VALUES ('collector_status', 'running', 'before-upgrade')
        """
    )
    connection.commit()
    connection.close()

    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    database.init_db()

    # Existing rows remain queryable, and new writes can still update them.
    assert database.get_latest_open_time() == open_time
    database.save_candle(
        {
            "open_time": open_time,
            "close_time": open_time + 59_999,
            "open": 100,
            "high": 103,
            "low": 99,
            "close": 102,
            "volume": 6,
            "quote_volume": 606,
            "trades": 9,
            "taker_buy_base": 3,
            "taker_buy_quote": 303,
            "is_closed": 1,
            "source": "rest",
        }
    )

    connection = database.db_connect()
    try:
        row = connection.execute(
            """
            SELECT open, high, close, volume, is_closed
            FROM candles
            WHERE symbol='BTCUSDT' AND interval='1m' AND open_time=?
            """,
            (open_time,),
        ).fetchone()
        assert row == (100.0, 103.0, 102.0, 6.0, 1)
        assert connection.execute(
            "SELECT value FROM collector_state WHERE key='collector_status'"
        ).fetchone()[0] == "running"
        assert connection.execute(
            "SELECT COUNT(*) FROM candles"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def _seed_candles(database_path, monkeypatch, open_times):
    monkeypatch.setattr(database, "DB_PATH", str(database_path))
    database.init_db()
    connection = database.db_connect()
    try:
        for open_time in open_times:
            connection.execute(
                """
                INSERT INTO candles (
                    symbol, interval, open_time, close_time,
                    open, high, low, close, volume, updated_at
                )
                VALUES (?, ?, ?, ?, 1, 1, 1, 1, 1, 'test')
                """,
                ("BTCUSDT", "1m", open_time, open_time + 59_999),
            )
        connection.commit()
    finally:
        connection.close()


def test_feed_database_uses_recommended_sqlite_settings(tmp_path, monkeypatch):
    db_path = tmp_path / "feed.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_path))

    database.init_db()
    connection = database.db_connect()
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 1_000
        assert (
            connection.execute("PRAGMA journal_size_limit").fetchone()[0]
            == 67_108_864
        )
    finally:
        connection.close()


def test_feed_finds_missing_prefix_from_configured_start_date(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "feed.db"
    start = 1_700_000_000_000
    interval = 60_000
    _seed_candles(db_path, monkeypatch, [start + 2 * interval, start + 3 * interval])

    assert database.get_first_missing_candle_range(start) == (
        start,
        start + interval,
    )


def test_feed_resumes_first_historical_gap(tmp_path, monkeypatch):
    db_path = tmp_path / "feed.db"
    start = 1_700_000_000_000
    interval = 60_000
    _seed_candles(
        db_path,
        monkeypatch,
        [start, start + interval, start + 3 * interval, start + 4 * interval],
    )

    assert database.get_first_missing_candle_range(start) == (
        start + 2 * interval,
        start + 2 * interval,
    )


def test_feed_returns_no_gap_when_history_is_contiguous(tmp_path, monkeypatch):
    db_path = tmp_path / "feed.db"
    start = 1_700_000_000_000
    interval = 60_000
    _seed_candles(
        db_path,
        monkeypatch,
        [start, start + interval, start + 2 * interval],
    )

    assert database.get_first_missing_candle_range(start) is None
