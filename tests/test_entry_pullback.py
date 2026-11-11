import numpy as np
import pandas as pd
import pytest

import strategy.strategy as strategy_module
from strategy.config import Config
from strategy.strategy import Backtester, EMACrossStrategy


def make_bars(open_, high, low, close):
    index = pd.date_range("2026-01-01", periods=len(close), freq="30min", tz="UTC")
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": 1.0,
        },
        index=index,
        dtype=float,
    )


def configure_indicators(monkeypatch, bars, fast, slow, macd):
    monkeypatch.setattr(
        strategy_module,
        "ema",
        lambda values, period: pd.Series({55: fast, 200: slow}[period], index=values.index),
    )
    monkeypatch.setattr(
        strategy_module.talib,
        "MACD",
        lambda *args, **kwargs: (np.asarray(macd), np.zeros(len(macd)), np.zeros(len(macd))),
    )


def make_config(**kwargs):
    return Config(
        timeframe="30min",
        macd_timeframe=None,
        confirm_bars=0,
        entry_adx_enabled=False,
        entry_atr_enabled=False,
        entry_rsi_divergence_enabled=False,
        entry_mfi_risk_scaling_enabled=False,
        entry_er_risk_scaling_enabled=False,
        streak_trend_only=False,
        adx_exit_enabled=False,
        ema_weakening_exit_enabled=False,
        rsi_divergence_exit_enabled=False,
        ema55_touch_exit_enabled=False,
        monthly_loss_limit_pct=0,
        force_close_at_end=False,
        **kwargs,
    )


def test_long_pullback_after_ema_cross_enters_on_next_open_and_keeps_50_50_split(monkeypatch):
    bars = make_bars(
        open_=[99.0, 99.0, 100.0, 102.0],
        high=[100.0, 100.5, 101.4, 102.5],
        low=[98.5, 99.3, 100.2, 101.5],
        close=[99.0, 99.5, 101.3, 102.1],
    )
    fast = np.array([99.0, 100.0, 100.0, 100.0])
    slow = np.array([100.0, 99.0, 99.0, 99.0])
    configure_indicators(monkeypatch, bars, fast, slow, [0.0, 1.0, 1.0, 1.0])

    cfg = make_config()
    signals = EMACrossStrategy(cfg).generate_signals(bars)

    # El cruce ocurre en la vela 1; el retroceso/rebote se confirma en la 2.
    assert signals.cross_up.tolist() == [False, True, False, False]
    assert signals.pullback_long.tolist() == [False, False, True, False]
    assert signals.target.tolist() == [0, 0, 1, 0]

    engine = Backtester(bars, cfg, signals=signals)
    engine.run()

    assert len(engine.opened) == 1
    assert engine.opened[0]["time"] == bars.index[3]
    assert engine.opened[0]["price"] == bars.iloc[3]["open"]
    assert engine.opened[0]["entry_pct"] == 50.0
    assert len(engine.adds) == 1
    assert engine.adds[0]["time"] == bars.index[3] + pd.Timedelta("30min")
    assert engine.position is not None
    assert engine.position.quantity == pytest.approx(engine.position.target_quantity)


def test_short_pullback_uses_high_and_rejects_ema_zone(monkeypatch):
    bars = make_bars(
        open_=[101.0, 100.5, 99.5, 99.0],
        high=[101.5, 101.0, 99.8, 98.7],
        low=[100.5, 100.0, 98.5, 98.3],
        close=[101.0, 100.5, 98.7, 98.5],
    )
    fast = np.array([100.5, 100.0, 100.0, 100.0])
    slow = np.array([100.0, 101.0, 101.0, 101.0])
    configure_indicators(monkeypatch, bars, fast, slow, [0.0, -1.0, -1.0, -1.0])

    signals = EMACrossStrategy(make_config(entry_pullback_tolerance_pct=0.2)).generate_signals(bars)

    # El máximo de la vela 2 llega exactamente al límite de 0.2% bajo EMA55.
    assert signals.pullback_short.tolist() == [False, False, True, False]
    assert signals.target.tolist() == [0, 0, -1, 0]


