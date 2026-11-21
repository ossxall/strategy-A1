import numpy as np
import pytest

from live.feed.candles import normalize_candle
from strategy.ohlcv import normalize_ohlcv_values


def test_live_candle_and_vector_ingestion_share_the_same_ohlcv_values():
    source_rows = [
        (100, 102, 99, 101, 5),
        (101, 103, 100, 102, 7),
    ]
    vectors = normalize_ohlcv_values(
        *(np.asarray(column) for column in zip(*source_rows))
    )
    live_rows = [
        normalize_candle(
            open_time=index * 60_000,
            close_time=index * 60_000 + 59_999,
            open_price=row[0],
            high=row[1],
            low=row[2],
            close_price=row[3],
            volume=row[4],
        )
        for index, row in enumerate(source_rows)
    ]

    for field, values in zip(
        ("open", "high", "low", "close", "volume"), vectors
    ):
        assert [row[field] for row in live_rows] == values.tolist()
        assert all(type(row[field]) is float for row in live_rows)


@pytest.mark.parametrize(
    "values",
    [
        (100, 99, 98, 101, 5),  # high below close
        (100, 102, 99, 101, -1),  # negative volume
        (0, 102, 99, 101, 5),  # non-positive price
    ],
)
def test_live_and_vector_ingestion_reject_the_same_invalid_ohlcv(values):
    with pytest.raises(ValueError):
        normalize_ohlcv_values(*values)
    with pytest.raises(ValueError):
        normalize_candle(
            open_time=0,
            close_time=59_999,
            open_price=values[0],
            high=values[1],
            low=values[2],
            close_price=values[3],
            volume=values[4],
        )
