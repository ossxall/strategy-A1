import numpy as np
import pandas as pd
import pytest

from strategy.config import Config
from strategy.indicators import efficiency_ratio
from strategy.strategy import Backtester, EMACrossStrategy


def make_bars(periods=3):
    index = pd.date_range("2026-01-01", periods=periods, freq="30min", tz="UTC")
    close = np.linspace(100.0, 102.0, periods)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 1.0,
        },
        index=index,
    )


def test_entry_mfi_and_er_risk_use_prior_signal_bar():
    bars = make_bars()
    signals = pd.DataFrame(
        {
            "valid": True,
            "target": 0,
            "trend_bias": 0,
            "entry_mfi": [50.0, 70.0, 30.0],
            "entry_er": [0.1, 0.5, 0.9],
        },
        index=bars.index,
    )
    engine = Backtester(bars, Config(timeframe="30min"), signals=signals)
    engine._bar_index = 2  # Entrada a la apertura de la vela 2, señal en la 1.

    assert engine._entry_mfi_risk_multiplier(1) == pytest.approx(1.2)
    assert engine._entry_mfi_risk_multiplier(-1) == pytest.approx(0.8)
    assert engine._entry_er_risk_multiplier() == pytest.approx(1.25)


def test_entry_mfi_and_er_risk_multipliers_are_clamped_and_can_be_disabled():
    bars = make_bars()
    signals = pd.DataFrame(
        {
            "valid": True,
            "target": 0,
            "trend_bias": 0,
            "entry_mfi": [50.0, 0.0, 100.0],
            "entry_er": [0.1, 0.0, 1.0],
        },
        index=bars.index,
    )
    engine = Backtester(bars, Config(timeframe="30min"), signals=signals)
    engine._bar_index = 2
    assert engine._entry_mfi_risk_multiplier(1) == pytest.approx(0.5)
    assert engine._entry_mfi_risk_multiplier(-1) == pytest.approx(1.25)
    assert engine._entry_er_risk_multiplier() == pytest.approx(0.75)

    disabled = Config(
        timeframe="30min",
        entry_mfi_risk_scaling_enabled=False,
        entry_er_risk_scaling_enabled=False,
    )
    no_scale = Backtester(bars, disabled, signals=signals)
    no_scale._bar_index = 2
    assert no_scale._entry_mfi_risk_multiplier(1) == 1.0
    assert no_scale._entry_er_risk_multiplier() == 1.0


def test_entry_sizing_factors_and_quantity_impact_are_saved_on_trade():
    bars = make_bars(2)
    signals = pd.DataFrame(
        {
            "valid": True,
            "target": 0,
            "trend_bias": 0,
            "entry_mfi": [70.0, 50.0],
            "entry_er": [0.5, 0.2],
        },
        index=bars.index,
    )
    config = Config(
        timeframe="30min",
        stop_loss_pct=0.1,
        entry_mfi_risk_scaling_enabled=True,
        entry_er_risk_scaling_enabled=True,
        entry_atr_enabled=False,
        entry_adx_enabled=False,
        streak_trend_only=False,
    )
    engine = Backtester(bars, config, signals=signals)
    engine._bar_index = 1
    engine.open_position(1, 100.0, bars.index[1])
    position = engine.position

    assert position.entry_mfi_multiplier == pytest.approx(1.2)
    assert position.entry_er_multiplier == pytest.approx(1.25)
    assert position.entry_size_multiplier == pytest.approx(1.5)
    assert position.quantity_without_entry_scaling == pytest.approx(10.0)
    assert position.initial_quantity == pytest.approx(15.0)

    engine.close_position(100.0, 100.0, bars.index[1], "TEST")
    trade = engine.trades[0]
    assert trade.position_initial_quantity == pytest.approx(15.0)
    assert trade.quantity_without_entry_scaling == pytest.approx(10.0)
    assert trade.entry_size_multiplier == pytest.approx(1.5)


def test_generated_signals_expose_mfi_and_er_without_filtering_entries():
    bars = make_bars(220)
    enabled = Config(timeframe="30min", streak_trend_only=False)
    disabled = Config(
        timeframe="30min",
        streak_trend_only=False,
        entry_mfi_risk_scaling_enabled=False,
        entry_er_risk_scaling_enabled=False,
    )

    signals_with_scaling = EMACrossStrategy(enabled).generate_signals(bars)
    signals_without_scaling = EMACrossStrategy(disabled).generate_signals(bars)

    assert "entry_mfi" in signals_with_scaling
    assert "entry_er" in signals_with_scaling
    assert "entry_mfi" not in signals_without_scaling
    assert "entry_er" not in signals_without_scaling
    assert np.array_equal(signals_with_scaling.target, signals_without_scaling.target)


def test_efficiency_ratio_separates_directional_and_choppy_paths():
    directional = pd.Series([100.0, 101.0, 102.0, 103.0, 104.0])
    choppy = pd.Series([100.0, 101.0, 100.0, 101.0, 100.0])

    directional_er = efficiency_ratio(directional, period=4)
    choppy_er = efficiency_ratio(choppy, period=4)

    assert directional_er.iloc[-1] == pytest.approx(1.0)
    assert choppy_er.iloc[-1] == pytest.approx(0.0)
