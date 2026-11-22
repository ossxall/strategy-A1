import json

import numpy as np
import pandas as pd
import pytest

from strategy.config import Config
from strategy.models import Position
from strategy.strategy import Backtester
from live.bot.series import resample
from live.bot.events import build_events_and_result
from live.bot.config import ts_ms


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
        "partial_tp2_r": 0.0,
        "partial_tp2_pct": 0.0,
        "move_stop_to_breakeven": True,
        "entry_adx_enabled": False,
        "entry_atr_enabled": False,
        "entry_rsi_divergence_enabled": False,
        "entry_gaussian_trend_enabled": False,
        "adx_exit_enabled": False,
        "exit_rsi_divergence_enabled": False,
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
            "ema_fast": np.full(len(bars), 100.0),
            "ema_slow": np.full(len(bars), 99.0),
            "adx_entry": np.full(len(bars), 30.0),
        },
        index=bars.index,
    )


def add_open_position(engine, timestamp, side=1):
    engine.position = Position(
        position_id=1,
        side=side,
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


def test_stop_has_priority_when_tp_and_stop_are_both_touched_in_one_minute():
    bars = make_strategy_bars()
    one_minute = make_one_minute_bars()
    one_minute.loc[one_minute.index[5], ["high", "low"]] = [111.0, 89.0]
    engine = Backtester(
        bars,
        make_config(),
        signals=make_signals(bars),
        intrabar_bars=one_minute,
    )
    add_open_position(engine, bars.index[0])

    trades, _ = engine.run()

    assert trades.reason.tolist() == ["STOP_LOSS_10.00%"]
    assert trades.exit_price.tolist() == pytest.approx([90.0])


def test_intrabar_executes_all_reached_tps_per_minute_from_nearest_to_farthest():
    bars = make_strategy_bars()
    one_minute = make_one_minute_bars()
    bars.loc[bars.index[1], "high"] = 121.0
    for minute in (5, 6):
        one_minute.iloc[minute, one_minute.columns.get_loc("high")] = 121.0

    engine = Backtester(
        bars,
        make_config(
            partial_tp_pct=20.0,
            partial_tp2_r=1.5,
            partial_tp2_pct=30.0,
            partial_tp3_r=0.0,
            partial_tp3_pct=0.0,
            partial_tp4_r=0.0,
            partial_tp4_pct=0.0,
            partial_tp5_r=0.0,
            partial_tp5_pct=0.0,
            partial_tp6_r=0.0,
            partial_tp6_pct=0.0,
            partial_tp7_r=0.0,
            partial_tp7_pct=0.0,
            partial_tp8_r=0.0,
            partial_tp8_pct=0.0,
            move_stop_to_breakeven=False,
        ),
        signals=make_signals(bars),
        intrabar_bars=one_minute,
    )
    add_open_position(engine, bars.index[0])

    trades, _ = engine.run()

    assert trades.reason.tolist() == ["PARTIAL_TP_1R", "PARTIAL_TP2_1.5R"]
    assert trades.exit_time.tolist() == [one_minute.index[5], one_minute.index[5]]
    assert trades.exit_price.tolist() == pytest.approx([110.0, 115.0])
    assert trades.quantity.tolist() == pytest.approx([0.2, 0.24])


@pytest.mark.parametrize(
    "side,minute_ohlc,expected_tp_price",
    [
        (1, [100.0, 111.0, 99.0, 99.0], 110.0),
        (-1, [100.0, 101.0, 89.0, 101.0], 90.0),
    ],
)
def test_intrabar_does_not_activate_breakeven_if_tp_minute_closes_past_entry(
    side, minute_ohlc, expected_tp_price
):
    bars = make_strategy_bars()
    one_minute = make_one_minute_bars()
    one_minute.loc[one_minute.index[5], ["open", "high", "low", "close"]] = (
        minute_ohlc
    )
    engine = Backtester(
        bars,
        make_config(),
        signals=make_signals(bars),
        intrabar_bars=one_minute,
    )
    add_open_position(engine, bars.index[0], side=side)

    trades, _ = engine.run()

    assert trades.reason.tolist() == ["PARTIAL_TP_1R"]
    assert trades.exit_price.tolist() == pytest.approx([expected_tp_price])
    assert engine.position is not None
    assert engine.position.breakeven is False


def test_intrabar_take_profit_and_breakeven_stop_are_symmetric_for_short():
    bars = make_strategy_bars()
    one_minute = make_one_minute_bars()
    bars.loc[bars.index[1], ["high", "low"]] = [100.0, 89.0]
    one_minute.loc[one_minute.index[5], ["open", "high", "low", "close"]] = [
        100.0,
        100.0,
        89.0,
        91.0,
    ]
    one_minute.loc[one_minute.index[6], ["open", "high", "low", "close"]] = [
        91.0,
        101.0,
        95.0,
        100.0,
    ]
    engine = Backtester(
        bars,
        make_config(),
        signals=make_signals(bars),
        intrabar_bars=one_minute,
    )
    engine.position = Position(
        position_id=1,
        side=-1,
        quantity=1.0,
        initial_quantity=1.0,
        entry_price=100.0,
        entry_time=bars.index[0],
        entry_fee=0.0,
        stop_pct=0.1,
        risk_amount=10.0,
    )

    trades, _ = engine.run()

    assert trades.reason.tolist() == ["PARTIAL_TP_1R", "BREAKEVEN"]
    assert trades.exit_price.tolist() == pytest.approx([90.0, 100.0])
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


def test_live_tail_uses_available_minutes_even_when_feed_has_gaps():
    bars = make_strategy_bars()
    tail_index = pd.DatetimeIndex(
        ["2026-01-01T00:10:00Z", "2026-01-01T00:12:00Z"]
    )
    sparse_tail = pd.DataFrame(
        [
            {"open": 100.0, "high": 111.0, "low": 100.0, "close": 109.0, "volume": 1.0},
            {"open": 109.0, "high": 109.0, "low": 99.0, "close": 101.0, "volume": 1.0},
        ],
        index=tail_index,
    )
    signals = make_signals(bars)
    signals.loc[bars.index[-1], "target"] = 1
    engine = Backtester(
        bars,
        make_config(),
        signals=signals,
        intrabar_bars=sparse_tail,
        process_intrabar_tail=True,
        intrabar_timeframe="1min",
    )

    trades, _ = engine.run()

    assert trades.reason.tolist() == ["PARTIAL_TP_1R", "BREAKEVEN"]
    assert trades.exit_time.tolist() == [tail_index[0], tail_index[1]]


def test_live_tail_replays_available_minutes_without_waiting_for_strategy_candle():
    bars = make_strategy_bars()
    one_minute = make_one_minute_bars()
    tail_index = pd.date_range(
        "2026-01-01T00:10:00Z", periods=2, freq="1min"
    )
    tail = pd.DataFrame(
        [
            {"open": 100.0, "high": 111.0, "low": 100.0, "close": 109.0, "volume": 1.0},
            {"open": 109.0, "high": 109.0, "low": 99.0, "close": 101.0, "volume": 1.0},
        ],
        index=tail_index,
    )
    one_minute = pd.concat([one_minute, tail])
    signals = make_signals(bars)
    signals.loc[bars.index[-1], "target"] = 1
    engine = Backtester(
        bars,
        make_config(),
        signals=signals,
        intrabar_bars=one_minute,
        process_intrabar_tail=True,
    )

    trades, _ = engine.run()

    assert engine.opened[0]["time"] == tail_index[0]
    assert trades.reason.tolist() == ["PARTIAL_TP_1R", "BREAKEVEN"]
    assert trades.exit_time.tolist() == [tail_index[0], tail_index[1]]


def test_intrabar_replay_ignores_unclosed_one_minute_candle():
    bars = make_strategy_bars()
    tail_index = pd.date_range(
        "2026-01-01T00:10:00Z", periods=2, freq="1min"
    )
    tail = pd.DataFrame(
        [
            {
                "open": 100.0,
                "high": 111.0,
                "low": 100.0,
                "close": 109.0,
                "volume": 1.0,
                "is_closed": True,
            },
            {
                "open": 109.0,
                "high": 109.0,
                "low": 89.0,
                "close": 90.0,
                "volume": 1.0,
                "is_closed": False,
            },
        ],
        index=tail_index,
    )
    signals = make_signals(bars)
    signals.loc[bars.index[-1], "target"] = 1
    engine = Backtester(
        bars,
        make_config(),
        signals=signals,
        intrabar_bars=tail,
        process_intrabar_tail=True,
        intrabar_timeframe="1min",
    )

    trades, _ = engine.run()

    assert engine._intrabar_bars.index.tolist() == [tail_index[0]]
    assert trades.reason.tolist() == ["PARTIAL_TP_1R"]
    # La vela abierta habría tocado el stop del remanente; no debe simularse.
    assert engine.position is not None


def test_live_intrabar_events_are_published_at_each_minute_close():
    bars = make_strategy_bars()
    one_minute = make_one_minute_bars()
    tail_index = pd.date_range(
        "2026-01-01T00:10:00Z", periods=2, freq="1min"
    )
    tail = pd.DataFrame(
        [
            {"open": 100.0, "high": 111.0, "low": 100.0, "close": 109.0, "volume": 1.0},
            {"open": 109.0, "high": 109.0, "low": 99.0, "close": 101.0, "volume": 1.0},
        ],
        index=tail_index,
    )
    signals = make_signals(bars)
    signals.loc[bars.index[-1], "target"] = 1

    events, trades, equity = build_events_and_result(
        bars,
        signals,
        None,
        None,
        make_config(),
        "BTCUSDT",
        "1m",
        pd.concat([one_minute, tail]),
    )

    exits = [event for event in events if event["event"] == "EXIT"]
    assert trades.reason.tolist() == ["PARTIAL_TP_1R", "BREAKEVEN"]
    assert [event["reason"] for event in exits] == [
        "PARTIAL_TP_1R",
        "BREAKEVEN",
    ]
    assert [event["signal_time"] for event in exits] == [
        ts_ms(tail_index[0] + pd.Timedelta("1min")),
        ts_ms(tail_index[1] + pd.Timedelta("1min")),
    ]
    assert all(event["bar_time"] == ts_ms(tail_index[0]) for event in exits)
    assert equity.timestamp.iloc[-1] == tail_index[1] + pd.Timedelta("1min")
    first_exit_payload = json.loads(exits[0]["payload"])
    assert exits[0]["price"] == pytest.approx(109.0)
    assert first_exit_payload["observed_close"] == pytest.approx(109.0)
    assert first_exit_payload["simulated_exit_price"] == pytest.approx(
        trades.iloc[0].exit_price
    )
    assert first_exit_payload["pnl_is_simulated"] is True


def test_live_simulation_cache_replays_only_the_growing_tail():
    bars = make_strategy_bars()
    closed_minutes = make_one_minute_bars()
    tail_index = pd.date_range(
        "2026-01-01T00:10:00Z", periods=2, freq="1min"
    )
    tail = pd.DataFrame(
        [
            {"open": 100.0, "high": 111.0, "low": 100.0, "close": 109.0, "volume": 1.0},
            {"open": 109.0, "high": 109.0, "low": 99.0, "close": 101.0, "volume": 1.0},
        ],
        index=tail_index,
    )
    signals = make_signals(bars)
    signals.loc[bars.index[-1], "target"] = 1
    cache = {}
    args = (
        bars,
        signals,
        None,
        None,
        make_config(),
        "BTCUSDT",
        "1m",
    )

    first_events, first_trades, _ = build_events_and_result(
        *args,
        pd.concat([closed_minutes, tail.iloc[:1]]),
        cache,
    )
    baseline = cache["baseline"]
    assert first_trades.reason.tolist() == ["PARTIAL_TP_1R"]

    second_events, second_trades, _ = build_events_and_result(
        *args,
        pd.concat([closed_minutes, tail]),
        cache,
    )

    assert cache["baseline"] is baseline
    assert second_trades.reason.tolist() == ["PARTIAL_TP_1R", "BREAKEVEN"]
    assert [event["reason"] for event in second_events if event["event"] == "EXIT"] == [
        "PARTIAL_TP_1R",
        "BREAKEVEN",
    ]
    first_event_ids = {event["signal_id"] for event in first_events}
    second_event_ids = {event["signal_id"] for event in second_events}
    assert first_event_ids <= second_event_ids
    assert len(
        [event for event in second_events if event["event"] == "ENTRY"]
    ) == 1
    assert len(
        [
            event
            for event in second_events
            if event["event"] == "EXIT" and event["reason"] == "PARTIAL_TP_1R"
        ]
    ) == 1

    # Una corrección al historial completo obliga a reconstruir el checkpoint.
    _, _, _ = build_events_and_result(
        *args,
        pd.concat([closed_minutes, tail]),
        cache,
        history_revision=1,
    )
    assert cache["baseline"] is not baseline


def test_live_cache_rebuild_replays_stop_from_incomplete_strategy_bucket():
    minute_index = pd.date_range(
        "2026-01-01T00:00:00Z", periods=10, freq="1min"
    )
    complete_minutes = pd.DataFrame(
        {
            "open": np.full(10, 100.0),
            "high": np.full(10, 100.0),
            "low": np.full(10, 100.0),
            "close": np.full(10, 100.0),
            "volume": np.ones(10),
        },
        index=minute_index,
    )
    gap_minute = pd.DataFrame(
        [
            {
                "open": 100.0,
                "high": 100.0,
                "low": 89.0,
                "close": 90.0,
                "volume": 1.0,
            }
        ],
        index=pd.DatetimeIndex(["2026-01-01T00:10:00Z"]),
    )
    next_bucket = pd.DataFrame(
        {
            "open": np.full(5, 100.0),
            "high": np.full(5, 100.0),
            "low": np.full(5, 100.0),
            "close": np.full(5, 100.0),
            "volume": np.ones(5),
        },
        index=pd.date_range("2026-01-01T00:15:00Z", periods=5, freq="1min"),
    )
    before_bucket_closes = pd.concat([complete_minutes, gap_minute])
    after_bucket_closes = pd.concat(
        [before_bucket_closes, next_bucket]
    )
    bars_before = resample(
        before_bucket_closes, "5min", source_interval_minutes=1
    )
    bars_after = resample(
        after_bucket_closes, "5min", source_interval_minutes=1
    )
    assert bars_before.index.tolist() == [
        pd.Timestamp("2026-01-01T00:00:00Z"),
        pd.Timestamp("2026-01-01T00:05:00Z"),
    ]
    assert bars_after.index.tolist() == [
        pd.Timestamp("2026-01-01T00:00:00Z"),
        pd.Timestamp("2026-01-01T00:05:00Z"),
        pd.Timestamp("2026-01-01T00:15:00Z"),
    ]

    signals_before = make_signals(bars_before)
    signals_before.iloc[0, signals_before.columns.get_loc("target")] = 1
    signals_after = make_signals(bars_after)
    signals_after.iloc[0, signals_after.columns.get_loc("target")] = 1
    cache = {}
    first_args = (
        bars_before,
        signals_before,
        None,
        None,
        make_config(),
        "BTCUSDT",
        "1m",
        before_bucket_closes,
        cache,
    )

    first_events, first_trades, _ = build_events_and_result(*first_args)
    first_baseline = cache["baseline"]
    assert first_trades.reason.tolist() == ["STOP_LOSS_10.00%"]
    assert first_trades.exit_time.tolist() == [
        pd.Timestamp("2026-01-01T00:10:00Z")
    ]

    second_events, second_trades, _ = build_events_and_result(
        bars_after,
        signals_after,
        None,
        None,
        make_config(),
        "BTCUSDT",
        "1m",
        after_bucket_closes,
        cache,
    )

    assert cache["baseline"] is not first_baseline
    assert second_trades.reason.tolist() == ["STOP_LOSS_10.00%"]
    assert second_trades.exit_time.tolist() == [
        pd.Timestamp("2026-01-01T00:10:00Z")
    ]
    first_exit_ids = {
        event["signal_id"]
        for event in first_events
        if event["event"] == "EXIT"
    }
    second_exit_ids = {
        event["signal_id"]
        for event in second_events
        if event["event"] == "EXIT"
    }
    assert first_exit_ids == second_exit_ids
