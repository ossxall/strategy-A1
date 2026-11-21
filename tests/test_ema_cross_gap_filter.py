import numpy as np
import pandas as pd
import pytest

import strategy.strategy as strategy_module
from strategy.config import Config
from strategy.strategy import EMACrossStrategy


def _bars(periods=7):
    index = pd.date_range("2026-01-01", periods=periods, freq="30min", tz="UTC")
    close = np.full(periods, 100.0)
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


def _config(**overrides):
    values = {
        "timeframe": "30min",
        "macd_timeframe": None,
        "confirm_bars": 4,
        "ema_cross_min_gap_atr": 0.10,
        "entry_adx_enabled": False,
        "entry_atr_enabled": False,
        "entry_rsi_divergence_enabled": False,
        "entry_gaussian_trend_enabled": False,
        "gaussian_trend_reversal_exit_enabled": False,
        "entry_price_ema_filter_enabled": False,
        "streak_trend_only": False,
    }
    values.update(overrides)
    return Config(**values)


def _patch_cross(monkeypatch, gap_at_confirmation):
    fast = np.array(
        [0.9, 1.01, 1.02, 1.03, 1.04, 1.0 + gap_at_confirmation, 1.1]
    )
    slow = np.ones(7)
    monkeypatch.setattr(
        strategy_module,
        "ema",
        lambda values, period: pd.Series(
            fast if period == 55 else slow, index=values.index
        ),
    )
    monkeypatch.setattr(
        strategy_module,
        "atr",
        lambda bars, length: pd.Series(np.full(len(bars), 0.5), index=bars.index),
    )
    monkeypatch.setattr(
        EMACrossStrategy,
        "_macd_line",
        lambda self, bars, raw: np.ones(len(bars)),
    )


def test_thin_ema_cross_is_rejected_at_confirmation(monkeypatch):
    _patch_cross(monkeypatch, gap_at_confirmation=0.049)

    signals = EMACrossStrategy(_config()).generate_signals(_bars())

    assert signals.cross_up_candidate.iloc[5]
    assert not signals.cross_up.iloc[5]
    assert signals.cross_up_rejected.iloc[5]
    assert signals.ema_cross_gap_atr.iloc[5] == pytest.approx(0.098)
    assert "separación EMA insuficiente" in signals.entry_rejection_reasons_long.iloc[5]


def test_ema_cross_meeting_minimum_atr_gap_is_allowed(monkeypatch):
    _patch_cross(monkeypatch, gap_at_confirmation=0.06)

    signals = EMACrossStrategy(_config()).generate_signals(_bars())

    assert signals.cross_up.iloc[5]
    assert not signals.cross_up_rejected.iloc[5]
    assert signals.ema_cross_gap_atr.iloc[5] == pytest.approx(0.12)


def test_ema_cross_gap_filter_can_be_disabled(monkeypatch):
    _patch_cross(monkeypatch, gap_at_confirmation=0.001)

    signals = EMACrossStrategy(
        _config(ema_cross_min_gap_atr=0)
    ).generate_signals(_bars())

    assert signals.cross_up.iloc[5]


@pytest.mark.parametrize(
    "overrides",
    [
        {"ema_cross_min_gap_atr": -0.01},
        {"ema_cross_atr_len": 0},
    ],
)
def test_ema_cross_gap_filter_config_is_validated(overrides):
    with pytest.raises(ValueError, match="ema_cross_"):
        _config(**overrides)
