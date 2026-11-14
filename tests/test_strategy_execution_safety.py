import numpy as np
import pandas as pd
import pytest

from strategy.config import Config
from strategy.models import Position
from strategy.strategy import Backtester


def make_bars(rows):
    index = pd.date_range("2026-01-01", periods=len(rows), freq="30min", tz="UTC")
    return pd.DataFrame(rows, index=index)


def quiet_config(**overrides):
    values = {
        "entry_adx_enabled": False,
        "entry_atr_enabled": False,
        "entry_rsi_divergence_enabled": False,
        "entry_mfi_risk_scaling_enabled": False,
        "entry_er_risk_scaling_enabled": False,
        "adx_exit_enabled": False,
        "ema_weakening_exit_enabled": False,
        "rsi_divergence_exit_enabled": False,
        "ema55_touch_exit_enabled": False,
        "volume_exit_enabled": False,
        "streak_trend_only": False,
        "stop_loss_pct": 0,
        "monthly_loss_limit_pct": 0,
        "force_close_at_end": False,
    }
    values.update(overrides)
    return Config(**values)


def manual_signals(bars, targets):
    return pd.DataFrame(
        {
            "valid": np.ones(len(bars), dtype=bool),
            "target": targets,
            "trend_bias": np.zeros(len(bars), dtype=int),
        },
        index=bars.index,
    )


@pytest.mark.parametrize(
    "side,open_price,high,low,expected_fill",
    [
        (1, 80.0, 82.0, 70.0, 80.0),
        (-1, 120.0, 130.0, 118.0, 120.0),
    ],
)
def test_gap_through_stop_fills_at_open_not_at_better_stop_price(
    side, open_price, high, low, expected_fill
):
    bars = make_bars(
        [
            {
                "open": open_price,
                "high": high,
                "low": low,
                "close": open_price,
                "volume": 1.0,
            }
        ]
    )
    engine = Backtester(
        bars,
        quiet_config(stop_loss_pct=0.1),
        signals=manual_signals(bars, [0]),
    )
    engine.position = Position(
        position_id=1,
        side=side,
        quantity=1.0,
        initial_quantity=1.0,
        entry_price=100.0,
        entry_time=bars.index[0],
        entry_fee=0.0,
        stop_pct=0.1,
        risk_amount=10.0,
    )

    assert engine._stop(0)
    assert engine.trades[0].reason == "STOP_LOSS_10.00%"
    assert engine.trades[0].exit_price == expected_fill


@pytest.mark.parametrize("side", [1, -1])
def test_stop_takes_priority_when_stop_and_take_profit_are_in_same_candle(side):
    bars = make_bars(
        [
            {
                "open": 100.0,
                "high": 120.0,
                "low": 80.0,
                "close": 100.0,
                "volume": 1.0,
            }
        ]
    )
    signals = manual_signals(bars, [0])
    config = quiet_config(
        stop_loss_pct=0.1,
        partial_tp_enabled=True,
        partial_tp_r=1.0,
        partial_tp_pct=50.0,
    )
    engine = Backtester(bars, config, signals=signals)
    engine.position = Position(
        position_id=1,
        side=side,
        quantity=1.0,
        initial_quantity=1.0,
        entry_price=100.0,
        entry_time=bars.index[0],
        entry_fee=0.0,
        stop_pct=0.1,
        risk_amount=10.0,
    )

    trades, _ = engine.run()

    assert trades.reason.tolist() == ["STOP_LOSS_10.00%"]
    assert trades.quantity.tolist() == [1.0]


def test_partial_closes_reconcile_cash_and_allocate_entry_fee_once():
    bars = make_bars(
        [
            {
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1.0,
            }
        ]
    )
    config = quiet_config(commission_bps=10.0, max_leverage=1.0)
    engine = Backtester(
        bars,
        config,
        signals=manual_signals(bars, [0]),
    )
    engine._bar_index = 0
    engine.open_position(1, 100.0, bars.index[0])
    assert engine.position is not None
    assert engine.position.quantity == pytest.approx(100.0)

    engine.close_position(50.0, 110.0, bars.index[0], "PARTIAL_TEST")
    engine.close_position(100.0, 110.0, bars.index[0], "FINAL_TEST")

    assert sum(trade.pnl_gross for trade in engine.trades) == pytest.approx(1000.0)
    assert sum(trade.fees for trade in engine.trades) == pytest.approx(21.0)
    assert sum(trade.pnl_net for trade in engine.trades) == pytest.approx(979.0)
    assert engine.cash == pytest.approx(config.initial_capital + 979.0)
    assert engine.position is None


