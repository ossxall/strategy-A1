"""Offline scenario checks for the USDⓈ-M executor test mode."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from ..config import resolve_path
from ..signal_contract import (
    STOP_ACTION_CANCEL,
    STOP_ACTION_KEEP,
    STOP_ACTION_MOVE_TO_BREAKEVEN,
)
from ..storage import SIGNAL_SCHEMA_VERSION, SignalStore
from .binance_usdm import BinanceOrderNotFound
from .worker import ExecutionWorker, ExecutorSettings


class _ScenarioAPIError(RuntimeError):
    pass


class _ScenarioFuturesGateway:
    """Deterministic USDⓈ-M simulator; never makes network requests."""

    def __init__(self, *, dual_side: bool = False, position_amt: float = 0.0):
        self.dual_side = dual_side
        self.position_amt = position_amt
        self.entry_price = 100.0
        self.mark = 100.0
        self.fail_next_mark_price = False
        self.leverage = 1
        self.regular_orders: list[dict] = []
        self.orders: list[dict] = []
        self.order_results: dict[str, dict] = {}
        self.orders_by_id: dict[int, dict] = {}
        self.stops: dict[str, dict] = {}
        self.next_order_id = 1
        self.fail_next_breakeven = False

    def position_mode(self):
        return {"dualSidePosition": self.dual_side}

    def exchange_information(self):
        return {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                        {
                            "filterType": "MARKET_LOT_SIZE",
                            "stepSize": "0.001",
                            "minQty": "0.001",
                            "maxQty": "1000",
                        },
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }
            ]
        }

    def account_information(self):
        return {
            "totalMarginBalance": "1000",
            "availableBalance": "1000",
        }

    def account_balance(self):
        return [
            {"asset": "USDT", "balance": "1000", "availableBalance": "1000"}
        ]

    def positions(self, symbol=None):
        if not self.position_amt:
            return []
        return [
            {
                "symbol": "BTCUSDT",
                "positionAmt": str(self.position_amt),
                "entryPrice": str(self.entry_price),
                "markPrice": str(self.mark),
                "unRealizedProfit": "0",
                "leverage": str(self.leverage),
            }
        ]

    def open_orders(self, symbol):
        return [dict(order) for order in self.regular_orders]

    def open_algo_orders(self, symbol):
        return [
            dict(order)
            for order in self.stops.values()
            if order["algoStatus"] == "NEW"
        ]

    def mark_price(self, symbol):
        if self.fail_next_mark_price:
            self.fail_next_mark_price = False
            raise _ScenarioAPIError("temporary mark-price failure")
        return {"symbol": symbol, "markPrice": str(self.mark)}

    def symbol_configuration(self, symbol):
        return {"symbol": symbol, "leverage": self.leverage}

    def change_leverage(self, symbol, leverage):
        self.leverage = leverage
        return {"symbol": symbol, "leverage": leverage}

    def place_market_order(
        self, *, symbol, side, quantity, client_order_id, reduce_only
    ):
        qty = float(quantity)
        if reduce_only:
            if side == "SELL" and self.position_amt > 0:
                self.position_amt = max(0.0, self.position_amt - qty)
            elif side == "BUY" and self.position_amt < 0:
                self.position_amt = min(0.0, self.position_amt + qty)
            else:
                raise AssertionError("Reduce-only order would not reduce position")
        elif self.position_amt == 0:
            self.position_amt = qty if side == "BUY" else -qty
            self.entry_price = self.mark
        else:
            raise AssertionError("Simulation refuses to increase/reverse position")

        self.orders.append(
            {
                "symbol": symbol,
                "side": side,
                "quantity": qty,
                "client_order_id": client_order_id,
                "reduce_only": reduce_only,
            }
        )
        result = {
            "status": "FILLED",
            "orderId": self.next_order_id,
            "clientOrderId": client_order_id,
            "executedQty": str(qty),
            "avgPrice": str(self.mark),
        }
        self.next_order_id += 1
        self.order_results[client_order_id] = result
        self.orders_by_id[int(result["orderId"])] = result
        return result

    def query_order(self, symbol, client_order_id=None, order_id=None):
        if client_order_id is not None:
            result = self.order_results.get(client_order_id)
            if result is None:
                raise BinanceOrderNotFound(client_order_id)
            return result
        result = self.orders_by_id.get(int(order_id))
        if result is None:
            raise BinanceOrderNotFound(str(order_id))
        return result

    def place_protective_stop(
        self, *, symbol, side, trigger_price, client_algo_id
    ):
        if client_algo_id.startswith("b1be_") and self.fail_next_breakeven:
            self.fail_next_breakeven = False
            raise _ScenarioAPIError(
                "(-2021, 'Order would immediately trigger.')"
            )
        if any(
            stop["side"] == side and stop["algoStatus"] == "NEW"
            for stop in self.stops.values()
        ):
            raise _ScenarioAPIError(
                "(-4130, 'An open closePosition order already exists.')"
            )
        stop = {
            "symbol": symbol,
            "side": side,
            "triggerPrice": trigger_price,
            "closePosition": "true",
            "workingType": "MARK_PRICE",
            "type": "STOP_MARKET",
            "clientAlgoId": client_algo_id,
            "algoStatus": "NEW",
        }
        self.stops[client_algo_id] = stop
        return stop

    def query_protective_stop(self, client_algo_id):
        stop = self.stops.get(client_algo_id)
        if stop is None:
            raise BinanceOrderNotFound(client_algo_id)
        return stop

    def cancel_protective_stop(self, client_algo_id):
        stop = self.stops.get(client_algo_id)
        if stop is None or stop["algoStatus"] != "NEW":
            raise BinanceOrderNotFound(client_algo_id)
        stop["algoStatus"] = "CANCELED"
        return stop

    def trigger_stop(self, client_algo_id):
        stop = self.stops[client_algo_id]
        if self.position_amt == 0:
            raise AssertionError("No position to close with simulated stop")
        qty = abs(self.position_amt)
        side = "SELL" if self.position_amt > 0 else "BUY"
        child_id = self.next_order_id
        self.next_order_id += 1
        result = {
            "status": "FILLED",
            "orderId": child_id,
            "executedQty": str(qty),
            "avgPrice": str(self.mark),
        }
        self.orders_by_id[child_id] = result
        self.position_amt = 0.0
        stop["algoStatus"] = "TRIGGERED"
        stop["actualOrderId"] = child_id
        self.orders.append(
            {
                "symbol": "BTCUSDT",
                "side": side,
                "quantity": qty,
                "reduce_only": True,
                "source": "simulated-stop",
            }
        )


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(f"executor self-test failed: {message}")


def _settings(signal_db: Path, account_id: str) -> ExecutorSettings:
    return ExecutorSettings(
        enabled=True,
        test_mode=False,
        environment="testnet",
        allow_live_trading=False,
        account_id=account_id,
        api_key="offline-self-test",
        api_secret="offline-self-test",
        symbol="BTCUSDT",
        poll_seconds=1.0,
        account_poll_seconds=1.0,
        lease_seconds=30,
        max_entry_age_seconds=90,
        initial_backfill_seconds=300,
        leverage=10,
        heartbeat_path=None,
        signal_db=signal_db,
    )


def _signal(
    now_ms: int,
    *,
    event: str,
    action: str,
    side: str,
    position_id: int,
    reason: str,
    close_pct: float | None = None,
    signal_time: int | None = None,
    move_stop_to_breakeven: bool = True,
    scheduled: bool = False,
    bar_time: int | None = None,
) -> dict:
    sizing = {
        "risk_pct": 0.001,
        "stop_loss_pct": 0.01,
        "commission_bps": 0.0,
        "slippage_bps": 0.0,
        "move_stop_to_breakeven": move_stop_to_breakeven,
    }
    payload = {
        "action": action,
        "position_id": position_id,
        "sizing": sizing,
    }
    if event == "ENTRY":
        payload["entry_pct"] = 100.0
    else:
        payload["close_pct"] = close_pct
    if scheduled:
        payload["pending"] = True
    timestamp = now_ms if signal_time is None else signal_time
    return {
        "schema_version": SIGNAL_SCHEMA_VERSION,
        "symbol": "BTCUSDT",
        "interval": "selftest",
        "timeframe": "selftest",
        "bar_time": timestamp if bar_time is None else bar_time,
        "signal_time": timestamp,
        "event": event,
        "action": action,
        "signal": side,
        "side": side,
        "position_id": position_id,
        "scheduled": scheduled,
        "reason": reason,
        "price": None,
        "ema_fast": None,
        "ema_slow": None,
        "adx_entry": None,
        "executed": 0,
        "created_at": "2026-01-01T00:00:00+00:00",
        "payload": payload,
    }


def _run_signal(
    worker: ExecutionWorker, store: SignalStore, event: dict
) -> dict:
    normalized = store._normalize_event(event)
    if store.insert([event]) != 1:
        raise AssertionError("self-test signal was not inserted")
    worker.run_once()
    state = store.get_execution_state(
        worker.settings.account_id, normalized["signal_id"]
    )
    if state is None or state["status"] != "FILLED":
        position = worker._account_snapshot(force=True).get("positions")
        active = store.get_active_execution_positions(
            worker.settings.account_id, worker.settings.symbol
        )
        raise AssertionError(
            f"self-test {event['event']}/{event['action']} state: {state}; "
            f"exchange positions={position}; ledger positions={active}"
        )
    return state


def _open_worker(
    path: Path, account_id: str, gateway, now_ms: int
):
    store = SignalStore(path)
    store.connect()
    clock = [now_ms]
    worker = ExecutionWorker(
        _settings(path, account_id),
        store,
        gateway=gateway,
        clock_ms=lambda: clock[0],
    )
    worker._initialize()
    return store, worker, clock


def _self_test_lifecycles(root: Path, now_ms: int) -> int:
    passed = 0
    path = root / "lifecycles.db"
    gateway = _ScenarioFuturesGateway()
    store, worker, _clock = _open_worker(
        path, "selftest-lifecycles", gateway, now_ms
    )
    try:
        for (
            position_id,
            side,
            favorable_mark,
            move_stop_to_breakeven,
            expect_breakeven,
        ) in (
            (1, "LONG", 101.0, True, True),
            (2, "SHORT", 99.0, True, True),
            (3, "LONG", 99.5, True, False),
            (4, "LONG", 101.0, False, False),
        ):
            gateway.mark = 100.0
            entry = _run_signal(
                worker,
                store,
                _signal(
                    now_ms,
                    event="ENTRY",
                    action="OPEN",
                    side=side,
                    position_id=position_id,
                    reason="SELFTEST_ENTRY",
                    move_stop_to_breakeven=move_stop_to_breakeven,
                ),
            )
            _check(entry["filled_qty"] > 0, f"{side} entry fill")
            _check(not gateway.orders[-1]["reduce_only"], f"{side} entry side")
            gateway.mark = favorable_mark
            partial = _run_signal(
                worker,
                store,
                _signal(
                    now_ms,
                    event="EXIT",
                    action="PARTIAL_CLOSE",
                    side=side,
                    position_id=position_id,
                    reason="PARTIAL_TP_1R",
                    close_pct=50.0,
                    move_stop_to_breakeven=move_stop_to_breakeven,
                ),
            )
            _check(gateway.orders[-1]["reduce_only"], f"{side} partial reduceOnly")
            stop_variant = "be" if expect_breakeven else "sl"
            active_stops = gateway.open_algo_orders("BTCUSDT")
            _check(
                len(active_stops) == 1
                and active_stops[0]["clientAlgoId"]
                == worker._algo_order_id(position_id, stop_variant),
                f"{side} appropriate protective stop remains",
            )
            if move_stop_to_breakeven and not expect_breakeven:
                _check(
                    "breakeven_stop_skipped" in partial["result"],
                    f"{side} unsafe breakeven is skipped",
                )
            _run_signal(
                worker,
                store,
                _signal(
                    now_ms,
                    event="EXIT",
                    action="PARTIAL_CLOSE",
                    side=side,
                    position_id=position_id,
                    reason="PARTIAL_EXIT_TEST",
                    close_pct=50.0,
                    move_stop_to_breakeven=move_stop_to_breakeven,
                ),
            )
            _check(
                gateway.orders[-1]["reduce_only"],
                f"{side} non-TP partial reduceOnly",
            )
            active_stops = gateway.open_algo_orders("BTCUSDT")
            _check(
                len(active_stops) == 1
                and active_stops[0]["clientAlgoId"]
                == worker._algo_order_id(position_id, stop_variant),
                f"{side} non-TP partial preserves its protective stop",
            )
            close = _run_signal(
                worker,
                store,
                _signal(
                    now_ms,
                    event="EXIT",
                    action="CLOSE",
                    side=side,
                    position_id=position_id,
                    reason="SELFTEST_CLOSE",
                    close_pct=100.0,
                    move_stop_to_breakeven=move_stop_to_breakeven,
                ),
            )
            _check(close["status"] == "FILLED", f"{side} full close")
            _check(gateway.position_amt == 0, f"{side} position flat")
            _check(
                not gateway.open_algo_orders("BTCUSDT"),
                f"{side} all protective stops canceled",
            )
            worker._account_snapshot(force=True)
            passed += 1
    finally:
        store.close()
    return passed


def _self_test_signal_reason_matrix(root: Path, now_ms: int) -> int:
    """Exercises each producer exit family through the shared worker."""
    cases = (
        ("PARTIAL_TP_1R", "LONG", "PARTIAL_CLOSE", "TAKE_PROFIT_1",
         STOP_ACTION_MOVE_TO_BREAKEVEN),
        ("PARTIAL_TP2_1.5R", "LONG", "PARTIAL_CLOSE", "TAKE_PROFIT_2",
         STOP_ACTION_KEEP),
        ("PARTIAL_TP3_2R", "LONG", "PARTIAL_CLOSE", "TAKE_PROFIT_3",
         STOP_ACTION_KEEP),
        ("PARTIAL_TP4_2.5R", "LONG", "PARTIAL_CLOSE", "TAKE_PROFIT_4",
         STOP_ACTION_KEEP),
        ("PARTIAL_TP5_3R", "LONG", "PARTIAL_CLOSE", "TAKE_PROFIT_5",
         STOP_ACTION_KEEP),
        ("PARTIAL_TP6_3.5R", "LONG", "PARTIAL_CLOSE", "TAKE_PROFIT_6",
         STOP_ACTION_KEEP),
        ("PARTIAL_TP7_4R", "LONG", "PARTIAL_CLOSE", "TAKE_PROFIT_7",
         STOP_ACTION_KEEP),
        ("EMA_WEAKENING_4H_1D_50%", "LONG", "PARTIAL_CLOSE",
         "EMA_WEAKENING", STOP_ACTION_KEEP),
        ("ADX_1D_REVERSAL_50%", "LONG", "PARTIAL_CLOSE",
         "ADX_REVERSAL", STOP_ACTION_KEEP),
        ("RSI_DIVERGENCE_BEAR_50%", "LONG", "PARTIAL_CLOSE",
         "RSI_DIVERGENCE_BEAR", STOP_ACTION_KEEP),
        ("RSI_DIVERGENCE_BULL", "SHORT", "CLOSE",
         "RSI_DIVERGENCE_BULL", STOP_ACTION_CANCEL),
        ("GAUSSIAN_TREND_BEAR_25%", "LONG", "PARTIAL_CLOSE",
         "GAUSSIAN_TREND_BEAR", STOP_ACTION_KEEP),
        ("GAUSSIAN_TREND_BULL", "SHORT", "CLOSE",
         "GAUSSIAN_TREND_BULL", STOP_ACTION_CANCEL),
        ("STOP_LOSS_1.00%", "LONG", "CLOSE", "STOP_LOSS",
         STOP_ACTION_CANCEL),
        ("BREAKEVEN", "LONG", "CLOSE", "BREAKEVEN", STOP_ACTION_CANCEL),
        ("EMA_CROSS_EXIT", "LONG", "CLOSE", "EMA_CROSS_EXIT",
         STOP_ACTION_CANCEL),
        ("EMA_SLOW_TOUCH_SHORT_AFTER_TP3", "SHORT", "CLOSE",
         "EMA_SLOW_TOUCH", STOP_ACTION_CANCEL),
        ("MONTHLY_LOSS_LIMIT_5%", "LONG", "CLOSE", "MONTHLY_LOSS_LIMIT",
         STOP_ACTION_CANCEL),
    )
    for index, (reason, side, action, reason_code, stop_action) in enumerate(
        cases,
        start=50,
    ):
        gateway = _ScenarioFuturesGateway()
        path = root / f"reason-matrix-{index}.db"
        store, worker, _clock = _open_worker(
            path, f"selftest-reason-{index}", gateway, now_ms
        )
        try:
            position_id = index
            _run_signal(
                worker,
                store,
                _signal(
                    now_ms,
                    event="ENTRY",
                    action="OPEN",
                    side=side,
                    position_id=position_id,
                    reason="EMA_CROSS",
                ),
            )
            gateway.mark = 101.0 if side == "LONG" else 99.0
            close_pct = 50.0 if action == "PARTIAL_CLOSE" else 100.0
            exit_event = _signal(
                now_ms,
                event="EXIT",
                action=action,
                side=side,
                position_id=position_id,
                reason=reason,
                close_pct=close_pct,
            )
            normalized = store._normalize_event(exit_event)
            payload = normalized["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            _check(
                payload["reason_code"] == reason_code,
                f"{reason} stable reason code",
            )
            _check(
                payload["stop_action"] == stop_action,
                f"{reason} declared stop action",
            )
            state = _run_signal(worker, store, exit_event)
            _check(
                state["status"] == "FILLED",
                f"{reason} executes through worker",
            )
            if action == "PARTIAL_CLOSE":
                expected_variant = (
                    "be"
                    if stop_action == STOP_ACTION_MOVE_TO_BREAKEVEN
                    else "sl"
                )
                expected_stop = worker._algo_order_id(
                    position_id, expected_variant
                )
                active_stops = gateway.open_algo_orders("BTCUSDT")
                _check(
                    len(active_stops) == 1
                    and active_stops[0]["clientAlgoId"] == expected_stop,
                    f"{reason} leaves the declared protective stop",
                )
            else:
                _check(
                    gateway.position_amt == 0
                    and not gateway.open_algo_orders("BTCUSDT"),
                    f"{reason} closes and removes protection",
                )
        finally:
            store.close()
    return len(cases)


def _self_test_pending_reversal(root: Path, now_ms: int) -> int:
    """Executes the producer's scheduled EXIT→ENTRY reversal through the worker."""
    path = root / "pending-reversal.db"
    gateway = _ScenarioFuturesGateway()
    store, worker, clock = _open_worker(
        path, "selftest-pending-reversal", gateway, now_ms
    )
    try:
        _run_signal(
            worker,
            store,
            _signal(
                now_ms,
                event="ENTRY",
                action="OPEN",
                side="LONG",
                position_id=1,
                reason="EMA_CROSS",
            ),
        )
        due_partial_ms = now_ms + 1_000
        pending_partial = _signal(
            now_ms,
            event="EXIT",
            action="PARTIAL_CLOSE",
            side="LONG",
            position_id=1,
            reason="EMA_WEAKENING_30MIN_50%",
            close_pct=50.0,
            signal_time=due_partial_ms,
            scheduled=True,
            bar_time=now_ms,
        )
        partial_signal = store._normalize_event(pending_partial)
        _check(
            store.insert([pending_partial]) == 1,
            "scheduled partial exit is persisted",
        )
        order_count = len(gateway.orders)
        worker.run_once()
        _check(
            len(gateway.orders) == order_count,
            "scheduled partial waits until signal_time",
        )
        clock[0] = due_partial_ms
        worker.run_once()
        partial_state = store.get_execution_state(
            worker.settings.account_id, partial_signal["signal_id"]
        )
        _check(
            partial_state is not None and partial_state["status"] == "FILLED",
            "scheduled partial exit is executed",
        )
        _check(
            gateway.orders[-1]["reduce_only"],
            "scheduled partial exit is reduce-only",
        )

        due_ms = due_partial_ms + 1_000
        pending_exit = _signal(
            now_ms,
            event="EXIT",
            action="CLOSE",
            side="LONG",
            position_id=1,
            reason="EMA_CROSS_EXIT",
            close_pct=100.0,
            signal_time=due_ms,
            scheduled=True,
            bar_time=now_ms,
        )
        pending_entry = _signal(
            now_ms,
            event="ENTRY",
            action="OPEN",
            side="SHORT",
            position_id=2,
            reason="EMA_CROSS",
            signal_time=due_ms,
            scheduled=True,
            bar_time=now_ms,
        )
        exit_signal = store._normalize_event(pending_exit)
        entry_signal = store._normalize_event(pending_entry)
        _check(
            store.insert([pending_entry, pending_exit]) == 2,
            "scheduled reversal signals are persisted",
        )

        order_count = len(gateway.orders)
        worker.run_once()
        _check(
            len(gateway.orders) == order_count,
            "scheduled signals wait until signal_time",
        )
        _check(
            store.get_execution_state(
                worker.settings.account_id, exit_signal["signal_id"]
            ) is None
            and store.get_execution_state(
                worker.settings.account_id, entry_signal["signal_id"]
            ) is None,
            "future signals remain unclaimed",
        )

        clock[0] = due_ms
        worker.run_once()
        exit_state = store.get_execution_state(
            worker.settings.account_id, exit_signal["signal_id"]
        )
        _check(
            exit_state is not None and exit_state["status"] == "FILLED",
            "scheduled reversal closes the old position first",
        )
        _check(
            store.get_execution_state(
                worker.settings.account_id, entry_signal["signal_id"]
            ) is None,
            "scheduled reversal entry waits for its exit",
        )
        worker.run_once()
        entry_state = store.get_execution_state(
            worker.settings.account_id, entry_signal["signal_id"]
        )
        _check(
            entry_state is not None and entry_state["status"] == "FILLED",
            "scheduled reversal opens the new position second",
        )
        _check(
            [order["side"] for order in gateway.orders] == [
                "BUY",
                "SELL",
                "SELL",
                "SELL",
            ],
            "reversal market orders use the expected sides",
        )
        _check(
            [order["reduce_only"] for order in gateway.orders] == [
                False,
                True,
                True,
                False,
            ],
            "reversal exit is reduce-only and new entry is not",
        )

        close = _signal(
            due_ms,
            event="EXIT",
            action="CLOSE",
            side="SHORT",
            position_id=2,
            reason="SELFTEST_CLOSE",
            close_pct=100.0,
        )
        _run_signal(worker, store, close)
        _check(gateway.position_amt == 0, "reversed position closes to flat")
        _check(
            not gateway.open_algo_orders("BTCUSDT"),
            "reversal leaves no protective stops open",
        )
    finally:
        store.close()
    return 1


