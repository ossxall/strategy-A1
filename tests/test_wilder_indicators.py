import numpy as np
import pandas as pd
import talib

from strategy.indicators import adx, atr, rsi


def test_rsi_and_adx_use_wilder_seed_and_match_talib():
    rng = np.random.default_rng(4)
    close = 100 + np.cumsum(rng.normal(size=200))
    high = close + rng.uniform(0, 2, size=len(close))
    low = close - rng.uniform(0, 2, size=len(close))
    bars = pd.DataFrame({"high": high, "low": low, "close": close})

    actual_rsi = rsi(bars["close"], length=14).to_numpy()
    expected_rsi = talib.RSI(close, timeperiod=14)
    actual_adx = adx(bars, dilen=14, adxlen=14)

    assert np.flatnonzero(np.isfinite(actual_rsi))[0] == 14
    np.testing.assert_allclose(actual_rsi, expected_rsi, atol=1e-12, equal_nan=True)
    np.testing.assert_allclose(
        actual_adx["adx"].to_numpy(),
        talib.ADX(high, low, close, timeperiod=14),
        atol=1e-12,
        equal_nan=True,
    )
    np.testing.assert_allclose(
        actual_adx["plus_di"].to_numpy(),
        talib.PLUS_DI(high, low, close, timeperiod=14),
        atol=1e-12,
        equal_nan=True,
    )
    np.testing.assert_allclose(
        actual_adx["minus_di"].to_numpy(),
        talib.MINUS_DI(high, low, close, timeperiod=14),
        atol=1e-12,
        equal_nan=True,
    )


def test_atr_seeds_from_a_full_true_range_window():
    bars = pd.DataFrame(
        {
            "high": [11.0, 12.0, 13.0, 14.0],
            "low": [9.0, 8.0, 7.0, 6.0],
            "close": [10.0, 10.0, 10.0, 10.0],
        }
    )

    np.testing.assert_allclose(
        atr(bars, length=2).to_numpy(),
        [np.nan, 3.0, 4.5, 6.25],
        equal_nan=True,
    )


def test_flat_rsi_is_neutral_only_after_its_seed_window():
    values = pd.Series(np.full(20, 100.0))

    actual = rsi(values, length=14)

    assert actual.iloc[:14].isna().all()
    np.testing.assert_allclose(actual.iloc[14:].to_numpy(), 50.0)
