import numpy as np
import pandas as pd
import pytest

import strategy.strategy as strategy_module
from strategy.config import Config
from strategy.indicators import gaussian_filter_trend
from strategy.models import Position
from strategy.strategy import Backtester, EMACrossStrategy
from live.bot.config import build_strategy_config


def make_bars(periods=4, close=None):
    index = pd.date_range("2026-01-01", periods=periods, freq="30min", tz="UTC")
    close = np.asarray(close if close is not None else np.full(periods, 100.0), float)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1.0,
        },
        index=index,
    )


def make_config(**overrides):
    values = {
        "timeframe": "30min",
        "gaussian_trend_reversal_exit_timeframe": "30min",
        "macd_timeframe": None,
        "confirm_bars": 0,
        "entry_adx_enabled": False,
        "entry_atr_enabled": False,
        "entry_rsi_divergence_enabled": False,
        "entry_price_ema_filter_enabled": False,
        "streak_trend_only": False,
        "monthly_loss_limit_pct": 0,
        "partial_tp_enabled": False,
        "partial_tp2_r": 0,
        "partial_tp2_pct": 0,
        "partial_tp3_r": 0,
        "partial_tp3_pct": 0,
        "partial_tp4_r": 0,
        "partial_tp4_pct": 0,
        "partial_tp5_r": 0,
        "partial_tp5_pct": 0,
        "partial_tp6_r": 0,
        "partial_tp6_pct": 0,
        "partial_tp7_r": 0,
        "partial_tp7_pct": 0,
        "adx_exit_enabled": False,
        "exit_rsi_divergence_enabled": False,
        "force_close_at_end": False,
    }
    values.update(overrides)
    return Config(**values)


def configure_crosses(monkeypatch):
    fast = np.array([1.0, 3.0, 3.0, 1.0])
    slow = np.array([2.0, 2.0, 2.0, 2.0])
    monkeypatch.setattr(
        strategy_module,
        "ema",
        lambda values, period: pd.Series(
            fast if period == 55 else slow, index=values.index
        ),
    )
    monkeypatch.setattr(
        strategy_module.talib,
        "MACD",
        lambda *args, **kwargs: (
            np.array([0.0, 1.0, 1.0, -1.0]),
            np.zeros(4),
            np.zeros(4),
        ),
    )


def test_gaussian_trend_filter_requires_matching_direction_for_each_side(monkeypatch):
    configure_crosses(monkeypatch)
    directions = np.array([0, -1, 1, 1])
    monkeypatch.setattr(
        strategy_module,
        "gaussian_filter_trend",
        lambda bars, *args: pd.DataFrame(
            {"trend_line": np.arange(len(bars)), "trend_dir": directions},
            index=bars.index,
        ),
    )

    signals = EMACrossStrategy(make_config()).generate_signals(make_bars())

    assert signals.target.tolist() == [0, 0, 0, 0]
    assert signals.cross_up_rejected.iloc[1]
    assert signals.cross_down_rejected.iloc[3]
    assert "Gaussian Filter Trend" in signals.entry_rejection_reasons_long.iloc[1]
    assert "Gaussian Filter Trend" in signals.entry_rejection_reasons_short.iloc[3]

    directions[:] = [0, 1, 1, -1]
    signals = EMACrossStrategy(make_config()).generate_signals(make_bars())
    assert signals.target.tolist() == [0, 1, 0, -1]


def test_gaussian_filter_trend_is_causal_and_detects_both_directions():
    close = np.r_[np.linspace(100, 150, 50), np.linspace(149, 90, 50)]
    bars = make_bars(len(close), close)

    full = gaussian_filter_trend(bars)
    prefix = gaussian_filter_trend(bars.iloc[:70])

    assert (full.trend_dir == 1).any()
    assert (full.trend_dir == -1).any()
    np.testing.assert_allclose(
        full.trend_line.iloc[:70], prefix.trend_line, equal_nan=True
    )
    np.testing.assert_array_equal(full.trend_dir.iloc[:70], prefix.trend_dir)


def test_gaussian_filter_trend_accepts_empty_bars():
    result = gaussian_filter_trend(make_bars().iloc[:0])

    assert result.empty
    assert {"trend_line", "trend_dir"} <= set(result.columns)