def _self_test_stop_reconciliation(root: Path, now_ms: int) -> int:
    path = root / "stop-reconciliation.db"
    gateway = _ScenarioFuturesGateway()
    store, worker, _clock = _open_worker(
        path, "selftest-stop-reconciliation", gateway, now_ms
    )
    try:
        _run_signal(
            worker,
            store,
            _signal(
                now_ms,
                event="ENTRY",
                action="OPEN",
                side="LONG",
                position_id=3,
                reason="SELFTEST_ENTRY",
            ),
        )
        gateway.mark = 98.0
        gateway.trigger_stop(worker._algo_order_id(3, "sl"))
        snapshot = worker._account_snapshot(force=True)
        worker._reconcile_protective_stops(snapshot)
        _check(
            store.get_active_execution_positions(
                worker.settings.account_id, "BTCUSDT"
            ) == [],
            "triggered stop reconciles ledger to flat",
        )
        adjustment_count = store.conn.execute(
            "SELECT COUNT(*) FROM execution_adjustments"
        ).fetchone()[0]
        worker._reconcile_protective_stops(
            worker._account_snapshot(force=True)
        )
        _check(
            store.conn.execute(
                "SELECT COUNT(*) FROM execution_adjustments"
            ).fetchone()[0] == adjustment_count,
            "stop reconciliation is idempotent",
        )
    finally:
        store.close()
    return 1