def make_two_crossover_series(force_long_stop=False):
    bars = make_bars(
        open_=[99.0, 100.5, 101.0, 101.5, 101.5, 101.0, 99.0, 98.5],
        high=[99.5, 101.2, 101.8, 102.1, 102.1, 101.5, 99.2, 98.8],
        low=[98.5, 100.3, 101.1, 100.4 if force_long_stop else 101.1,
             101.1, 101.0, 98.2, 98.3],
        close=[99.0, 100.5, 101.5, 102.0, 101.5, 101.0, 98.5, 98.5],
    )
    fast = np.array([99.0, 101.0, 101.0, 101.0, 101.0, 99.0, 99.0, 99.0])
    slow = np.full(8, 100.0)
    return bars, fast, slow, [0.0, 1.0, 1.0, 1.0, 1.0, -1.0, -1.0, -1.0]


def test_only_one_pullback_entry_is_allowed_per_new_ema_cross(monkeypatch):
    bars, fast, slow, macd = make_two_crossover_series()
    configure_indicators(monkeypatch, bars, fast, slow, macd)

    signals = EMACrossStrategy(make_config()).generate_signals(bars)

    # El segundo pullback LONG no reabre; el nuevo cruce bajista rearma la
    # estrategia y permite un único pullback SHORT.
    assert signals.target.tolist() == [0, 0, 1, 0, 0, 0, -1, 0]


def test_opposite_ema_cross_closes_position_but_waits_for_pullback_to_reverse(monkeypatch):
    bars, fast, slow, macd = make_two_crossover_series()
    configure_indicators(monkeypatch, bars, fast, slow, macd)
    cfg = make_config()
    signals = EMACrossStrategy(cfg).generate_signals(bars)

    engine = Backtester(bars, cfg, signals=signals)
    engine.run()

    assert [opened["side"] for opened in engine.opened] == [1, -1]
    assert engine.trades[0].reason == "EMA_CROSS_EXIT"
    assert engine.trades[0].exit_time == bars.index[6]
    assert engine.opened[1]["time"] == bars.index[7]
    assert engine.opened[1]["entry_pct"] == 50.0


def test_stop_closed_position_cannot_reenter_until_a_new_ema_cross(monkeypatch):
    bars, fast, slow, macd = make_two_crossover_series(force_long_stop=True)
    configure_indicators(monkeypatch, bars, fast, slow, macd)
    cfg = make_config()
    signals = EMACrossStrategy(cfg).generate_signals(bars)

    engine = Backtester(bars, cfg, signals=signals)
    engine.run()

    assert len(engine.trades) == 1
    assert engine.trades[0].reason.startswith("STOP_LOSS")
    assert [opened["side"] for opened in engine.opened] == [1, -1]
    assert engine.opened[1]["time"] == bars.index[7]


@pytest.mark.parametrize(
    ("side", "close", "extreme", "fast", "slow", "macd"),
    [
        (1, 100.5, 100.21, 100.0, 99.0, 1.0),  # low queda fuera de la zona LONG
        (-1, 99.5, 99.79, 100.0, 101.0, -1.0),  # high queda fuera de la zona SHORT
    ],
)
def test_pullback_is_rejected_when_wick_misses_the_ema_zone(
    monkeypatch, side, close, extreme, fast, slow, macd
):
    if side == 1:
        bars = make_bars([100, 100], [101, 101], [100.5, extreme], [100.8, close])
    else:
        bars = make_bars([100, 100], [99.5, extreme], [99, 99], [99.2, close])
    configure_indicators(
        monkeypatch,
        bars,
        np.array([fast, fast]),
        np.array([slow, slow]),
        [macd, macd],
    )

    signals = EMACrossStrategy(make_config(entry_pullback_tolerance_pct=0.2)).generate_signals(bars)

    assert signals.target.tolist() == [0, 0]


def test_pullback_tolerance_is_configurable_and_validated():
    assert Config().entry_pullback_tolerance_pct == 0.2
    assert not Config().streak_trend_only
    assert Config(entry_pullback_tolerance_pct=0.0).entry_pullback_tolerance_pct == 0.0
    with pytest.raises(ValueError, match="entry_pullback_tolerance_pct"):
        Config(entry_pullback_tolerance_pct=100.0)