def test_backtester_applies_gaussian_filter_to_custom_entry_signals(monkeypatch):
    bars = make_bars(4)
    monkeypatch.setattr(
        strategy_module,
        "gaussian_filter_trend",
        lambda frame, *args: pd.DataFrame(
            {"trend_line": np.zeros(len(frame)), "trend_dir": np.array([0, -1, -1, -1])},
            index=frame.index,
        ),
    )
    signals = pd.DataFrame(
        {"valid": True, "target": [0, 1, 0, 0], "trend_bias": 0},
        index=bars.index,
    )

    trades, _ = Backtester(bars, make_config(), signals=signals).run()

    assert trades.empty


@pytest.mark.parametrize(
    ("side", "directions", "reason"),
    [
        (1, [1, -1, -1, 1], "GAUSSIAN_TREND_BEAR_40%"),
        (-1, [-1, 1, 1, -1], "GAUSSIAN_TREND_BULL_40%"),
    ],
)
def test_gaussian_reversal_partially_closes_position_once_per_flip(
    side, directions, reason
):
    bars = make_bars(4)
    bars["open"] = [100.0, 101.0, 102.0, 103.0]
    signals = pd.DataFrame(
        {
            "valid": True,
            "target": 0,
            "trend_bias": 0,
            "entry_gaussian_trend_dir": [directions[0]] * 4,
            "gaussian_trend_reversal_exit_dir": directions,
        },
        index=bars.index,
    )
    engine = Backtester(
        bars,
        make_config(gaussian_trend_reversal_exit_pct=40.0),
        signals=signals,
    )
    engine.position = Position(
        position_id=1,
        side=side,
        quantity=1.0,
        initial_quantity=1.0,
        entry_price=100.0,
        entry_time=bars.index[0],
        entry_fee=0.0,
        stop_pct=0.0,
        risk_amount=0.0,
    )

    trades, _ = engine.run()

    assert trades.reason.tolist() == [reason]
    assert trades.quantity.tolist() == pytest.approx([0.4])
    assert trades.exit_time.iloc[0] == bars.index[2]
    assert engine.position is not None
    assert engine.position.quantity == pytest.approx(0.6)


def test_gaussian_reversal_exit_calculates_trend_when_entry_filter_is_disabled(
    monkeypatch,
):
    configure_crosses(monkeypatch)
    directions = np.array([0, 1, 1, -1])
    monkeypatch.setattr(
        strategy_module,
        "gaussian_filter_trend",
        lambda bars, *args: pd.DataFrame(
            {"trend_line": np.arange(len(bars)), "trend_dir": directions},
            index=bars.index,
        ),
    )

    signals = EMACrossStrategy(
        make_config(entry_gaussian_trend_enabled=False)
    ).generate_signals(make_bars())

    assert signals.entry_gaussian_trend_dir.tolist() == directions.tolist()
    assert signals.target.tolist() == [0, 1, 0, -1]


def test_gaussian_reversal_exit_uses_selected_higher_timeframe_without_lookahead(
    monkeypatch,
):
    configure_crosses(monkeypatch)
    observed_lengths = []

    def fake_gaussian(bars, *args):
        observed_lengths.append(len(bars))
        direction = [-1, 1] if len(bars) == 2 else [0, 0, 0, 0]
        return pd.DataFrame(
            {"trend_line": np.arange(len(bars)), "trend_dir": direction},
            index=bars.index,
        )

    monkeypatch.setattr(strategy_module, "gaussian_filter_trend", fake_gaussian)

    signals = EMACrossStrategy(
        make_config(
            entry_gaussian_trend_enabled=False,
            gaussian_trend_reversal_exit_timeframe="1h",
        )
    ).generate_signals(make_bars())

    assert observed_lengths == [4, 2]
    assert signals.gaussian_trend_reversal_exit_dir.tolist() == [0, -1, -1, 1]


