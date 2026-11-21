import numpy as np
import pandas as pd

import strategy.indicators as indicators
from strategy.config import Config


def test_lower_timeframe_weakening_uses_raw_bars(monkeypatch):
    strategy_index = pd.date_range(
        "2026-01-01", periods=60, freq="30min", tz="UTC"
    )
    strategy_close = 100.0 + np.arange(len(strategy_index), dtype=float)
    bars = pd.DataFrame(
        {
            "open": strategy_close,
            "high": strategy_close + 1.0,
            "low": strategy_close - 1.0,
            "close": strategy_close,
            "volume": 1.0,
        },
        index=strategy_index,
    )

    raw_index = pd.date_range(
        strategy_index[0],
        periods=(len(strategy_index) - 1) * 30 + 1,
        freq="1min",
        tz="UTC",
    )
    raw_close = 100.0 + np.arange(len(raw_index), dtype=float) / 30.0
    raw = pd.DataFrame(
        {
            "open": raw_close,
            "high": raw_close + 0.1,
            "low": raw_close - 0.1,
            "close": raw_close,
            "volume": 1.0,
        },
        index=raw_index,
    )
    cfg = Config(
        timeframe="30min",
        ema_weakening_timeframe="1min",
        ema_weakening_fast=2,
        ema_weakening_slow=3,
        entry_adx_enabled=False,
        entry_atr_enabled=False,
        entry_rsi_divergence_enabled=False,
        entry_gaussian_trend_enabled=False,
        adx_exit_enabled=False,
        exit_rsi_divergence_enabled=False,
        streak_trend_only=False,
    )

    lengths = []

    def record_weakening_flags(fast, slow, gap_pct):
        lengths.append((len(fast), len(slow)))
        return np.zeros(len(fast), dtype=bool)

    monkeypatch.setattr(
        indicators, "ema_weakening_flags", record_weakening_flags
    )

    flags = indicators.build_weakening_exit(raw, bars, cfg)

    assert lengths == [(len(raw), len(raw))]
    assert len(flags) == len(bars)