def _self_test_inflight_recovery(root: Path, now_ms: int) -> int:
    path = root / "inflight-recovery.db"
    gateway = _ScenarioFuturesGateway()
    store, worker, _clock = _open_worker(
        path, "selftest-inflight", gateway, now_ms
    )
    try:
        event = _signal(
            now_ms,
            event="ENTRY",
            action="OPEN",
            side="LONG",
            position_id=4,
            reason="SELFTEST_ENTRY",
        )
        normalized = store._normalize_event(event)
        store.insert([event])
        claimed = store.claim_next_signal(
            worker.settings.account_id,
            worker.worker_id,
            now_ms,
            worker.settings.lease_seconds * 1000,
            activation_cutoff_ms=worker._activation_cutoff_ms,
        )
        _check(claimed is not None, "inflight signal claimed")
        client_order_id = worker._client_order_id(normalized["signal_id"])
        intent = {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": "1",
            "reduce_only": False,
        }
        store.record_order_intent(
            worker.settings.account_id,
            normalized["signal_id"],
            client_order_id,
            "SUBMITTING",
            intent,
        )
        gateway.place_market_order(
            symbol="BTCUSDT",
            side="BUY",
            quantity="1",
            client_order_id=client_order_id,
            reduce_only=False,
        )
        order_count = len(gateway.orders)
        blocked = worker._reconcile_inflight()
        state = store.get_execution_state(
            worker.settings.account_id, normalized["signal_id"]
        )
        _check(not blocked, "known fill reconciles")
        _check(state["status"] == "FILLED", "known fill state persisted")
        _check(len(gateway.orders) == order_count, "recovery does not resubmit")
    finally:
        store.close()

    uncertain_path = root / "uncertain-submission.db"
    uncertain_gateway = _ScenarioFuturesGateway()
    uncertain_store, uncertain_worker, _clock = _open_worker(
        uncertain_path, "selftest-uncertain", uncertain_gateway, now_ms
    )
    try:
        event = _signal(
            now_ms,
            event="ENTRY",
            action="OPEN",
            side="LONG",
            position_id=6,
            reason="SELFTEST_UNCERTAIN",
        )
        normalized = uncertain_store._normalize_event(event)
        uncertain_store.insert([event])
        claimed = uncertain_store.claim_next_signal(
            uncertain_worker.settings.account_id,
            uncertain_worker.worker_id,
            now_ms,
            uncertain_worker.settings.lease_seconds * 1000,
            activation_cutoff_ms=uncertain_worker._activation_cutoff_ms,
        )
        _check(claimed is not None, "uncertain signal claimed")
        client_order_id = uncertain_worker._client_order_id(
            normalized["signal_id"]
        )
        uncertain_store.record_order_intent(
            uncertain_worker.settings.account_id,
            normalized["signal_id"],
            client_order_id,
            "SUBMITTING",
            {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "quantity": "1",
                "reduce_only": False,
            },
        )
        blocked = uncertain_worker._reconcile_inflight()
        _check(blocked, "missing SUBMITTING order blocks retries")
        _check(not uncertain_gateway.orders, "uncertain order is not resent")
    finally:
        uncertain_store.close()
    return 2


