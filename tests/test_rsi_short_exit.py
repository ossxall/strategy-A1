import numpy as np
import pandas as pd
import pytest

import strategy.indicators as indicators
from strategy.config import Config
from strategy.strategy import Backtester


def make_bars(periods=4):
    index = pd.date_range("2026-01-01", periods=periods, freq="30min", tz="UTC")
    return pd.DataFrame(
        {
            "open": np.full(periods, 100.0),
            "high": np.full(periods, 100.5),
            "low": np.full(periods, 99.5),
            "close": np.full(periods, 100.0),
            "volume": np.ones(periods),
        },
        index=index,
    )


def make_config(**kwargs):
    return Config(
        timeframe="30min",
        stop_loss_pct=0,
        entry_adx_enabled=False,
        entry_atr_enabled=False,
        entry_rsi_divergence_enabled=False,
        entry_mfi_risk_scaling_enabled=False,
        entry_er_risk_scaling_enabled=False,
        partial_tp_enabled=False,
        entry_partial_enabled=False,
        adx_exit_enabled=False,
        ema_weakening_exit_enabled=False,
        rsi_divergence_exit_enabled=False,
        ema55_touch_exit_enabled=False,
        streak_trend_only=False,
        monthly_loss_limit_pct=0,
        force_close_at_end=False,
        **kwargs,
    )


def test_rsi_short_exit_uses_rsi14_on_30m_and_flags_values_at_or_below_30(monkeypatch):
    bars = make_bars(5)
    seen = {}

    def fake_rsi(values, length):
        seen["length"] = length
        return pd.Series([50.0, 31.0, 30.0, 25.0, 40.0], index=values.index)

    monkeypatch.setattr(indicators, "rsi", fake_rsi)
    cfg = make_config()

    flags = indicators.build_rsi_short_exit(None, bars, cfg)

    assert cfg.rsi_short_exit_enabled
    assert cfg.rsi_short_exit_timeframe == "30min"
    assert cfg.rsi_short_exit_threshold == 30.0
    assert seen["length"] == 14
    assert flags.tolist() == [False, False, True, True, False]


@pytest.mark.parametrize(("side", "should_exit"), [(-1, True), (1, False)])
def test_rsi_short_exit_closes_shorts_only(side, should_exit):
    bars = make_bars()
    signals = pd.DataFrame(
        {
            "valid": [True] * len(bars),
            "target": [side, 0, 0, 0],
            "trend_bias": [0] * len(bars),
            "rsi_short_exit": [False, True, False, False],
        },
        index=bars.index,
    )

    engine = Backtester(bars, make_config(), signals=signals)
    trades, _ = engine.run()

    if should_exit:
        assert len(trades) == 1
        assert trades.iloc[0].reason == "RSI_SHORT_EXIT_30"
        assert trades.iloc[0].exit_time == bars.index[2]
    else:
        assert trades.empty
        assert engine.position is not None
        assert engine.position.side == 1


def test_rsi_short_exit_config_validation():
    with pytest.raises(ValueError, match="salida RSI SHORT"):
        Config(rsi_short_exit_len=0)
    with pytest.raises(ValueError, match="salida RSI SHORT"):
        Config(rsi_short_exit_threshold=101.0)