def test_gaussian_reversal_exit_combines_multiple_timeframes_with_or(monkeypatch):
    bars = make_bars(8)

    def fake_gaussian(frame, *args):
        directions = {
            8: np.zeros(8, dtype=int),
            4: np.array([1, 1, -1, -1]),
            2: np.array([-1, 1]),
        }[len(frame)]
        return pd.DataFrame(
            {"trend_line": np.arange(len(frame)), "trend_dir": directions},
            index=frame.index,
        )

    monkeypatch.setattr(strategy_module, "gaussian_filter_trend", fake_gaussian)
    config = make_config(
        entry_gaussian_trend_enabled=False,
        gaussian_trend_reversal_exit_timeframe=("1h", "2h"),
        gaussian_trend_reversal_exit_pct=40.0,
    )
    signals = EMACrossStrategy(config).generate_signals(bars)

    assert signals.gaussian_trend_reversal_exit_bearish.tolist() == [
        False, False, False, False, False, True, False, False,
    ]
    assert signals.gaussian_trend_reversal_exit_bullish.tolist() == [
        False, False, False, False, False, False, False, True,
    ]

    engine = Backtester(bars, config, signals=signals)
    engine.position = Position(
        position_id=1,
        side=1,
        quantity=1.0,
        initial_quantity=1.0,
        entry_price=100.0,
        entry_time=bars.index[0],
        entry_fee=0.0,
        stop_pct=0.0,
        risk_amount=0.0,
    )
    trades, _ = engine.run()

    assert trades.reason.tolist() == ["GAUSSIAN_TREND_BEAR_40%"]
    assert trades.quantity.tolist() == pytest.approx([0.4])


def test_gaussian_reversal_exit_does_not_trigger_from_neutral_warmup():
    bars = make_bars(3)
    signals = pd.DataFrame(
        {
            "valid": True,
            "target": 0,
            "trend_bias": 0,
            "entry_gaussian_trend_dir": [0, -1, -1],
        },
        index=bars.index,
    )
    engine = Backtester(bars, make_config(), signals=signals)
    engine.position = Position(
        position_id=1,
        side=1,
        quantity=1.0,
        initial_quantity=1.0,
        entry_price=100.0,
        entry_time=bars.index[0],
        entry_fee=0.0,
        stop_pct=0.0,
        risk_amount=0.0,
    )

    trades, _ = engine.run()

    assert trades.empty
    assert engine.position is not None


def test_backtester_accepts_empty_bars_with_gaussian_exit_enabled():
    bars = make_bars().iloc[:0]
    signals = pd.DataFrame(
        {"valid": pd.Series(dtype=bool), "target": pd.Series(dtype=int)},
        index=bars.index,
    )

    trades, equity = Backtester(bars, make_config(), signals=signals).run()

    assert trades.empty
    assert equity.empty


def test_gaussian_filter_config_defaults_and_validation():
    config = Config()

    assert config.entry_gaussian_trend_enabled
    assert config.entry_gaussian_source == "close"
    assert config.entry_gaussian_length == 14
    assert config.entry_gaussian_poles == 4
    assert config.entry_gaussian_atr_length == 14
    assert config.entry_gaussian_adaptive_width
    assert config.gaussian_trend_reversal_exit_enabled
    assert config.gaussian_trend_reversal_exit_pct == 10.0
    assert config.gaussian_trend_reversal_exit_timeframe == ("30min", "4h", "1D")
    assert config.gaussian_trend_reversal_exit_timeframes == ("30min", "4h", "1D")
    assert Config(
        gaussian_trend_reversal_exit_timeframe=("1h", "4h")
    ).gaussian_trend_reversal_exit_timeframes == ("1h", "4h")

    with pytest.raises(ValueError, match="Gaussian"):
        Config(entry_gaussian_source="volume")
    with pytest.raises(ValueError, match="Gaussian"):
        Config(entry_gaussian_poles=5)
    with pytest.raises(ValueError, match="gaussian_trend_reversal_exit_pct"):
        Config(gaussian_trend_reversal_exit_pct=0)
    with pytest.raises(ValueError, match="gaussian_trend_reversal_exit_timeframe"):
        Config(gaussian_trend_reversal_exit_timeframe="0min")


def test_live_config_ignores_gaussian_environment_overrides(monkeypatch):
    monkeypatch.setenv("BOT_STRATEGY_ENTRY_GAUSSIAN_TREND", "false")
    monkeypatch.setenv("BOT_STRATEGY_ENTRY_GAUSSIAN_SOURCE", "hl2")
    monkeypatch.setenv("BOT_STRATEGY_ENTRY_GAUSSIAN_LENGTH", "8")
    monkeypatch.setenv("BOT_STRATEGY_GAUSSIAN_TREND_REVERSAL_EXIT", "false")
    monkeypatch.setenv("BOT_STRATEGY_GAUSSIAN_TREND_REVERSAL_EXIT_PCT", "35")

    config = build_strategy_config()

    assert config.entry_gaussian_trend_enabled
    assert config.entry_gaussian_source == "close"
    assert config.entry_gaussian_length == 14
    assert config.gaussian_trend_reversal_exit_enabled
    assert config.gaussian_trend_reversal_exit_pct == 10.0
