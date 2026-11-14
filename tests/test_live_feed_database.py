from live.feed import database


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
