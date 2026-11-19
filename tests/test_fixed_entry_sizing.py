import numpy as np
import pandas as pd
import pytest

from strategy.config import Config
from strategy.strategy import Backtester


def test_entry_sizing_uses_fixed_base_risk_without_multipliers():
    index = pd.date_range("2026-01-01", periods=2, freq="30min", tz="UTC")
    bars = pd.DataFrame(
        {
            "open": [100.0, 100.0],
            "high": [100.0, 100.0],
            "low": [100.0, 100.0],
            "close": [100.0, 100.0],
            "volume": [1.0, 1.0],
        },
        index=index,
    )
    signals = pd.DataFrame(
        {
            "valid": np.ones(2, dtype=bool),
            "target": np.zeros(2, dtype=int),
            "trend_bias": np.zeros(2, dtype=int),
        },
        index=index,
    )
    config = Config(
        timeframe="30min",
        initial_capital=1_000.0,
        risk_pct=0.01,
        stop_loss_pct=0.01,
        entry_adx_enabled=False,
        entry_atr_enabled=False,
        streak_trend_only=False,
    )
    engine = Backtester(bars, config, signals=signals)

    sizing = engine.entry_sizing_context(1, sizing_equity=1_000.0)
    assert sizing == {
        "risk_pct": 0.01,
        "stop_loss_pct": 0.01,
        "commission_bps": 0.0,
        "slippage_bps": 0.0,
        "sizing_equity": 1_000.0,
    }

    engine._bar_index = 0
    engine.open_position(1, 100.0, index[0])
    assert engine.position.initial_quantity == pytest.approx(10.0)
    assert engine.position.risk_amount == pytest.approx(10.0)
