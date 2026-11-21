import pandas as pd
import pytest

from strategy.utils import load_parquet, normalize_columns, normalize_ohlcv


def _raw_frame(timestamps):
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [1.0] * len(timestamps),
            "high": [2.0] * len(timestamps),
            "low": [0.5] * len(timestamps),
            "close": [1.5] * len(timestamps),
            "volume": [10.0] * len(timestamps),
        }
    )


def test_load_parquet_rejects_unsorted_timestamps(monkeypatch, tmp_path):
    frame = _raw_frame(
        [
            pd.Timestamp("2020-01-01T00:01:00Z"),
            pd.Timestamp("2020-01-01T00:00:00Z"),
        ]
    )
    monkeypatch.setattr("strategy.utils.pd.read_parquet", lambda _path: frame)

    with pytest.raises(ValueError, match="orden cronológico ascendente"):
        load_parquet(tmp_path / "raw.parquet")


def test_load_parquet_rejects_duplicate_timestamps(monkeypatch, tmp_path):
    timestamp = pd.Timestamp("2020-01-01T00:00:00Z")
    frame = _raw_frame([timestamp, timestamp])
    monkeypatch.setattr("strategy.utils.pd.read_parquet", lambda _path: frame)

    with pytest.raises(ValueError, match="timestamps duplicados"):
        load_parquet(tmp_path / "raw.parquet")


def test_normalize_columns_interprets_early_unix_seconds_as_seconds():
    frame = pd.DataFrame(
        {
            "timestamp": [946_684_800],  # 2000-01-01 00:00:00 UTC
            "open": [1.0],
            "high": [2.0],
            "low": [0.5],
            "close": [1.5],
            "volume": [10.0],
        }
    )

    normalized = normalize_columns(frame, timestamp_unit="s")

    assert normalized.index[0] == pd.Timestamp("2000-01-01T00:00:00Z")


@pytest.mark.parametrize("value", [946_684_800, "946684800"])
def test_numeric_epochs_require_an_explicit_unit(value):
    frame = pd.DataFrame(
        {
            "timestamp": [value],
            "open": [1.0],
            "high": [2.0],
            "low": [0.5],
            "close": [1.5],
            "volume": [10.0],
        }
    )

    with pytest.raises(ValueError, match="timestamp_unit es obligatorio"):
        normalize_columns(frame)


def test_numeric_strings_are_parsed_with_the_declared_epoch_unit():
    frame = pd.DataFrame(
        {
            "timestamp": ["946684800"],
            "open": ["1"],
            "high": ["2"],
            "low": ["0.5"],
            "close": ["1.5"],
            "volume": ["10"],
        }
    )

    normalized = normalize_columns(frame, timestamp_unit="s")

    assert normalized.index[0] == pd.Timestamp("2000-01-01T00:00:00Z")
    assert normalized.dtypes.tolist() == [float] * 5


def test_normalizer_rejects_mixed_timestamp_representations():
    frame = pd.DataFrame(
        {
            "timestamp": ["2020-01-01T00:00:00Z", "1577836860000"],
            "open": [1.0, 1.0],
            "high": [2.0, 2.0],
            "low": [0.5, 0.5],
            "close": [1.5, 1.5],
            "volume": [10.0, 10.0],
        }
    )

    with pytest.raises(ValueError, match="mezcla epochs numéricos"):
        normalize_ohlcv(frame, timestamp_unit="ms")


def test_normalizer_returns_a_typed_empty_ohlcv_frame():
    frame = pd.DataFrame(
        columns=["timestamp", "open", "high", "low", "close", "volume"]
    )

    normalized = normalize_ohlcv(frame)

    assert normalized.empty
    assert normalized.index.name == "timestamp"
    assert str(normalized.index.dtype) == "datetime64[ms, UTC]"
    assert normalized.columns.tolist() == [
        "open", "high", "low", "close", "volume"
    ]
    assert normalized.dtypes.tolist() == [float] * 5


@pytest.mark.parametrize(
    "bad_row",
    [
        {"open": 0.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0},
        {"open": 1.0, "high": 0.8, "low": 0.5, "close": 1.5, "volume": 1.0},
        {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": -1.0},
    ],
)
def test_normalizer_rejects_invalid_ohlcv(bad_row):
    frame = pd.DataFrame({"timestamp": ["2020-01-01T00:00:00Z"], **{
        key: [value] for key, value in bad_row.items()
    }})

    with pytest.raises(ValueError):
        normalize_ohlcv(frame)
