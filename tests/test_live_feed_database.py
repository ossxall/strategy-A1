from live.feed import database


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