def test_monthly_loss_limit_closes_position_and_blocks_reentry_until_month_end():
    bars = make_bars(
        [
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1.0},
            {"open": 100.0, "high": 101.0, "low": 90.0, "close": 90.0, "volume": 1.0},
            {"open": 90.0, "high": 91.0, "low": 89.0, "close": 90.0, "volume": 1.0},
        ]
    )
    engine = Backtester(
        bars,
        quiet_config(monthly_loss_limit_pct=5.0),
        signals=manual_signals(bars, [1, 0, 1]),
    )

    trades, equity = engine.run()

    assert trades.reason.tolist() == ["MONTHLY_LOSS_LIMIT_5%"]
    assert trades.pnl_net.iloc[0] == pytest.approx(-1000.0)
    assert trades.exit_time.iloc[0] == bars.index[1] + pd.Timedelta("30min")
    assert engine.position is None
    assert engine.halt_until is not None
    assert "protección mensual" in equity.execution_rejection.iloc[2]


def test_monthly_loss_limit_rearms_at_the_start_of_the_next_calendar_month():
    bars = make_bars(
        [
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1.0},
            {"open": 100.0, "high": 101.0, "low": 90.0, "close": 90.0, "volume": 1.0},
            {"open": 90.0, "high": 91.0, "low": 89.0, "close": 90.0, "volume": 1.0},
            {"open": 90.0, "high": 91.0, "low": 89.0, "close": 90.0, "volume": 1.0},
        ]
    )
    bars.index = pd.DatetimeIndex(
        [
            "2026-01-31 22:30:00+00:00",
            "2026-01-31 23:00:00+00:00",
            "2026-02-01 00:00:00+00:00",
            "2026-02-01 00:30:00+00:00",
        ]
    )
    engine = Backtester(
        bars,
        quiet_config(monthly_loss_limit_pct=5.0),
        signals=manual_signals(bars, [1, 0, 1, 0]),
    )

    trades, _ = engine.run()

    assert trades.reason.tolist() == ["MONTHLY_LOSS_LIMIT_5%"]
    assert len(engine.opened) == 2
    assert engine.position is not None
    assert engine.position.entry_time == bars.index[3]


def test_equity_records_effective_stop_and_breakeven_activation():
    bars = make_bars(
        [
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1.0},
            {"open": 100.0, "high": 111.0, "low": 99.0, "close": 110.0, "volume": 1.0},
        ]
    )
    engine = Backtester(
        bars,
        quiet_config(
            stop_loss_pct=0.1,
            partial_tp_enabled=True,
            partial_tp_r=1.0,
            partial_tp_pct=50.0,
            move_stop_to_breakeven=True,
        ),
        signals=manual_signals(bars, [0, 0]),
    )
    engine._bar_index = 0
    engine.open_position(1, 100.0, bars.index[0])
    engine._bar_index = 1

    engine._manage(1)
    engine._mark(bars.index[1] + pd.Timedelta("30min"), 110.0, signal_time=bars.index[1])

    assert engine.eq_breakeven_activated == [True]
    assert engine.eq_breakeven_price == [100.0]
    assert engine.eq_stop_price == [100.0]


def test_adaptive_risk_multiplier_uses_only_the_configured_recent_window():
    bars = make_bars(
        [
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1.0},
        ]
    )
    config = quiet_config(
        adaptive_risk_window=2,
        adaptive_risk_k=2.0,
        adaptive_risk_min=0.5,
        adaptive_risk_max=1.5,
    )
    engine = Backtester(bars, config, signals=manual_signals(bars, [0]))

    engine._recent_wins = [False, False]
    assert engine._risk_multiplier() == pytest.approx(0.5)
    engine._recent_wins = [False, True]
    assert engine._risk_multiplier() == pytest.approx(1.0)
    engine._recent_wins = [True, True]
    assert engine._risk_multiplier() == pytest.approx(1.5)