def _self_test_guards(root: Path, now_ms: int) -> int:
    passed = 0
    path = root / "guards.db"
    gateway = _ScenarioFuturesGateway()
    store, worker, _clock = _open_worker(
        path, "selftest-guards", gateway, now_ms
    )
    try:
        stale = _signal(
            now_ms,
            event="ENTRY",
            action="OPEN",
            side="LONG",
            position_id=5,
            reason="SELFTEST_STALE",
            signal_time=now_ms - 120_000,
        )
        normalized = store._normalize_event(stale)
        store.insert([stale])
        worker.run_once()
        state = store.get_execution_state(
            worker.settings.account_id, normalized["signal_id"]
        )
        _check(state["status"] == "EXPIRED", "stale entry expires")
        _check(not gateway.orders, "stale entry sends no order")
        passed += 1

        gateway.position_amt = 0.25
        worker._last_account_poll = 0
        untracked_exit = _signal(
            now_ms,
            event="EXIT",
            action="CLOSE",
            side="LONG",
            position_id=99,
            reason="SELFTEST_UNTRACKED_EXIT",
            close_pct=100.0,
        )
        normalized_exit = store._normalize_event(untracked_exit)
        store.insert([untracked_exit])
        order_count = len(gateway.orders)
        worker.run_once()
        _check(
            len(gateway.orders) == order_count,
            "untracked real position is never closed",
        )
        _check(
            store.get_execution_state(
                worker.settings.account_id, normalized_exit["signal_id"]
            ) is None,
            "untracked position blocks signal claim",
        )
        passed += 1
    finally:
        store.close()

    hedge_gateway = _ScenarioFuturesGateway(dual_side=True)
    hedge_store = SignalStore(root / "hedge.db")
    hedge_store.connect()
    try:
        hedge_worker = ExecutionWorker(
            _settings(root / "hedge.db", "selftest-hedge"),
            hedge_store,
            gateway=hedge_gateway,
            clock_ms=lambda: now_ms,
        )
        try:
            hedge_worker._initialize()
        except RuntimeError as exc:
            _check("Hedge Mode" in str(exc), "Hedge Mode is rejected")
        else:
            raise AssertionError("executor self-test failed: Hedge Mode accepted")
        passed += 1
    finally:
        hedge_store.close()
    return passed


