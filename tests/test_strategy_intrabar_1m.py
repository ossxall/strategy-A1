import numpy as np
import pandas as pd
import pytest

from strategy.config import Config
from strategy.models import Position
from strategy.strategy import Backtester


def make_strategy_bars():
    index = pd.date_range(
        "2026-01-01", periods=2, freq="5min", tz="UTC"
    )
    return pd.DataFrame(
        {
            "open": [100.0, 100.0],
            "high": [100.0, 111.0],
            "low": [100.0, 99.0],
            "close": [100.0, 101.0],
            "volume": [5.0, 5.0],
        },
        index=index,
    )


def make_one_minute_bars():
    index = pd.date_range(
        "2026-01-01", periods=10, freq="1min", tz="UTC"
    )
    rows = [
        {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1.0}
        for _ in range(10)
    ]
    # En la segunda vela de 5m, TP1 sucede antes que el stop a breakeven.
    rows[5] = {
        "open": 100.0, "high": 111.0, "low": 100.0, "close": 109.0, "volume": 1.0
    }
    rows[6] = {
        "open": 109.0, "high": 109.0, "low": 99.0, "close": 101.0, "volume": 1.0
    }
    return pd.DataFrame(rows, index=index)


def make_config(**overrides):
    values = {
        "timeframe": "5min",
        "ema_fast": 1,
        "ema_slow": 2,
        "stop_loss_pct": 0.1,
        "partial_tp_enabled": True,
        "partial_tp_r": 1.0,
        "partial_tp_pct": 50.0,
        "move_stop_to_breakeven": True,
        "entry_adx_enabled": False,
        "entry_atr_enabled": False,
        "entry_rsi_divergence_enabled": False,
        "entry_gaussian_trend_enabled": False,
        "adx_exit_enabled": False,
        "ema_weakening_exit_enabled": False,
        "rsi_divergence_exit_enabled": False,
        "streak_trend_only": False,
        "monthly_loss_limit_pct": 0,
        "force_close_at_end": False,
    }
    values.update(overrides)
    return Config(**values)


def make_signals(bars):
    return pd.DataFrame(
        {
            "valid": np.ones(len(bars), dtype=bool),
            "target": np.zeros(len(bars), dtype=int),
            "trend_bias": np.zeros(len(bars), dtype=int),
        },
        index=bars.index,
    )


def add_open_position(engine, timestamp):
    engine.position = Position(
        position_id=1,
        side=1,
        quantity=1.0,
        initial_quantity=1.0,
        entry_price=100.0,
        entry_time=timestamp,
        entry_fee=0.0,
        stop_pct=0.1,
        risk_amount=10.0,
    )


def test_one_minute_replay_processes_tp_then_breakeven_stop_chronologically():
    bars = make_strategy_bars()
    one_minute = make_one_minute_bars()
    engine = Backtester(
        bars,
        make_config(),
        signals=make_signals(bars),
        intrabar_bars=one_minute,
    )
    add_open_position(engine, bars.index[0])

    trades, _ = engine.run()

    assert trades.reason.tolist() == ["PARTIAL_TP_1R", "BREAKEVEN"]
    assert trades.quantity.tolist() == pytest.approx([0.5, 0.5])
    assert trades.exit_price.tolist() == pytest.approx([110.0, 100.0])
    assert trades.exit_time.tolist() == [one_minute.index[5], one_minute.index[6]]
    assert engine.position is None


def test_incomplete_one_minute_window_falls_back_to_strategy_ohlc():
    bars = make_strategy_bars()
    bars.loc[bars.index[1], "low"] = 89.0
    one_minute = make_one_minute_bars().drop(index=pd.Timestamp("2026-01-01T00:09:00Z"))
    engine = Backtester(
        bars,
        make_config(),
        signals=make_signals(bars),
        intrabar_bars=one_minute,
    )
    add_open_position(engine, bars.index[0])

    trades, _ = engine.run()

    assert not engine._intrabar_slices[1][2]
    # La vela agregada contiene stop y TP: el fallback conserva la prioridad
    # previa del stop cuando faltan minutos de la ventana.
    assert trades.reason.tolist() == ["STOP_LOSS_10.00%"]
    assert trades.quantity.tolist() == pytest.approx([1.0])


def test_intrabar_source_must_be_one_minute():
    bars = make_strategy_bars()
    five_minute = bars.copy()

    with pytest.raises(ValueError, match="resolución de 1m"):
        Backtester(
            bars,
            make_config(),
            signals=make_signals(bars),
            intrabar_bars=five_minute,
        )
