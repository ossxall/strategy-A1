import numpy as np

from strategy.indicators import (
    adx_reversal_flags,
    confirmed_crosses,
    ema_weakening_flags,
    rsi_divergence_flags,
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


def test_confirmed_cross_waits_for_all_configured_bars_and_rejects_failed_cross():
    cross = np.array([True, False, False, True, False, False])
    side_ok = np.array([True, True, True, True, False, True])

    confirmed = confirmed_crosses(cross, side_ok, bars=2)

    assert confirmed.tolist() == [False, False, True, False, False, False]


def test_adx_reversal_requires_a_peak_above_key_level():
    values = np.array([20.0, 24.0, 23.0, 22.0, 25.0, 25.0, 24.0])

    flags = adx_reversal_flags(values, key_level=23.0)

    assert flags.tolist() == [False, False, True, False, False, False, False]


def test_ema_weakening_fires_only_when_a_separated_pair_converges():
    slow = np.full(4, 100.0)
    fast = np.array([103.0, 102.0, 100.9, 100.5])

    flags = ema_weakening_flags(fast, slow, gap_pct=0.01)

    assert flags.tolist() == [False, False, True, False]
