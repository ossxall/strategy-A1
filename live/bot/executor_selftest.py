"""Offline scenario checks for the USDⓈ-M executor test mode."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from .binance_usdm import BinanceOrderNotFound
from .executor import ExecutionWorker, ExecutorSettings
from .storage import SignalStore


class _ScenarioAPIError(RuntimeError):
    pass


class _ScenarioFuturesGateway:
    """Deterministic USDⓈ-M simulator; never makes network requests."""

    def __init__(self, *, dual_side: bool = False, position_amt: float = 0.0):
        self.dual_side = dual_side
        self.position_amt = position_amt
        self.entry_price = 100.0
        self.mark = 100.0
        self.leverage = 1
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
        return []

    def open_algo_orders(self, symbol):
        return [
            dict(order)
            for order in self.stops.values()
            if order["algoStatus"] == "NEW"
        ]

    def mark_price(self, symbol):
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
        max_leverage=10,
        set_leverage=True,
        max_equity_fraction=0.95,
        max_risk_pct=0.03,
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
) -> dict:
    sizing = {
        "risk_pct": 0.001,
        "risk_multiplier": 1.0,
        "stop_loss_pct": 0.01,
        "max_leverage": 10.0,
        "commission_bps": 0.0,
        "slippage_bps": 0.0,
        "move_stop_to_breakeven": True,
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
    timestamp = now_ms if signal_time is None else signal_time
    return {
        "schema_version": 3,
        "symbol": "BTCUSDT",
        "interval": "selftest",
        "timeframe": "selftest",
        "bar_time": timestamp,
        "signal_time": timestamp,
        "event": event,
        "action": action,
        "signal": side,
        "side": side,
        "position_id": position_id,
        "scheduled": False,
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
        for position_id, side, favorable_mark, expect_breakeven in (
            (1, "LONG", 101.0, True),
            (2, "SHORT", 99.0, True),
            (3, "LONG", 99.5, False),
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
                ),
            )
            _check(gateway.orders[-1]["reduce_only"], f"{side} partial reduceOnly")
            stop_variant = "be" if expect_breakeven else "sl"
            _check(
                partial["result"].get("protective_stop_client_algo_id")
                == worker._algo_order_id(position_id, stop_variant),
                f"{side} appropriate protective stop remains",
            )
            if not expect_breakeven:
                _check(
                    "breakeven_stop_skipped" in partial["result"],
                    f"{side} unsafe breakeven is skipped",
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


def run_executor_self_tests() -> int:
    """Run offline happy-path and safety/recovery scenarios before live testing."""
    previous_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with TemporaryDirectory(prefix="executor-selftest-") as temp_dir:
            root = Path(temp_dir)
            now_ms = int(time.time() * 1000)
            passed = (
                _self_test_lifecycles(root, now_ms)
                + _self_test_stop_reconciliation(root, now_ms)
                + _self_test_inflight_recovery(root, now_ms)
                + _self_test_guards(root, now_ms)
            )
    finally:
        logging.disable(previous_disable)
    return passed