def _self_test_open_orders_and_stop_repair(root: Path, now_ms: int) -> int:
    passed = 0
    for kind in ("market", "conditional"):
        path = root / f"unreconciled-{kind}.db"
        gateway = _ScenarioFuturesGateway()
        if kind == "market":
            gateway.regular_orders.append(
                {"symbol": "BTCUSDT", "orderId": 900, "status": "NEW"}
            )
        else:
            gateway.stops["external-stop"] = {
                "symbol": "BTCUSDT",
                "side": "SELL",
                "triggerPrice": "99",
                "clientAlgoId": "external-stop",
                "algoStatus": "NEW",
            }
        store, worker, _clock = _open_worker(
            path, f"selftest-open-{kind}", gateway, now_ms
        )
        try:
            event = _signal(
                now_ms,
                event="ENTRY",
                action="OPEN",
                side="LONG",
                position_id=20 + passed,
                reason="SELFTEST_OPEN_ORDER_GUARD",
            )
            store.insert([event])
            worker.run_once()
            _check(
                not gateway.orders,
                f"unreconciled {kind} order blocks ENTRY",
            )
            _check(
                store.conn.execute(
                    "SELECT COUNT(*) FROM signal_execution"
                ).fetchone()[0] == 0,
                f"unreconciled {kind} order leaves signal unclaimed",
            )
            passed += 1
        finally:
            store.close()

    path = root / "restore-protection.db"
    gateway = _ScenarioFuturesGateway(position_amt=1.0)
    store, worker, _clock = _open_worker(
        path, "selftest-restore-protection", gateway, now_ms
    )
    try:
        event = _signal(
            now_ms,
            event="ENTRY",
            action="OPEN",
            side="LONG",
            position_id=30,
            reason="SELFTEST_ENTRY",
        )
        normalized = store._normalize_event(event)
        store.insert([event])
        claimed = store.claim_next_signal(
            worker.settings.account_id,
            worker.worker_id,
            now_ms,
            worker.settings.lease_seconds * 1000,
            activation_cutoff_ms=worker._activation_cutoff_ms,
        )
        _check(claimed is not None, "entry with active position is claimed")
        store.update_execution(
            worker.settings.account_id,
            normalized["signal_id"],
            "FILLED",
            order_status="FILLED",
            filled_qty=1.0,
            average_fill_price=100.0,
        )
        worker.run_once()
        _check(
            len(gateway.open_algo_orders("BTCUSDT")) == 1,
            "missing protective stop is restored",
        )
        stop_id = next(iter(gateway.stops))
        gateway.stops[stop_id]["triggerPrice"] = "90"
        bad_snapshot = worker._account_snapshot(force=True)
        _check(
            "activación" in (worker._account_consistency_error(bad_snapshot) or ""),
            "incorrect protective stop trigger is detected",
        )
        passed += 1
    finally:
        store.close()

    path = root / "restore-breakeven.db"
    gateway = _ScenarioFuturesGateway(position_amt=0.5)
    gateway.mark = 101.0
    store, worker, _clock = _open_worker(
        path, "selftest-restore-breakeven", gateway, now_ms
    )
    try:
        entry_event = _signal(
            now_ms,
            event="ENTRY",
            action="OPEN",
            side="LONG",
            position_id=31,
            reason="SELFTEST_ENTRY",
        )
        normalized_entry = store._normalize_event(entry_event)
        store.insert([entry_event])
        store.claim_next_signal(
            worker.settings.account_id,
            worker.worker_id,
            now_ms,
            worker.settings.lease_seconds * 1000,
            activation_cutoff_ms=worker._activation_cutoff_ms,
        )
        store.update_execution(
            worker.settings.account_id,
            normalized_entry["signal_id"],
            "FILLED",
            order_status="FILLED",
            filled_qty=1.0,
            average_fill_price=100.0,
        )
        partial_event = _signal(
            now_ms,
            event="EXIT",
            action="PARTIAL_CLOSE",
            side="LONG",
            position_id=31,
            reason="PARTIAL_TP_1R",
            close_pct=50.0,
        )
        normalized_partial = store._normalize_event(partial_event)
        store.insert([partial_event])
        store.claim_next_signal(
            worker.settings.account_id,
            worker.worker_id,
            now_ms,
            worker.settings.lease_seconds * 1000,
            activation_cutoff_ms=worker._activation_cutoff_ms,
        )
        store.update_execution(
            worker.settings.account_id,
            normalized_partial["signal_id"],
            "FILLED",
            order_status="FILLED",
            filled_qty=0.5,
            average_fill_price=101.0,
            result={
                "protective_stop_client_algo_id":
                    worker._algo_order_id(31, "be"),
            },
        )
        worker.run_once()
        active_stops = gateway.open_algo_orders("BTCUSDT")
        _check(
            len(active_stops) == 1
            and active_stops[0]["clientAlgoId"]
            == worker._algo_order_id(31, "be")
            and float(active_stops[0]["triggerPrice"]) == 100.0,
            "missing breakeven stop is restored from the confirmed TP1",
        )
        passed += 1
    finally:
        store.close()
    return passed


