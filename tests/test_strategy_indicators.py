import numpy as np

from strategy.indicators import (
    adx_reversal_flags,
    confirmed_crosses,
    rsi_divergence_flags,
    rsi_divergence_signals,
)


def test_bearish_rsi_divergence_is_emitted_only_after_pivot_confirmation():
    high = np.array([1.0, 3.0, 1.0, 2.0, 4.0, 2.0])
    low = np.zeros(len(high))
    rsi = np.array([50.0, 70.0, 50.0, 50.0, 60.0, 50.0])

    flags = rsi_divergence_flags(high, low, rsi, pivot=1, lookback=10)

    assert flags.tolist() == [0, 0, 0, 0, 0, -1]
    # Hasta el cierre de la vela que confirma el segundo pivote no hay señal.
    assert not flags[:-1].any()


def test_bullish_rsi_divergence_and_lookback_boundary():
    high = np.full(6, 10.0)
    low = np.array([3.0, 1.0, 3.0, 2.0, 0.0, 2.0])
    rsi = np.array([50.0, 30.0, 50.0, 50.0, 40.0, 50.0])

    flags = rsi_divergence_flags(high, low, rsi, pivot=1, lookback=3)
    outside_lookback = rsi_divergence_flags(
        high, low, rsi, pivot=1, lookback=2
    )

    assert flags.tolist() == [0, 0, 0, 0, 0, 1]
    assert not outside_lookback.any()


def test_bullish_and_bearish_divergences_can_coincide():
    high = np.array([3.0, 5.0, 4.0, 3.0, 5.0, 7.0, 6.0])
    low = np.array([4.0, 5.0, 4.0, 3.0, 4.0, 2.0, 3.0])
    rsi = np.array([50.0, 70.0, 50.0, 40.0, 50.0, 60.0, 50.0])

    signals = rsi_divergence_signals(high, low, rsi, pivot=1, lookback=10)

    assert signals.bullish.tolist() == [False, False, False, False, False, False, True]
    assert signals.bearish.tolist() == [False, False, False, False, False, False, True]
    # La API firmada antigua sigue priorizando la bajista.
    signed = rsi_divergence_flags(high, low, rsi, pivot=1, lookback=10)
    assert signed.tolist() == [0, 0, 0, 0, 0, 0, -1]


def test_confirmed_cross_waits_for_all_configured_bars_and_rejects_failed_cross():
    cross = np.array([True, False, False, True, False, False])
    side_ok = np.array([True, True, True, True, False, True])

    confirmed = confirmed_crosses(cross, side_ok, bars=2)

    assert confirmed.tolist() == [False, False, True, False, False, False]


def test_adx_reversal_requires_a_peak_above_key_level():
    values = np.array([20.0, 24.0, 23.0, 22.0, 25.0, 25.0, 24.0])

    flags = adx_reversal_flags(values, key_level=23.0)

    assert flags.tolist() == [False, False, True, False, False, False, False]
