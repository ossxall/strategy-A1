import csv
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from strategy.utils import load_parquet

pyarrow = pytest.importorskip("pyarrow")
pytest.importorskip("pyarrow.parquet")

SCRIPT_PATH = Path(__file__).parents[1] / "dataset" / "binance_klines.py"
SPEC = importlib.util.spec_from_file_location("binance_klines", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
binance_klines = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = binance_klines
SPEC.loader.exec_module(binance_klines)

COLUMNS = binance_klines.COLUMNS
merge_parquet = binance_klines.merge_parquet
ts_to_iso = binance_klines.ts_to_iso


def _write_csv(path: Path, open_time: int, close_time: int) -> None:
    row = {
        "open_time": open_time,
        "open": 1,
        "high": 2,
        "low": 0.5,
        "close": 1.5,
        "volume": 10,
        "close_time": close_time,
        "quote_volume": 15,
        "count": 2,
        "taker_buy_volume": 4,
        "taker_buy_quote_volume": 6,
        "ignore": 0,
    }
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerow(row)


def test_merge_parquet_normalizes_millisecond_and_microsecond_spot_timestamps(tmp_path):
    old_csv = tmp_path / "old.csv"
    new_csv = tmp_path / "new.csv"
    _write_csv(old_csv, 1_577_836_800_000, 1_577_836_859_999)
    _write_csv(new_csv, 1_735_689_600_000_000, 1_735_689_659_999_999)
    output = tmp_path / "merged.parquet"

    assert merge_parquet([old_csv, new_csv], output) == 2

    table = pyarrow.parquet.read_table(output)
    assert table.column_names == [
        "timestamp", "open", "high", "low", "close", "volume"
    ]
    assert table.schema.field("timestamp").type.tz == "UTC"
    assert table.schema.field("timestamp").type.unit == "ms"
    assert table["timestamp"].to_pylist() == [
        datetime(2020, 1, 1, tzinfo=timezone.utc),
        datetime(2025, 1, 1, tzinfo=timezone.utc),
    ]
    assert all(
        table.schema.field(column).type == pyarrow.float64()
        for column in ("open", "high", "low", "close", "volume")
    )
    business_frame = load_parquet(output)
    assert business_frame.index.tolist() == [
        pd.Timestamp("2020-01-01T00:00:00Z"),
        pd.Timestamp("2025-01-01T00:00:00Z"),
    ]
    assert business_frame.columns.tolist() == [
        "open", "high", "low", "close", "volume"
    ]
    assert str(business_frame.index.dtype) == "datetime64[ms, UTC]"


def test_ts_to_iso_accepts_microsecond_timestamps():
    assert ts_to_iso(1_735_689_600_000_000) == "2025-01-01T00:00:00.000Z"
