import numpy as np
import pandas as pd

from strategy.config import Config
from strategy.report import (
    _closed_x_ms,
    _configured_tp_levels,
    _position_spans,
    _r_target_price,
    calculate_metrics,
)


def test_daily_ratios_include_first_day_return_from_initial_capital():
    capital = 100.0
    equity = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2024-01-01T00:00:00Z",
                    "2024-01-01T23:59:00Z",
                    "2024-01-02T23:59:00Z",
                    "2024-01-03T23:59:00Z",
                ]
            ),
            "equity": [capital, 110.0, 120.0, 108.0],
        }
    )
    daily_returns = np.array([0.10, 120 / 110 - 1, 108 / 120 - 1])

    metrics = calculate_metrics(pd.DataFrame(), equity, capital)

    expected_sharpe = daily_returns.mean() / daily_returns.std(ddof=1) * np.sqrt(365)
    downside = np.sqrt(np.mean(np.minimum(daily_returns, 0) ** 2))
    expected_sortino = daily_returns.mean() / downside * np.sqrt(365)
    assert np.isclose(metrics["sharpe_daily_365"], expected_sharpe)
    assert np.isclose(metrics["sortino_daily_365"], expected_sortino)


def test_drawdown_uses_initial_capital_as_starting_peak():
    equity = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2024-01-01T00:00:00Z", "2024-01-01T12:00:00Z"]
            ),
            "equity": [90.0, 80.0],
        }
    )

    metrics = calculate_metrics(pd.DataFrame(), equity, 100.0)

    assert np.isclose(metrics["max_drawdown_pct"], -20.0)
    assert metrics["max_dd_peak_equity"] == 100.0
    assert metrics["max_dd_trough_equity"] == 80.0
    assert metrics["min_equity"] == 80.0
    assert np.isclose(metrics["min_equity_vs_capital_pct"], -20.0)


def test_tp_chart_levels_match_position_risk_and_execution_enablement():
    trades = pd.DataFrame(
        [
            {
                "trade_id": 1,
                "position_id": 7,
                "side": "LONG",
                "entry_time": pd.Timestamp("2024-01-01T00:00:00Z"),
                "exit_time": pd.Timestamp("2024-01-01T01:00:00Z"),
                "entry_price": 100.0,
                "exit_price": 110.0,
                "quantity": 10.0,
                "pnl_net": 95.0,
                "position_risk": 50.0,
                "position_initial_quantity": 10.0,
            }
        ]
    )
    span = _position_spans(trades)[0]

    assert span["risk_per_unit"] == 5.0
    assert _r_target_price(span, 1, 1.5) == 107.5
    assert _r_target_price(span, -1, 1.5) == 92.5
    assert _configured_tp_levels(Config(partial_tp_enabled=False)) == []
    levels = _configured_tp_levels(Config(partial_tp_enabled=True))
    assert len(levels) == 40
    assert levels[-1][0] == "TP40"


def test_higher_timeframe_plot_timestamps_are_candle_close_times():
    index = pd.date_range("2024-01-01", periods=2, freq="1h", tz="UTC")

    closed = _closed_x_ms(index, "1h")

    assert pd.to_datetime(closed, unit="ms", utc=True).tolist() == [
        index[0] + pd.Timedelta("1h"),
        index[1] + pd.Timedelta("1h"),
    ]
