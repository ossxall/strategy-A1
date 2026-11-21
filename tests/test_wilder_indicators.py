import numpy as np
import pandas as pd
import pytest
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


@pytest.mark.parametrize(
    "missing_fields",
    [("high", "low", "close"), ("close",), ("high",)],
)
def test_adx_and_di_recover_after_a_non_finite_bar(missing_fields):
    rng = np.random.default_rng(12)
    close = 100 + np.cumsum(rng.normal(size=160))
    bars = pd.DataFrame(
        {
            "high": close + rng.uniform(0.2, 1.5, size=len(close)),
            "low": close - rng.uniform(0.2, 1.5, size=len(close)),
            "close": close,
        }
    )
    bars.loc[70, list(missing_fields)] = np.nan

    actual = adx(bars, dilen=14, adxlen=14)
    after_gap = adx(bars.iloc[71:], dilen=14, adxlen=14)

    assert np.isfinite(actual.iloc[69]["adx"])
    assert actual.iloc[70:]["plus_di"].notna().any()
    assert actual.iloc[70:]["minus_di"].notna().any()
    assert actual.iloc[70:]["adx"].notna().any()
    np.testing.assert_allclose(
        actual.iloc[71:].to_numpy(),
        after_gap.to_numpy(),
        atol=1e-12,
        equal_nan=True,
    )
