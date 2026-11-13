import numpy as np
import pandas as pd

import strategy.strategy as strategy_module
from strategy.config import Config
from strategy.strategy import EMACrossStrategy


def make_bars(closes):
    closes = np.asarray(closes, dtype=float)
    index = pd.date_range("2026-01-01", periods=len(closes), freq="30min", tz="UTC")
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes + 1.0,
            "low": closes - 1.0,
            "close": closes,
            "volume": 1.0,
        },
        index=index,
    )


def configure_crosses(monkeypatch, fast, slow, macd_line):
    """Fija EMAs y MACD sintéticos para aislar el filtro de precio."""
    ema_values = {
        55: np.asarray(fast, dtype=float),
        200: np.asarray(slow, dtype=float),
    }
    monkeypatch.setattr(
        strategy_module,
        "ema",
        lambda values, period: pd.Series(ema_values[period], index=values.index),
    )
    line = np.asarray(macd_line, dtype=float)
    monkeypatch.setattr(
        strategy_module.talib,
        "MACD",
        lambda *args, **kwargs: (line, np.zeros(len(line)), np.zeros(len(line))),
    )


def make_config(**overrides):
    values = {
        "timeframe": "30min",
        "macd_timeframe": None,
        "confirm_bars": 0,
        "entry_adx_enabled": False,
        "entry_atr_enabled": False,
        "entry_mfi_risk_scaling_enabled": False,
        "entry_er_risk_scaling_enabled": False,
        "entry_rsi_divergence_enabled": False,
        "streak_trend_only": False,
    }
    values.update(overrides)
    return Config(**values)


def test_bullish_cross_is_skipped_when_price_is_below_both_emas(monkeypatch):
    # EMA55 cruza al alza en la vela 1, pero el cierre (1.5) queda por debajo
    # de la EMA rápida (3) y de la lenta (2).
    configure_crosses(
        monkeypatch,
        fast=[1.0, 3.0, 3.0, 3.0],
        slow=[2.0, 2.0, 2.0, 2.0],
        macd_line=[0.0, 1.0, 1.0, 1.0],
    )

    signals = EMACrossStrategy(make_config()).generate_signals(
        make_bars([1.0, 1.5, 1.5, 1.5])
    )

    assert signals.target.tolist() == [0, 0, 0, 0]


def test_bullish_cross_is_allowed_when_price_is_above_both_emas(monkeypatch):
    configure_crosses(
        monkeypatch,
        fast=[1.0, 3.0, 3.0, 3.0],
        slow=[2.0, 2.0, 2.0, 2.0],
        macd_line=[0.0, 1.0, 1.0, 1.0],
    )

    signals = EMACrossStrategy(make_config()).generate_signals(
        make_bars([1.0, 4.0, 4.0, 4.0])
    )

    assert signals.target.tolist() == [0, 1, 0, 0]


def test_bullish_cross_between_the_emas_is_allowed(monkeypatch):
    # El cierre está por debajo de la EMA rápida pero por encima de la lenta:
    # no está debajo de *ambas*, así que la entrada se permite.
    configure_crosses(
        monkeypatch,
        fast=[1.0, 3.0, 3.0, 3.0],
        slow=[2.0, 2.0, 2.0, 2.0],
        macd_line=[0.0, 1.0, 1.0, 1.0],
    )

    signals = EMACrossStrategy(make_config()).generate_signals(
        make_bars([1.0, 2.5, 2.5, 2.5])
    )

    assert signals.target.tolist() == [0, 1, 0, 0]


def test_bearish_cross_is_skipped_when_price_is_above_both_emas(monkeypatch):
    # Espejo del caso alcista: el cruce bajista no se opera si el precio sigue
    # por encima de las dos EMAs.
    configure_crosses(
        monkeypatch,
        fast=[3.0, 1.0, 1.0, 1.0],
        slow=[2.0, 2.0, 2.0, 2.0],
        macd_line=[0.0, -1.0, -1.0, -1.0],
    )

    signals = EMACrossStrategy(make_config()).generate_signals(
        make_bars([3.0, 4.0, 4.0, 4.0])
    )

    assert signals.target.tolist() == [0, 0, 0, 0]


def test_filter_can_be_disabled(monkeypatch):
    configure_crosses(
        monkeypatch,
        fast=[1.0, 3.0, 3.0, 3.0],
        slow=[2.0, 2.0, 2.0, 2.0],
        macd_line=[0.0, 1.0, 1.0, 1.0],
    )

    signals = EMACrossStrategy(
        make_config(entry_price_ema_filter_enabled=False)
    ).generate_signals(make_bars([1.0, 1.5, 1.5, 1.5]))

    assert signals.target.tolist() == [0, 1, 0, 0]


def test_filter_defaults_to_enabled_and_config_can_disable_it():
    assert Config().entry_price_ema_filter_enabled

    cfg = Config(entry_price_ema_filter_enabled=False)
    assert cfg.entry_price_ema_filter_enabled is False
