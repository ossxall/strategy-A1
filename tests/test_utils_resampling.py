import pandas as pd
import pytest

from strategy.utils import resample_ohlcv


def _bars(index):
    return pd.DataFrame(
        {
            "open": range(100, 100 + len(index)),
            "high": range(101, 101 + len(index)),
            "low": range(99, 99 + len(index)),
            "close": range(100, 100 + len(index)),
            "volume": 1.0,
        },
        index=index,
    )


def test_resample_rejects_non_integer_source_to_target_ratio():
    index = pd.date_range("2026-01-01", periods=4, freq="45min", tz="UTC")

    with pytest.raises(ValueError, match="múltiplo entero"):
        resample_ohlcv(_bars(index), "1h")


def test_resample_rejects_source_candle_that_crosses_target_bucket_close():
    index = pd.date_range(
        "2026-01-01 00:10", periods=4, freq="20min", tz="UTC"
    )

    with pytest.raises(ValueError, match="cruza el cierre del bucket"):
        resample_ohlcv(_bars(index), "1h")
