"""Cálculo de series y traducción del backtest al formato de eventos live."""

from __future__ import annotations

import contextlib
import io
import json
from dataclasses import replace

import numpy as np
import pandas as pd

from strategy.models import CROSS_REASON, empty_trades
from strategy.strategy import Backtester, Config

from .config import opt_float, ts_ms, utc_iso


def events_to_trades(events: list[dict]) -> list[dict]:
    """Convierte eventos a trades planos ordenados para el gráfico."""
    trades = [
        {
            "event": event["event"],
            "side": event["side"],
            "reason": event["reason"],
            "time": event["signal_time"],
            "bar_time": event["bar_time"],
            "price": event["price"],
            **json.loads(event["payload"]),
        }
        for event in events
    ]
    return sorted(
        trades,
        key=lambda trade: (
            trade["time"],
            trade["bar_time"],
            trade["event"] != "EXIT",
        ),
    )


def _reason_is_intrabar(reason: str) -> bool:
    """Indica las salidas modeladas por precio dentro de una vela OHLC."""
    return reason.startswith(
        ("STOP_LOSS", "PARTIAL_TP", "EMA_SLOW_TOUCH", "CONFIRM_MOVE_AGAINST")
    ) or reason == "BREAKEVEN"


def build_events_and_result(
    bars: pd.DataFrame,
    signals: pd.DataFrame,
    adx_exit: np.ndarray | None,
    weak_exit: np.ndarray | None,
    rsi_div_exit: np.ndarray | None,
    cfg: Config,
    symbol: str,
    interval: str,
) -> tuple[list[dict], pd.DataFrame, pd.DataFrame]:
    """Ejecuta una vez el backtester y devuelve eventos, trades y equity.

    Las intenciones de la última vela se publican para la siguiente apertura,
    sin esperar al cierre de esa vela futura.
    """
    if len(bars) < 2:
        return (
            [],
            empty_trades(),
            pd.DataFrame(
                columns=[
                    "timestamp",
                    "cash",
                    "equity",
                    "position_side",
                    "position_quantity",
                ]
            ),
        )

    engine = Backtester(
        bars,
        replace(cfg, force_close_at_end=False),
        signals,
        adx_exit,
        weak_exit,
        rsi_div_exit,
    )
    with contextlib.redirect_stdout(io.StringIO()):
        backtest_trades, equity = engine.run()

    timeframe_ms = int(pd.Timedelta(cfg.timeframe).total_seconds() * 1000)
    created_at = utc_iso()
    bar_times = bars.index.as_unit("ms").asi8.tolist()
    indicators = dict(
        zip(
            bar_times,
            zip(
                *(signals[column].to_numpy(float)
                  for column in ("ema_fast", "ema_slow", "adx_entry"))
            ),
        )
    )
    closes = dict(zip(bar_times, bars["close"].to_numpy(float)))

    def make_event(
        event: str,
        side: str,
        reason: str,
        bar_time: int,
        signal_time: int,
        price: float | None,
        payload: dict,
    ) -> dict:
        fast, slow, adx_entry = indicators.get(bar_time, (np.nan,) * 3)
        return {
            "symbol": symbol,
            "interval": interval,
            "timeframe": cfg.timeframe,
            "bar_time": bar_time,
            "signal_time": signal_time,
            "event": event,
            "signal": side,
            "side": side,
            "reason": reason,
            "price": opt_float(price),
            "ema_fast": opt_float(fast),
            "ema_slow": opt_float(slow),
            "adx_entry": opt_float(adx_entry),
            "executed": 0,
            "created_at": created_at,
            "payload": json.dumps(payload),
        }

    open_position = engine.position
    entries: dict[int, tuple[str, pd.Timestamp, float]] = {}
    closed_legs: dict[int, list] = {}
    entry_quantities: dict[int, float] = {}
    for trade in engine.trades:
        entries.setdefault(
            trade.position_id,
            (trade.side, trade.entry_time, trade.entry_price),
        )
        closed_legs.setdefault(trade.position_id, []).append(trade)
    if open_position is not None:
        entries.setdefault(
            open_position.position_id,
            (
                "LONG" if open_position.side == 1 else "SHORT",
                open_position.entry_time,
                open_position.entry_price,
            ),
        )
    for opened in engine.opened:
        entries[opened["position_id"]] = (
            "LONG" if opened["side"] == 1 else "SHORT",
            opened["time"],
            opened["price"],
        )
        entry_quantities[opened["position_id"]] = float(opened["quantity"])

    events: list[dict] = []
    for position_id in sorted(entries):
        side, entry_time, entry_price = entries[position_id]
        signal_time = ts_ms(entry_time)
        events.append(
            make_event(
                "ENTRY",
                side,
                "EMA_CROSS",
                signal_time - timeframe_ms,
                signal_time,
                entry_price,
                {
                    "action": "OPEN",
                    "position_id": position_id,
                    "entry_pct": 100.0,
                },
            )
        )

    for position_id in sorted(closed_legs):
        legs = sorted(closed_legs[position_id], key=lambda trade: trade.trade_id)
        initial_quantity = entry_quantities.get(
            position_id, sum(trade.quantity for trade in legs)
        )
        prior_closed_quantity = 0.0
        for leg in legs:
            intrabar = _reason_is_intrabar(leg.reason)
            leg_time = ts_ms(leg.exit_time)
            bar_time = leg_time if intrabar else leg_time - timeframe_ms
            # Solo se cargan velas cerradas: un evento intrabar se observa al cierre.
            signal_time = bar_time + timeframe_ms if intrabar else leg_time
            quantity_before_close = initial_quantity - prior_closed_quantity
            close_pct = (
                100.0 * leg.quantity / quantity_before_close
                if quantity_before_close > 0
                else 100.0
            )
            r_multiple = (
                leg.pnl_net / leg.position_risk if leg.position_risk else None
            )
            event_price = leg.exit_price
            payload = {
                "action": "CLOSE" if close_pct >= 99.999 else "PARTIAL_CLOSE",
                "position_id": position_id,
                "close_pct": close_pct,
                "exit_price": opt_float(leg.exit_price),
                "pnl_net": opt_float(leg.pnl_net),
                "r_multiple": opt_float(r_multiple),
            }
            if intrabar:
                event_price = closes.get(bar_time, leg.exit_price)
                payload.update(
                    {
                        "fill_price_is_simulated": True,
                        "simulated_exit_price": opt_float(leg.exit_price),
                        "pnl_is_simulated": True,
                        "observed_close": opt_float(event_price),
                        "exit_price": opt_float(event_price),
                    }
                )
            events.append(
                make_event(
                    "EXIT",
                    leg.side,
                    leg.reason,
                    bar_time,
                    signal_time,
                    event_price,
                    payload,
                )
            )
            prior_closed_quantity += leg.quantity

    if len(bars):
        last_bar_time = bar_times[-1]
        next_open_time = last_bar_time + timeframe_ms
        if engine.pending_target is not None:
            target = int(engine.pending_target)
            position = engine.position
            if position is not None and position.side != target:
                old_side = "LONG" if position.side == 1 else "SHORT"
                events.append(
                    make_event(
                        "EXIT",
                        old_side,
                        CROSS_REASON,
                        last_bar_time,
                        next_open_time,
                        None,
                        {
                            "action": "CLOSE",
                            "position_id": position.position_id,
                            "close_pct": 100.0,
                            "exit_price": None,
                            "pnl_net": None,
                            "r_multiple": None,
                            "pending": True,
                        },
                    )
                )
            if position is None or position.side != target:
                new_side = "LONG" if target == 1 else "SHORT"
                events.append(
                    make_event(
                        "ENTRY",
                        new_side,
                        "EMA_CROSS",
                        last_bar_time,
                        next_open_time,
                        None,
                        {
                            "action": "OPEN",
                            "position_id": engine.position_id + 1,
                            "entry_pct": 100.0,
                            "pending": True,
                        },
                    )
                )
        elif engine.pending_exit is not None and engine.position is not None:
            reason, close_pct = engine.pending_exit
            side = "LONG" if engine.position.side == 1 else "SHORT"
            events.append(
                make_event(
                    "EXIT",
                    side,
                    reason,
                    last_bar_time,
                    next_open_time,
                    None,
                    {
                        "action": "CLOSE" if close_pct >= 99.999 else "PARTIAL_CLOSE",
                        "position_id": engine.position.position_id,
                        "close_pct": close_pct,
                        "exit_price": None,
                        "pnl_net": None,
                        "r_multiple": None,
                        "pending": True,
                    },
                )
            )

    ordered_events = sorted(
        events,
        key=lambda event: (
            event["signal_time"],
            event["bar_time"],
            event["event"] != "EXIT",
        ),
    )
    return ordered_events, backtest_trades, equity


def build_events(
    bars: pd.DataFrame,
    signals: pd.DataFrame,
    adx_exit: np.ndarray | None,
    weak_exit: np.ndarray | None,
    rsi_div_exit: np.ndarray | None,
    cfg: Config,
    symbol: str,
    interval: str,
) -> list[dict]:
    """Compatibilidad: devuelve solo los eventos live generados."""
    events, _, _ = build_events_and_result(
        bars,
        signals,
        adx_exit,
        weak_exit,
        rsi_div_exit,
        cfg,
        symbol,
        interval,
    )
    return events