def _self_test_transient_and_terminal_partial_recovery(
    root: Path, now_ms: int
) -> int:
    passed = 0
    path = root / "transient-before-submit.db"
    gateway = _ScenarioFuturesGateway()
    gateway.fail_next_mark_price = True
    store, worker, _clock = _open_worker(
        path, "selftest-transient-before-submit", gateway, now_ms
    )
    try:
        event = _signal(
            now_ms,
            event="ENTRY",
            action="OPEN",
            side="LONG",
            position_id=40,
            reason="SELFTEST_RETRYABLE_MARK_FAILURE",
        )
        normalized = store._normalize_event(event)
        store.insert([event])
        worker.run_once()
        state = store.get_execution_state(
            worker.settings.account_id, normalized["signal_id"]
        )
        _check(
            state is not None and state["status"] == "READY",
            "pre-submit transient error returns signal to READY",
        )
        _check(not gateway.orders, "pre-submit failure sends no order")
        worker.run_once()
        state = store.get_execution_state(
            worker.settings.account_id, normalized["signal_id"]
        )
        _check(
            state is not None and state["status"] == "FILLED",
            "pre-submit transient error is safely retried",
        )
        passed += 1
    finally:
        store.close()

    path = root / "terminal-partial-fill.db"
    gateway = _ScenarioFuturesGateway(position_amt=0.6)
    client_order_id = "b1_selftest_partial_cancel"
    gateway.order_results[client_order_id] = {
        "status": "CANCELED",
        "orderId": 901,
        "origQty": "0.5",
        "executedQty": "0.4",
        "avgPrice": "100",
    }
    store, worker, _clock = _open_worker(
        path, "selftest-terminal-partial", gateway, now_ms
    )
    try:
        entry_event = _signal(
            now_ms,
            event="ENTRY",
            action="OPEN",
            side="LONG",
            position_id=41,
            reason="SELFTEST_ENTRY",
        )
        normalized_entry = store._normalize_event(entry_event)
        store.insert([entry_event])
        claimed_entry = store.claim_next_signal(
            worker.settings.account_id,
            worker.worker_id,
            now_ms,
            worker.settings.lease_seconds * 1000,
            activation_cutoff_ms=worker._activation_cutoff_ms,
        )
        _check(claimed_entry is not None, "seed entry is claimed")
        store.update_execution(
            worker.settings.account_id,
            normalized_entry["signal_id"],
            "FILLED",
            order_status="FILLED",
            filled_qty=1.0,
            average_fill_price=100.0,
        )
        exit_event = _signal(
            now_ms + 1,
            event="EXIT",
            action="PARTIAL_CLOSE",
            side="LONG",
            position_id=41,
            reason="PARTIAL_TP_1R",
            close_pct=50.0,
            signal_time=now_ms + 1,
            bar_time=now_ms,
        )
        normalized_exit = store._normalize_event(exit_event)
        store.insert([exit_event])
        claimed = store.claim_next_signal(
            worker.settings.account_id,
            worker.worker_id,
            now_ms + 1,
            worker.settings.lease_seconds * 1000,
            activation_cutoff_ms=worker._activation_cutoff_ms,
        )
        _check(claimed is not None, "partial exit is claimed")
        store.update_execution(
            worker.settings.account_id,
            normalized_exit["signal_id"],
            "PARTIALLY_FILLED",
            order_status="PARTIALLY_FILLED",
            client_order_id=client_order_id,
            filled_qty=0.2,
            result={"order": {"origQty": "0.5"}},
        )
        blocked = worker._reconcile_inflight()
        state = store.get_execution_state(
            worker.settings.account_id, normalized_exit["signal_id"]
        )
        position = store.get_position_execution_state(
            worker.settings.account_id, "BTCUSDT", 41
        )
        _check(blocked, "terminal partial fill blocks new signals")
        _check(
            state is not None
            and state["status"] == "RECONCILIATION_REQUIRED"
            and state["filled_qty"] == 0.4,
            "terminal partial fill is preserved for reconciliation",
        )
        _check(
            abs(position["open_qty"] - 0.6) < 1e-9,
            "ledger quantity includes terminal partial fill",
        )
        _check(
            len(gateway.open_algo_orders("BTCUSDT")) == 1,
            "remaining position retains a protective stop",
        )
        passed += 1
    finally:
        store.close()
    return passed


def run_executor_self_tests(data_dir: Path | None = None) -> int:
    """Run offline happy-path and safety/recovery scenarios before live testing."""
    previous_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        root_dir = Path(data_dir) if data_dir is not None else resolve_path("data")
        root_dir.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(
            prefix="executor-selftest-", dir=root_dir
        ) as temp_dir:
            root = Path(temp_dir)
            now_ms = int(time.time() * 1000)
            passed = (
                _self_test_lifecycles(root, now_ms)
                + _self_test_signal_reason_matrix(root, now_ms)
                + _self_test_pending_reversal(root, now_ms)
                + _self_test_stop_reconciliation(root, now_ms)
                + _self_test_inflight_recovery(root, now_ms)
                + _self_test_guards(root, now_ms)
                + _self_test_open_orders_and_stop_repair(root, now_ms)
                + _self_test_transient_and_terminal_partial_recovery(
                    root, now_ms
                )
            )
    finally:
        logging.disable(previous_disable)
    return passed
