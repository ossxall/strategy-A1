import json
from decimal import Decimal

import pandas as pd
import pytest

from live.bot.events import build_events
from live.bot.execution.binance_usdm import BinanceOrderNotFound, BinanceUsdmClient
from live.bot.execution.selftest import run_executor_self_tests
from live.bot.execution.test_mode import ExecutionTestMode
from live.bot.execution.test_mode_policy import calculate_test_entry_quantity
from live.bot.execution.worker import ExecutionWorker, ExecutorSettings
from live.bot.storage import SignalStore
from strategy.config import Config


def _settings(tmp_path, **overrides):
    values = {
        "enabled": False,
        "test_mode": False,
        "environment": "testnet",
        "allow_live_trading": False,
        "account_id": "test-account",
        "api_key": "",
        "api_secret": "",
        "symbol": "BTCUSDT",
        "poll_seconds": 1.0,
        "account_poll_seconds": 15.0,
        "lease_seconds": 30,
        "max_entry_age_seconds": 90,
        "initial_backfill_seconds": 90,
        "leverage": 1,
        "heartbeat_path": tmp_path / "executor.heartbeat",
        "signal_db": tmp_path / "signal.db",
    }
    values.update(overrides)
    return ExecutorSettings(**values)


def _entry_event():
    return {
        "schema_version": 3,
        "signal_id": "ignored-and-recomputed",
        "symbol": "BTCUSDT",
        "interval": "1m",
        "timeframe": "30min",
        "bar_time": 1_000_000,
        "signal_time": 1_000_000,
        "event": "ENTRY",
        "action": "OPEN",
        "signal": "LONG",
        "side": "LONG",
        "position_id": 1,
        "scheduled": False,
        "reason": "EMA_CROSS",
        "price": 100.0,
        "ema_fast": 101.0,
        "ema_slow": 100.0,
        "adx_entry": 25.0,
        "executed": 0,
        "created_at": "2026-01-01T00:00:00+00:00",
        "payload": json.dumps(
            {
                "action": "OPEN",
                "position_id": 1,
                "sizing": {
                    "risk_pct": 0.01,
                    "stop_loss_pct": 0.01,
                    "commission_bps": 0.0,
                    "slippage_bps": 0.0,
                },
            }
        ),
    }


class FakeFuturesGateway:
    def __init__(self, position_amt=0.0):
        self.position_amt = position_amt
        self.leverage = 1
        self.mark_price_value = 100.0
        self.orders = []
        self.order_results = {}
        self.stops = {}
        self.stop_placements = []
        self.fail_next_breakeven = False
        self.next_order_id = 1

    def position_mode(self):
        return {"dualSidePosition": False}

    def exchange_information(self):
        return {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "filters": [
                        {
                            "filterType": "PRICE_FILTER",
                            "tickSize": "0.1",
                        },
                        {
                            "filterType": "MARKET_LOT_SIZE",
                            "stepSize": "0.01",
                            "minQty": "0.01",
                            "maxQty": "1000",
                        },
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }
            ]
        }

    def symbol_configuration(self, symbol):
        return {"symbol": symbol, "leverage": self.leverage}

    def change_leverage(self, symbol, leverage):
        self.leverage = leverage
        return {"symbol": symbol, "leverage": leverage}

    def account_information(self):
        return {"totalMarginBalance": "100", "availableBalance": "100"}

    def account_balance(self):
        return [{"asset": "USDT", "balance": "100", "availableBalance": "100"}]

    def positions(self, symbol=None):
        if not self.position_amt:
            return []
        return [
            {
                "symbol": "BTCUSDT",
                "positionAmt": str(self.position_amt),
                "entryPrice": "100",
                "markPrice": str(self.mark_price_value),
                "unRealizedProfit": "0",
                "leverage": "1",
            }
        ]

    def open_orders(self, symbol):
        return []

    def open_algo_orders(self, symbol):
        return [
            {
                "clientAlgoId": client_algo_id,
                **stop,
            }
            for client_algo_id, stop in self.stops.items()
        ]

    def mark_price(self, symbol):
        return {"symbol": symbol, "markPrice": str(self.mark_price_value)}

    def place_market_order(
        self, *, symbol, side, quantity, client_order_id, reduce_only
    ):
        qty = float(quantity)
        self.orders.append(
            {
                "symbol": symbol,
                "side": side,
                "quantity": qty,
                "client_order_id": client_order_id,
                "reduce_only": reduce_only,
            }
        )
        self.position_amt += qty if side == "BUY" else -qty
        result = {
            "status": "FILLED",
            "orderId": self.next_order_id,
            "clientOrderId": client_order_id,
            "executedQty": str(qty),
            "avgPrice": "100",
        }
        self.next_order_id += 1
        self.order_results[client_order_id] = result
        return result

    def place_protective_stop(
        self, *, symbol, side, trigger_price, client_algo_id
    ):
        if client_algo_id.startswith("b1be_") and self.fail_next_breakeven:
            self.fail_next_breakeven = False
            raise RuntimeError("(-2021, 'Order would immediately trigger.')")
        if any(
            stop["side"] == side and stop.get("status", "NEW") == "NEW"
            for stop in self.stops.values()
        ):
            raise RuntimeError(
                "(-4130, 'An open closePosition order already exists.')"
            )
        stop = {
            "symbol": symbol,
            "side": side,
            "trigger_price": trigger_price,
            "closePosition": "true",
            "status": "NEW",
        }
        self.stop_placements.append((client_algo_id, dict(stop)))
        self.stops[client_algo_id] = stop
        return {"clientAlgoId": client_algo_id, "algoStatus": "NEW"}

    def query_protective_stop(self, client_algo_id):
        if client_algo_id not in self.stops:
            raise BinanceOrderNotFound(client_algo_id)
        return {"clientAlgoId": client_algo_id, "algoStatus": "NEW"}

    def cancel_protective_stop(self, client_algo_id):
        self.stops.pop(client_algo_id, None)
        return {"clientAlgoId": client_algo_id, "algoStatus": "CANCELED"}

    def query_order(self, symbol, client_order_id=None, order_id=None):
        if client_order_id is not None:
            if client_order_id in self.order_results:
                return self.order_results[client_order_id]
            raise BinanceOrderNotFound(client_order_id)
        for result in self.order_results.values():
            if int(result["orderId"]) == int(order_id):
                return result
        raise BinanceOrderNotFound(str(order_id))


def test_claim_is_account_scoped_and_lease_reclaims_after_expiry(tmp_path):
    store = SignalStore(tmp_path / "signal.db")
    store.connect()
    try:
        assert store.insert([_entry_event()]) == 1
        first = store.claim_next_signal(
            "test-a", "worker-a", now_ms=1_000_000, lease_ms=100
        )
        assert first is not None
        assert first["event"] == "ENTRY"
        assert store.claim_next_signal(
            "test-a", "worker-b", now_ms=1_000_050, lease_ms=100
        ) is None

        reclaimed = store.claim_next_signal(
            "test-a", "worker-b", now_ms=1_000_101, lease_ms=100
        )
        assert reclaimed["signal_id"] == first["signal_id"]
        attempts = store.conn.execute(
            """
            SELECT attempt_count, lease_owner
            FROM signal_execution
            WHERE account_id = 'test-a'
            """
        ).fetchone()
        assert attempts == (2, "worker-b")

        # El mismo outbox puede tener estado de ejecución independiente por cuenta.
        other_account = store.claim_next_signal(
            "test-b", "worker-c", now_ms=1_000_000, lease_ms=100
        )
        assert other_account["signal_id"] == first["signal_id"]
    finally:
        store.close()


def test_execution_status_persists_fill_and_stops_reclaiming(tmp_path):
    store = SignalStore(tmp_path / "signal.db")
    store.connect()
    try:
        store.insert([_entry_event()])
        signal = store.claim_next_signal(
            "test-a", "worker-a", now_ms=1_000_000, lease_ms=100
        )
        store.update_execution(
            "test-a",
            signal["signal_id"],
            "FILLED",
            order_status="FILLED",
            client_order_id="b1_test",
            exchange_order_id="123",
            filled_qty=0.95,
            average_fill_price=100.25,
            result={"protective_stop_client_algo_id": "b1sl_test"},
        )
        assert store.claim_next_signal(
            "test-a", "worker-b", now_ms=2_000_000, lease_ms=100
        ) is None
        row = store.conn.execute(
            """
            SELECT status, filled_qty, average_fill_price, result_json
            FROM signal_execution
            WHERE account_id = 'test-a'
            """
        ).fetchone()
        assert row[:3] == ("FILLED", 0.95, 100.25)
        assert json.loads(row[3])["protective_stop_client_algo_id"] == "b1sl_test"
    finally:
        store.close()


def test_account_stop_fills_are_idempotent_position_adjustments(tmp_path):
    store = SignalStore(tmp_path / "signal.db")
    store.connect()
    try:
        store.insert([_entry_event()])
        entry = store.claim_next_signal(
            "test-account", "worker", now_ms=1_000_000, lease_ms=100
        )
        store.update_execution(
            "test-account",
            entry["signal_id"],
            "FILLED",
            filled_qty=1.0,
        )
        store.save_execution_adjustment(
            "test-account",
            "BTCUSDT",
            1,
            "stop-order:9001",
            0.4,
            99.0,
            "BINANCE_PROTECTIVE_STOP",
            1_000_001,
        )
        # A later cumulative update for the same exchange stop replaces the
        # amount monotonically instead of double-counting the fill.
        store.save_execution_adjustment(
            "test-account",
            "BTCUSDT",
            1,
            "stop-order:9001",
            1.0,
            99.0,
            "BINANCE_PROTECTIVE_STOP",
            1_000_002,
        )
        state = store.get_position_execution_state(
            "test-account", "BTCUSDT", 1
        )
        assert state["adjusted_qty"] == 1.0
        assert state["open_qty"] == 0.0
    finally:
        store.close()


def test_disabled_executor_does_not_claim_or_call_binance(tmp_path):
    class NeverGateway:
        def __getattr__(self, name):
            raise AssertionError(f"Binance no debe consultarse: {name}")

    store = SignalStore(tmp_path / "signal.db")
    store.connect()
    try:
        store.insert([_entry_event()])
        worker = ExecutionWorker(
            _settings(tmp_path),
            store,
            gateway=NeverGateway(),
            clock_ms=lambda: 2_000_000,
        )
        worker.run_once()
        assert store.conn.execute(
            "SELECT COUNT(*) FROM signal_execution"
        ).fetchone()[0] == 0
        assert (tmp_path / "executor.heartbeat").is_file()
    finally:
        store.close()


def test_official_connector_adapter_passes_market_order_parameters():
    class Response:
        def __init__(self, data):
            self.payload = data

        def data(self):
            return self.payload

    class RestClient:
        def new_order(self, **kwargs):
            return Response(kwargs)

    class Client:
        rest_api = RestClient()

    adapter = BinanceUsdmClient("", "", "testnet", client=Client())
    result = adapter.place_market_order(
        symbol="BTCUSDT",
        side="SELL",
        quantity="0.5",
        client_order_id="b1_deterministic",
        reduce_only=True,
    )
    assert result == {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "type": "MARKET",
        "quantity": 0.5,
        "new_client_order_id": "b1_deterministic",
        "new_order_resp_type": "RESULT",
        "reduce_only": "true",
    }


@pytest.mark.parametrize(
    "error_args",
    [
        (-2013, "Order does not exist."),
        ((-2013, "Order does not exist."),),
    ],
)
def test_binance_query_order_translates_sdk_order_not_found(error_args):
    class SDKError(Exception):
        pass

    class Rest:
        def query_order(self, **kwargs):
            raise SDKError(*error_args)

    class Client:
        rest_api = Rest()

    gateway = BinanceUsdmClient("", "", "testnet", client=Client())

    with pytest.raises(BinanceOrderNotFound):
        gateway.query_order("BTCUSDT", "client-id")


def test_production_requires_explicit_live_opt_in(tmp_path):
    with pytest.raises(RuntimeError, match="Producción bloqueada"):
        ExecutionWorker(
            _settings(
                tmp_path,
                enabled=True,
                environment="production",
                allow_live_trading=False,
            ),
            store=None,
        )


def test_test_mode_never_connects_to_production_for_order_execution(tmp_path):
    with pytest.raises(RuntimeError, match="solo puede enviar órdenes a testnet"):
        ExecutionWorker(
            _settings(
                tmp_path,
                enabled=True,
                test_mode=True,
                environment="production",
                allow_live_trading=True,
            ),
            store=None,
            gateway=FakeFuturesGateway(),
        )


def test_strategy_has_no_leverage_setting_and_executor_owns_binance_leverage(
    monkeypatch,
):
    monkeypatch.setenv("BOT_BINANCE_LEVERAGE", "37")
    monkeypatch.setenv("BOT_EXECUTOR_TEST_MODE", "false")
    settings = ExecutorSettings.from_env()
    assert not hasattr(Config(), "max_leverage")
    assert settings.leverage == 37


def test_executor_leverage_defaults_to_10(monkeypatch):
    monkeypatch.delenv("BOT_BINANCE_LEVERAGE", raising=False)
    monkeypatch.setenv("BOT_EXECUTOR_TEST_MODE", "false")

    assert ExecutorSettings.from_env().leverage == 10


def test_executor_database_and_heartbeat_default_to_live_data(monkeypatch):
    from live.bot.config import APP_DIR

    monkeypatch.setenv("BOT_EXECUTOR_TEST_MODE", "false")
    monkeypatch.setenv("BOT_SIGNAL_DB", "data/signal.db")
    monkeypatch.setenv("BOT_EXECUTOR_HEARTBEAT", "data/executor.heartbeat")

    settings = ExecutorSettings.from_env()

    data_dir = APP_DIR / "data"
    assert settings.signal_db == data_dir / "signal.db"
    assert settings.heartbeat_path == data_dir / "executor.heartbeat"


def test_test_mode_forces_test_instrument_limits_and_isolated_state(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("BOT_EXECUTOR_TEST_MODE", "true")
    monkeypatch.setenv("BOT_BINANCE_ENVIRONMENT", "testnet")
    monkeypatch.setenv("BOT_BINANCE_ACCOUNT_ID", "test-account")
    monkeypatch.setenv("BOT_BINANCE_LEVERAGE", "25")
    monkeypatch.setenv("BOT_SYMBOL", "ETHUSDT")
    monkeypatch.setenv("BOT_SIGNAL_DB", str(tmp_path / "signal.db"))
    monkeypatch.setenv("BOT_EXECUTOR_PRODUCTION_AUDIT", "true")
    monkeypatch.setenv("BOT_BINANCE_PRODUCTION_AUDIT_API_KEY", "audit-key")
    monkeypatch.setenv(
        "BOT_BINANCE_PRODUCTION_AUDIT_API_SECRET", "audit-secret"
    )

    settings = ExecutorSettings.from_env()

    assert settings.test_mode is True
    assert settings.symbol == "BTCUSDT"
    assert settings.leverage == 10
    assert settings.account_id == "test-account-executor-test"
    assert settings.signal_db == tmp_path / "signal.executor-test.db"
    assert settings.production_audit_enabled is True
    assert settings.production_audit_api_key == "audit-key"
    assert settings.production_audit_api_secret == "audit-secret"


def test_production_read_only_audit_checks_readiness_without_orders(tmp_path):
    from live.bot.execution.production_audit import (
        run_production_read_only_audit,
    )

    settings = _settings(
        tmp_path,
        enabled=True,
        test_mode=True,
        production_audit_enabled=True,
        production_audit_api_key="read-only-key",
        production_audit_api_secret="read-only-secret",
    )
    gateway = FakeFuturesGateway()

    report = run_production_read_only_audit(settings, gateway=gateway)

    assert report["environment"] == "production"
    assert report["position_mode"] == "ONE_WAY"
    assert report["flat"] is True
    assert report["open_orders"] == 0
    assert report["open_algo_orders"] == 0
    assert report["orders_sent"] == 0
    assert gateway.orders == []
    assert gateway.stops == {}


def test_production_read_only_audit_rejects_unsafe_account_state(tmp_path):
    from live.bot.execution.production_audit import (
        run_production_read_only_audit,
    )

    settings = _settings(
        tmp_path,
        enabled=True,
        test_mode=True,
        production_audit_enabled=True,
        production_audit_api_key="read-only-key",
        production_audit_api_secret="read-only-secret",
    )
    gateway = FakeFuturesGateway(position_amt=0.1)

    with pytest.raises(RuntimeError, match="no está flat"):
        run_production_read_only_audit(settings, gateway=gateway)

    assert gateway.orders == []
    assert gateway.stops == {}


def test_test_mode_stops_before_orders_if_production_audit_fails(
    tmp_path, monkeypatch
):
    from live.bot.config import STOP_EVENT
    from live.bot.execution import production_audit, selftest, test_mode

    settings = _settings(
        tmp_path,
        enabled=True,
        test_mode=True,
        production_audit_enabled=True,
        production_audit_api_key="read-only-key",
        production_audit_api_secret="read-only-secret",
    )
    store = SignalStore(settings.signal_db)
    store.connect()
    worker = ExecutionWorker(settings, store, gateway=FakeFuturesGateway())
    sequence_calls = []
    monkeypatch.setattr(selftest, "run_executor_self_tests", lambda **_: 1)

    def fail_audit(_settings):
        raise RuntimeError("auditoría no disponible")

    monkeypatch.setattr(
        production_audit, "run_production_read_only_audit", fail_audit
    )
    monkeypatch.setattr(
        test_mode.ExecutionTestMode,
        "run_sequence",
        lambda _self: sequence_calls.append("started"),
    )
    STOP_EVENT.clear()
    try:
        assert test_mode.run_test_mode(worker) == 1
        assert sequence_calls == []
    finally:
        STOP_EVENT.clear()
        store.close()


def test_mainnet_environment_alias_uses_production_endpoint(monkeypatch):
    monkeypatch.setenv("BOT_BINANCE_ENVIRONMENT", "mainnet")
    monkeypatch.delenv("BOT_BINANCE_ACCOUNT_ID", raising=False)
    monkeypatch.setenv("BOT_EXECUTOR_TEST_MODE", "false")

    settings = ExecutorSettings.from_env()

    assert settings.environment == "production"
    assert settings.account_id == "binance-production"


def test_entry_size_uses_strategy_risk_and_available_leverage(tmp_path):
    class FakeGateway:
        def symbol_configuration(self, symbol):
            assert symbol == "BTCUSDT"
            return {"symbol": symbol, "leverage": 1}

    store = SignalStore(tmp_path / "signal.db")
    store.connect()
    try:
        worker = ExecutionWorker(
            _settings(tmp_path),
            store,
            gateway=FakeGateway(),
        )
        worker._rules_cache["BTCUSDT"] = {
            "step_size": Decimal("0.01"),
            "min_qty": Decimal("0.01"),
            "max_qty": Decimal(100),
            "min_notional": Decimal(5),
        }
        signal = _entry_event()
        signal["payload"] = json.loads(signal["payload"])
        quantity = worker._entry_quantity(
            signal,
            {"wallet_balance": 100.0, "available_balance": 100.0},
            mark_price=100.0,
        )
        # 1% risk / 1% stop = 1 contract; the available balance at 1x allows it.
        assert quantity == Decimal(1)
    finally:
        store.close()


def test_entry_size_uses_ten_usdt_margin_for_hundred_usdt_account(tmp_path):
    class FakeGateway:
        def symbol_configuration(self, symbol):
            assert symbol == "BTCUSDT"
            return {"symbol": symbol, "leverage": 10}

    store = SignalStore(tmp_path / "signal.db")
    store.connect()
    try:
        worker = ExecutionWorker(
            _settings(
                tmp_path,
                leverage=10,
            ),
            store,
            gateway=FakeGateway(),
        )
        worker._rules_cache["BTCUSDT"] = {
            "step_size": Decimal("0.01"),
            "min_qty": Decimal("0.01"),
            "max_qty": Decimal(100),
            "min_notional": Decimal(5),
        }
        signal = _entry_event()
        signal["payload"] = json.loads(signal["payload"])
        signal["payload"]["sizing"]["risk_pct"] = 0.01

        quantity = worker._entry_quantity(
            signal,
            {"wallet_balance": 100.0, "available_balance": 100.0},
            mark_price=100.0,
        )

        # 1% de 100 USDT arriesga 1 USDT; con stop del 1% son 100 USDT
        # nominales (10 USDT de margen a 10x).
        assert quantity == Decimal(1)
        assert quantity * Decimal(100) / Decimal(10) == Decimal(10)
    finally:
        store.close()


def test_entry_risk_budget_uses_available_futures_balance(tmp_path):
    class FakeGateway:
        def __init__(self):
            self.leverage = 10

        def symbol_configuration(self, symbol):
            assert symbol == "BTCUSDT"
            return {"symbol": symbol, "leverage": self.leverage}

        def change_leverage(self, symbol, leverage):
            assert symbol == "BTCUSDT"
            self.leverage = leverage
            return {"symbol": symbol, "leverage": leverage}

    store = SignalStore(tmp_path / "signal.db")
    store.connect()
    try:
        gateway = FakeGateway()
        worker = ExecutionWorker(
            _settings(tmp_path, leverage=50),
            store,
            gateway=gateway,
        )
        worker._rules_cache["BTCUSDT"] = {
            "step_size": Decimal("0.01"),
            "min_qty": Decimal("0.01"),
            "max_qty": Decimal(100),
            "min_notional": Decimal(5),
        }
        signal = _entry_event()
        signal["payload"] = json.loads(signal["payload"])
        signal["payload"]["sizing"]["risk_pct"] = 0.10

        quantity = worker._entry_quantity(
            signal,
            {"wallet_balance": 100.0, "available_balance": 40.0},
            mark_price=100.0,
        )

        # El 10% se calcula sobre los 40 USDT disponibles, no los 100 de wallet.
        assert quantity == Decimal(4)
        assert gateway.leverage == 50
    finally:
        store.close()


def test_worker_executes_entry_with_risk_sized_quantity_and_protective_stop(tmp_path):
    store = SignalStore(tmp_path / "signal.db")
    store.connect()
    gateway = FakeFuturesGateway()
    try:
        store.insert([_entry_event()])
        worker = ExecutionWorker(
            _settings(
                tmp_path,
                enabled=True,
                api_key="test-key",
                api_secret="test-secret",
            ),
            store,
            gateway=gateway,
            clock_ms=lambda: 1_000_000,
        )
        worker.run_once()

        assert len(gateway.orders) == 1
        assert gateway.orders[0]["side"] == "BUY"
        assert gateway.orders[0]["quantity"] == pytest.approx(1.0)
        assert gateway.orders[0]["reduce_only"] is False
        assert len(gateway.stops) == 1
        stop = next(iter(gateway.stops.values()))
        assert stop["side"] == "SELL"
        assert float(stop["trigger_price"]) == pytest.approx(99.0)
        execution = store.conn.execute(
            "SELECT status, filled_qty FROM signal_execution"
        ).fetchone()
        assert execution == ("FILLED", 1.0)
    finally:
        store.close()


def test_worker_accepts_entry_without_a_configured_stop_loss(tmp_path):
    store = SignalStore(tmp_path / "signal.db")
    store.connect()
    gateway = FakeFuturesGateway()
    try:
        event = _entry_event()
        payload = json.loads(event["payload"])
        payload["sizing"]["stop_loss_pct"] = 0.0
        event["payload"] = json.dumps(payload)
        store.insert([event])
        worker = ExecutionWorker(
            _settings(
                tmp_path,
                enabled=True,
                api_key="test-key",
                api_secret="test-secret",
            ),
            store,
            gateway=gateway,
            clock_ms=lambda: 1_000_000,
        )

        worker.run_once()
        worker.run_once()

        assert len(gateway.orders) == 1
        assert gateway.stops == {}
        assert store.conn.execute(
            "SELECT status FROM signal_execution"
        ).fetchone() == ("FILLED",)
        health = worker.health.build_payload("127.0.0.1", 8081, 1.0)
        assert health["executor"]["account_consistency_error"] is None
    finally:
        store.close()


@pytest.mark.parametrize("order_kind", ["market", "conditional"])
def test_worker_blocks_entry_when_symbol_has_unreconciled_open_orders(
    tmp_path, order_kind
):
    store = SignalStore(tmp_path / "signal.db")
    store.connect()
    gateway = FakeFuturesGateway()
    if order_kind == "market":
        gateway.open_orders = lambda _symbol: [
            {"symbol": "BTCUSDT", "orderId": 91, "status": "NEW"}
        ]
    else:
        gateway.stops["external-stop"] = {
            "symbol": "BTCUSDT",
            "side": "SELL",
            "trigger_price": "99",
            "closePosition": "true",
            "status": "NEW",
        }
    try:
        store.insert([_entry_event()])
        worker = ExecutionWorker(
            _settings(
                tmp_path,
                enabled=True,
                api_key="test-key",
                api_secret="test-secret",
            ),
            store,
            gateway=gateway,
            clock_ms=lambda: 1_000_000,
        )

        worker.run_once()

        assert gateway.orders == []
        assert store.conn.execute(
            "SELECT COUNT(*) FROM signal_execution"
        ).fetchone()[0] == 0
        health = worker.health.build_payload("127.0.0.1", 8081, 1.0)
        assert health["executor"]["account_consistency_error"]
    finally:
        store.close()


def test_worker_restores_missing_stop_for_reconciled_active_position(tmp_path):
    store = SignalStore(tmp_path / "signal.db")
    store.connect()
    gateway = FakeFuturesGateway(position_amt=1.0)
    try:
        store.insert([_entry_event()])
        entry = store.claim_next_signal(
            "test-account", "seed", now_ms=1_000_000, lease_ms=100
        )
        store.update_execution(
            "test-account",
            entry["signal_id"],
            "FILLED",
            order_status="FILLED",
            filled_qty=1.0,
            average_fill_price=100.0,
        )
        worker = ExecutionWorker(
            _settings(
                tmp_path,
                enabled=True,
                api_key="test-key",
                api_secret="test-secret",
            ),
            store,
            gateway=gateway,
            clock_ms=lambda: 1_000_000,
        )

        worker.run_once()

        assert len(gateway.stops) == 1
        stop = next(iter(gateway.stops.values()))
        assert stop["side"] == "SELL"
        assert float(stop["trigger_price"]) == pytest.approx(99.0)
        health = worker.health.build_payload("127.0.0.1", 8081, 1.0)
        assert health["executor"]["account_consistency_error"] is None
    finally:
        store.close()


def test_worker_restores_last_confirmed_breakeven_stop_variant(tmp_path):
    store = SignalStore(tmp_path / "signal.db")
    store.connect()
    gateway = FakeFuturesGateway(position_amt=0.5)
    worker = ExecutionWorker(
        _settings(
            tmp_path,
            enabled=True,
            api_key="test-key",
            api_secret="test-secret",
        ),
        store,
        gateway=gateway,
        clock_ms=lambda: 1_000_000,
    )
    expected_be_id = worker._algo_order_id(1, "be")
    try:
        entry_event = _entry_event()
        store.insert([entry_event])
        entry = store.claim_next_signal(
            "test-account", "seed-entry", now_ms=1_000_000, lease_ms=100
        )
        store.update_execution(
            "test-account",
            entry["signal_id"],
            "FILLED",
            order_status="FILLED",
            filled_qty=1.0,
            average_fill_price=100.0,
        )
        exit_event = {
            **entry_event,
            "event": "EXIT",
            "action": "PARTIAL_CLOSE",
            "reason": "PARTIAL_TP_1R",
            "payload": json.dumps(
                {
                    "action": "PARTIAL_CLOSE",
                    "position_id": 1,
                    "close_pct": 50.0,
                    "sizing": json.loads(entry_event["payload"])["sizing"],
                }
            ),
        }
        store.insert([exit_event])
        exit_signal = store.claim_next_signal(
            "test-account", "seed-exit", now_ms=1_000_000, lease_ms=100
        )
        store.update_execution(
            "test-account",
            exit_signal["signal_id"],
            "FILLED",
            order_status="FILLED",
            client_order_id="b1_tp1-filled",
            filled_qty=0.5,
            result={"protective_stop_client_algo_id": expected_be_id},
        )

        worker.run_once()

        assert list(gateway.stops) == [expected_be_id]
        assert float(gateway.stops[expected_be_id]["trigger_price"]) == pytest.approx(
            100.0
        )
        health = worker.health.build_payload("127.0.0.1", 8081, 1.0)
        assert health["executor"]["account_consistency_error"] is None
    finally:
        store.close()


@pytest.mark.parametrize(
    ("mark_price", "fail_breakeven", "expected_placements"),
    [
        (99.5, False, ["sl"]),
        (101.0, True, ["sl", "sl"]),
    ],
)
def test_worker_recovers_tp1_without_leaving_position_unprotected(
    tmp_path, mark_price, fail_breakeven, expected_placements
):
    store = SignalStore(tmp_path / "signal.db")
    store.connect()
    gateway = FakeFuturesGateway(position_amt=0.5)
    gateway.mark_price_value = mark_price
    gateway.fail_next_breakeven = fail_breakeven
    try:
        entry_event = _entry_event()
        entry_payload = json.loads(entry_event["payload"])
        entry_payload["sizing"]["move_stop_to_breakeven"] = True
        entry_event["payload"] = json.dumps(entry_payload)
        store.insert([entry_event])
        entry = store.claim_next_signal(
            "test-account", "seed-entry", now_ms=1_000_000, lease_ms=100
        )
        store.update_execution(
            "test-account",
            entry["signal_id"],
            "FILLED",
            order_status="FILLED",
            filled_qty=1.0,
            average_fill_price=100.0,
        )

        worker = ExecutionWorker(
            _settings(
                tmp_path,
                enabled=True,
                api_key="test-key",
                api_secret="test-secret",
            ),
            store,
            gateway=gateway,
            clock_ms=lambda: 1_000_000,
        )
        worker._initialize()
        snapshot = worker._account_snapshot(force=True)
        worker._ensure_protective_stop(entry, snapshot)

        exit_event = {
            **entry_event,
            "event": "EXIT",
            "action": "PARTIAL_CLOSE",
            "reason": "PARTIAL_TP_1R",
            "payload": json.dumps(
                {
                    "action": "PARTIAL_CLOSE",
                    "position_id": 1,
                    "close_pct": 50.0,
                    "sizing": entry_payload["sizing"],
                }
            ),
        }
        store.insert([exit_event])
        exit_signal = store.claim_next_signal(
            "test-account", "seed-exit", now_ms=1_000_000, lease_ms=100
        )
        client_order_id = worker._client_order_id(exit_signal["signal_id"])
        gateway.order_results[client_order_id] = {
            "status": "FILLED",
            "orderId": 77,
            "executedQty": "0.5",
            "avgPrice": "100",
        }
        intent = {
            "symbol": "BTCUSDT",
            "side": "SELL",
            "quantity": "0.5",
            "reduce_only": True,
        }
        store.record_order_intent(
            "test-account",
            exit_signal["signal_id"],
            client_order_id,
            "SUBMITTING",
            intent,
        )
        store.update_execution(
            "test-account",
            exit_signal["signal_id"],
            "SUBMITTED",
            order_status="SUBMITTING",
            client_order_id=client_order_id,
        )

        assert worker._reconcile_inflight() is False

        execution = store.get_execution_state(
            "test-account", exit_signal["signal_id"]
        )
        assert execution["status"] == "FILLED"
        assert [client_id[2:4] for client_id, _ in gateway.stop_placements] \
            == expected_placements
        assert len(gateway.stops) == 1
        assert next(iter(gateway.stops)).startswith("b1sl_")
    finally:
        store.close()


def test_worker_retries_transient_failure_before_order_intent(tmp_path):
    store = SignalStore(tmp_path / "signal.db")
    store.connect()
    gateway = FakeFuturesGateway()
    original_mark_price = gateway.mark_price
    calls = 0

    def fail_once(symbol):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("testnet mark endpoint temporarily unavailable")
        return original_mark_price(symbol)

    gateway.mark_price = fail_once
    try:
        store.insert([_entry_event()])
        worker = ExecutionWorker(
            _settings(
                tmp_path,
                enabled=True,
                api_key="test-key",
                api_secret="test-secret",
            ),
            store,
            gateway=gateway,
            clock_ms=lambda: 1_000_000,
        )

        worker.run_once()
        state = store.conn.execute(
            "SELECT status, client_order_id, order_status FROM signal_execution"
        ).fetchone()
        assert state == ("READY", None, None)
        assert gateway.orders == []

        worker.run_once()

        assert len(gateway.orders) == 1
        assert store.conn.execute(
            "SELECT status FROM signal_execution"
        ).fetchone()[0] == "FILLED"
    finally:
        store.close()


@pytest.mark.parametrize(
    ("reported_fill", "previous_fill", "actual_position", "expected_fill"),
    [(0.4, 0.2, 0.6, 0.4), (0.0, 0.2, 0.8, 0.2)],
)
def test_worker_preserves_terminal_partial_fill_and_blocks_for_reconciliation(
    tmp_path, reported_fill, previous_fill, actual_position, expected_fill
):
    store = SignalStore(tmp_path / "signal.db")
    store.connect()
    gateway = FakeFuturesGateway(position_amt=actual_position)
    client_order_id = "b1_partial-canceled"
    gateway.order_results[client_order_id] = {
        "status": "CANCELED",
        "orderId": 92,
        "origQty": "0.5",
        "executedQty": str(reported_fill),
        "avgPrice": "100",
    }
    try:
        entry_event = _entry_event()
        store.insert([entry_event])
        entry = store.claim_next_signal(
            "test-account", "seed", now_ms=1_000_000, lease_ms=100
        )
        store.update_execution(
            "test-account",
            entry["signal_id"],
            "FILLED",
            order_status="FILLED",
            filled_qty=1.0,
            average_fill_price=100.0,
        )

        exit_event = {
            **entry_event,
            "event": "EXIT",
            "action": "PARTIAL_CLOSE",
            "reason": "PARTIAL_TP_1R",
            "payload": json.dumps(
                {
                    "action": "PARTIAL_CLOSE",
                    "position_id": 1,
                    "close_pct": 50.0,
                    "sizing": json.loads(entry_event["payload"])["sizing"],
                }
            ),
        }
        store.insert([exit_event])
        exit_signal = store.claim_next_signal(
            "test-account", "worker", now_ms=1_000_000, lease_ms=100
        )
        store.update_execution(
            "test-account",
            exit_signal["signal_id"],
            "PARTIALLY_FILLED",
            order_status="PARTIALLY_FILLED",
            client_order_id=client_order_id,
            filled_qty=previous_fill,
            result={"order": {"origQty": "0.5"}},
        )
        worker = ExecutionWorker(
            _settings(
                tmp_path,
                enabled=True,
                api_key="test-key",
                api_secret="test-secret",
            ),
            store,
            gateway=gateway,
            clock_ms=lambda: 1_000_000,
        )
        worker._initialize()

        blocked = worker._reconcile_inflight()

        assert blocked is True
        execution = store.get_execution_state(
            "test-account", exit_signal["signal_id"]
        )
        assert execution["status"] == "RECONCILIATION_REQUIRED"
        assert execution["order_status"] == "CANCELED"
        assert execution["filled_qty"] == pytest.approx(expected_fill)
        position = store.get_position_execution_state(
            "test-account", "BTCUSDT", 1
        )
        assert position["open_qty"] == pytest.approx(1.0 - expected_fill)
        assert len(gateway.stops) == 1
    finally:
        store.close()


@pytest.mark.parametrize(
    ("side", "expected_trigger"),
    [("LONG", "65019.4"), ("SHORT", "66333")],
)
def test_protective_stop_trigger_is_rounded_to_exchange_tick(
    tmp_path, side, expected_trigger
):
    store = SignalStore(tmp_path / "signal.db")
    store.connect()
    gateway = FakeFuturesGateway(position_amt=0.1 if side == "LONG" else -0.1)
    try:
        worker = ExecutionWorker(
            _settings(tmp_path),
            store,
            gateway=gateway,
        )
        worker._rules_cache["BTCUSDT"] = {"tick_size": Decimal("0.1")}
        signal = {
            "position_id": 42,
            "side": side,
            "payload": {"sizing": {"stop_loss_pct": 0.01}},
        }
        snapshot = {
            "positions": [
                {
                    "symbol": "BTCUSDT",
                    "position_amt": 0.1 if side == "LONG" else -0.1,
                    "entry_price": 65676.2,
                }
            ]
        }

        worker._protective_stop(signal, snapshot)

        stop = next(iter(gateway.stops.values()))
        assert stop["trigger_price"] == expected_trigger
    finally:
        store.close()


def test_worker_reconciles_filled_order_after_crash_without_resubmitting(tmp_path):
    store = SignalStore(tmp_path / "signal.db")
    store.connect()
    gateway = FakeFuturesGateway(position_amt=0.95)
    try:
        store.insert([_entry_event()])
        claimed = store.claim_next_signal(
            "test-account", "worker-before-crash", now_ms=1_000_000, lease_ms=100
        )
        settings = _settings(
            tmp_path,
            enabled=True,
            api_key="test-key",
            api_secret="test-secret",
        )
        worker = ExecutionWorker(
            settings,
            store,
            gateway=gateway,
            clock_ms=lambda: 1_000_500,
        )
        client_order_id = worker._client_order_id(claimed["signal_id"])
        store.record_order_intent(
            "test-account",
            claimed["signal_id"],
            client_order_id,
            "SUBMITTING",
            {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "quantity": "0.95",
                "reduce_only": False,
            },
        )
        gateway.orders.append(
            {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "quantity": 0.95,
                "client_order_id": client_order_id,
                "reduce_only": False,
            }
        )
        gateway.order_results[client_order_id] = {
            "status": "FILLED",
            "orderId": 700,
            "clientOrderId": client_order_id,
            "executedQty": "0.95",
            "avgPrice": "100",
        }

        worker.run_once()

        assert len(gateway.orders) == 1
        state = store.conn.execute(
            "SELECT status, filled_qty FROM signal_execution"
        ).fetchone()
        assert state == ("FILLED", 0.95)
        assert len(gateway.stops) == 1
    finally:
        store.close()


def test_worker_exit_reduces_only_its_correlated_real_position(tmp_path):
    store = SignalStore(tmp_path / "signal.db")
    store.connect()
    gateway = FakeFuturesGateway(position_amt=1.0)
    try:
        entry = _entry_event()
        store.insert([entry])
        entry_row = store.claim_next_signal(
            "test-account", "seed", now_ms=1_000_000, lease_ms=100
        )
        store.update_execution(
            "test-account",
            entry_row["signal_id"],
            "FILLED",
            filled_qty=1.0,
            average_fill_price=100.0,
        )
        exit_event = {
            **entry,
            "signal_id": "ignored-exit-id",
            "event": "EXIT",
            "action": "PARTIAL_CLOSE",
            "reason": "PARTIAL_TP_1R",
            "payload": json.dumps(
                {
                    "action": "PARTIAL_CLOSE",
                    "position_id": 1,
                    "close_pct": 50.0,
                    "sizing": json.loads(entry["payload"])["sizing"],
                }
            ),
        }
        store.insert([exit_event])
        worker = ExecutionWorker(
            _settings(
                tmp_path,
                enabled=True,
                api_key="test-key",
                api_secret="test-secret",
            ),
            store,
            gateway=gateway,
            clock_ms=lambda: 1_000_000,
        )
        worker.run_once()
        exit_order = gateway.orders[-1]
        assert exit_order["side"] == "SELL"
        assert exit_order["quantity"] == pytest.approx(0.5)
        assert exit_order["reduce_only"] is True
        assert gateway.position_amt == pytest.approx(0.5)
        state = store.get_position_execution_state(
            "test-account", "BTCUSDT", 1
        )
        assert state["open_qty"] == pytest.approx(0.5)
    finally:
        store.close()


def test_generated_tp1_full_close_flows_through_worker_without_be_error(
    tmp_path,
):
    bars = pd.DataFrame(
        {
            "open": [100.0, 100.0, 100.0],
            "high": [100.1, 100.1, 101.2],
            "low": [99.9, 99.9, 101.05],
            "close": [100.0, 100.0, 101.1],
            "volume": [1.0, 1.0, 1.0],
        },
        index=pd.date_range("2026-01-01", periods=3, freq="1min", tz="UTC"),
    )
    strategy_signals = pd.DataFrame(
        {
            "valid": [True, True, True],
            "target": [1, 0, 0],
            "trend_bias": [0, 0, 0],
            "ema_fast": [100.0, 100.0, 100.0],
            "ema_slow": [99.0, 99.0, 99.0],
            "adx_entry": [30.0, 30.0, 30.0],
        },
        index=bars.index,
    )
    strategy = Config(
        timeframe="1min",
        entry_adx_enabled=False,
        entry_atr_enabled=False,
        entry_rsi_divergence_enabled=False,
        entry_gaussian_trend_enabled=False,
        gaussian_trend_reversal_exit_enabled=False,
        adx_exit_enabled=False,
        exit_rsi_divergence_enabled=False,
        partial_tp_enabled=True,
        partial_tp_r=1.0,
        partial_tp_pct=100.0,
        partial_tp2_r=0.0,
        partial_tp2_pct=0.0,
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
        stop_loss_pct=0.01,
        commission_bps=0.0,
        slippage_bps=0.0,
        monthly_loss_limit_pct=0.0,
        streak_trend_only=False,
        move_stop_to_breakeven=True,
    )
    events = build_events(
        bars,
        strategy_signals,
        None,
        None,
        strategy,
        "BTCUSDT",
        "1m",
    )
    entry_event = next(event for event in events if event["event"] == "ENTRY")
    exit_event = next(event for event in events if event["event"] == "EXIT")
    exit_payload = json.loads(exit_event["payload"])
    assert exit_event["action"] == "CLOSE"
    assert exit_payload["reason_code"] == "TAKE_PROFIT_1"
    assert exit_payload["stop_action"] == "CANCEL"

    store = SignalStore(tmp_path / "producer-to-worker.db")
    store.connect()
    gateway = FakeFuturesGateway()
    clock = [entry_event["signal_time"]]
    try:
        worker = ExecutionWorker(
            _settings(
                tmp_path,
                enabled=True,
                api_key="test-key",
                api_secret="test-secret",
                max_entry_age_seconds=300,
            ),
            store,
            gateway=gateway,
            clock_ms=lambda: clock[0],
        )
        assert store.insert(events) == 2

        worker.run_once()
        entry = store.get_execution_state(
            worker.settings.account_id, entry_event["signal_id"]
        )
        assert entry["status"] == "FILLED"
        assert len(gateway.stops) == 1

        gateway.mark_price_value = 101.1
        clock[0] = exit_event["signal_time"]
        worker.run_once()

        exit_state = store.get_execution_state(
            worker.settings.account_id, exit_event["signal_id"]
        )
        assert exit_state["status"] == "FILLED"
        assert gateway.position_amt == pytest.approx(0.0)
        assert gateway.stops == {}
        assert (
            store.get_active_execution_positions(
                worker.settings.account_id, "BTCUSDT"
            )
            == []
        )
    finally:
        store.close()


def test_worker_does_not_close_untracked_binance_position(tmp_path):
    store = SignalStore(tmp_path / "signal.db")
    store.connect()
    gateway = FakeFuturesGateway(position_amt=1.0)
    try:
        exit_event = {
            **_entry_event(),
            "event": "EXIT",
            "action": "CLOSE",
            "reason": "EMA_CROSS_EXIT",
            "payload": json.dumps(
                {"action": "CLOSE", "position_id": 1, "close_pct": 100.0}
            ),
        }
        store.insert([exit_event])
        worker = ExecutionWorker(
            _settings(
                tmp_path,
                enabled=True,
                api_key="test-key",
                api_secret="test-secret",
            ),
            store,
            gateway=gateway,
            clock_ms=lambda: 1_000_000,
        )
        worker.run_once()
        assert gateway.orders == []
        assert store.conn.execute(
            "SELECT COUNT(*) FROM signal_execution"
        ).fetchone()[0] == 0
        last_error = store.conn.execute(
            "SELECT last_error FROM binance_account_snapshot"
        ).fetchone()[0]
        assert last_error is None
        health = worker.health.build_payload("127.0.0.1", 8081, 1.0)
        assert "sin ENTRY ejecutada" in health["executor"][
            "account_consistency_error"
        ]
    finally:
        store.close()


def test_test_mode_runs_bounded_long_lifecycle_and_alternates_side(tmp_path):
    store = SignalStore(tmp_path / "test-signal.db")
    store.connect()
    gateway = FakeFuturesGateway()
    gateway.mark_price_value = 101.0
    try:
        worker = ExecutionWorker(
            _settings(
                tmp_path,
                enabled=True,
                test_mode=True,
                api_key="test-key",
                api_secret="test-secret",
                leverage=10,
            ),
            store,
            gateway=gateway,
            clock_ms=lambda: 1_000_000,
        )

        test_mode = ExecutionTestMode(worker)
        test_mode.run_sequence()

        assert [order["side"] for order in gateway.orders] == [
            "BUY", "SELL", "SELL",
        ]
        assert [order["reduce_only"] for order in gateway.orders] == [
            False, True, True,
        ]
        assert gateway.position_amt == pytest.approx(0.0)
        assert gateway.stops == {}
        assert gateway.leverage == 10
        assert test_mode.next_side == "SHORT"
        assert [client_id[2:4] for client_id, _ in gateway.stop_placements] == [
            "sl", "be",
        ]
    finally:
        store.close()


def test_test_mode_refuses_to_start_with_unresolved_signal(tmp_path):
    store = SignalStore(tmp_path / "test-signal.db")
    store.connect()
    gateway = FakeFuturesGateway()
    try:
        store.insert([_entry_event()])
        worker = ExecutionWorker(
            _settings(
                tmp_path,
                enabled=True,
                test_mode=True,
                api_key="test-key",
                api_secret="test-secret",
                leverage=10,
            ),
            store,
            gateway=gateway,
            clock_ms=lambda: 1_000_000,
        )

        with pytest.raises(RuntimeError, match="señal anterior sin resolver"):
            ExecutionTestMode(worker).run_sequence()

        assert gateway.orders == []
    finally:
        store.close()


@pytest.mark.parametrize("order_kind", ["market", "conditional"])
def test_test_mode_refuses_open_orders_before_starting_cycle(
    tmp_path, order_kind
):
    store = SignalStore(tmp_path / "test-signal.db")
    store.connect()
    gateway = FakeFuturesGateway()
    if order_kind == "market":
        gateway.open_orders = lambda _symbol: [
            {"symbol": "BTCUSDT", "orderId": 901, "status": "NEW"}
        ]
    else:
        gateway.stops["external-stop"] = {
            "symbol": "BTCUSDT",
            "side": "SELL",
            "trigger_price": "99",
            "closePosition": "true",
            "status": "NEW",
        }
    try:
        worker = ExecutionWorker(
            _settings(
                tmp_path,
                enabled=True,
                test_mode=True,
                api_key="test-key",
                api_secret="test-secret",
                leverage=10,
            ),
            store,
            gateway=gateway,
            clock_ms=lambda: 1_000_000,
        )

        with pytest.raises(RuntimeError):
            ExecutionTestMode(worker).run_sequence()

        assert gateway.orders == []
    finally:
        store.close()


def test_test_mode_stops_if_exchange_reports_wrong_protective_trigger(tmp_path):
    store = SignalStore(tmp_path / "test-signal.db")
    store.connect()
    gateway = FakeFuturesGateway()
    place_stop = gateway.place_protective_stop

    def place_wrong_stop(**kwargs):
        result = place_stop(**kwargs)
        gateway.stops[kwargs["client_algo_id"]]["trigger_price"] = "90"
        return result

    gateway.place_protective_stop = place_wrong_stop
    try:
        worker = ExecutionWorker(
            _settings(
                tmp_path,
                enabled=True,
                test_mode=True,
                api_key="test-key",
                api_secret="test-secret",
                leverage=10,
            ),
            store,
            gateway=gateway,
            clock_ms=lambda: 1_000_000,
        )

        with pytest.raises(RuntimeError, match="activación del stop"):
            ExecutionTestMode(worker).run_sequence()

        assert len(gateway.orders) == 1
        assert gateway.orders[0]["reduce_only"] is False
    finally:
        store.close()


def test_test_mode_persists_signals_in_isolated_db_and_uses_worker_pipeline(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BOT_EXECUTOR_TEST_MODE", "true")
    monkeypatch.setenv("BOT_BINANCE_TRADING_ENABLED", "true")
    monkeypatch.setenv("BOT_BINANCE_ENVIRONMENT", "testnet")
    monkeypatch.setenv("BOT_BINANCE_ACCOUNT_ID", "test-account")
    monkeypatch.setenv("BOT_BINANCE_API_KEY", "test-key")
    monkeypatch.setenv("BOT_BINANCE_API_SECRET", "test-secret")
    monkeypatch.setenv("BOT_SIGNAL_DB", str(tmp_path / "signal.db"))
    monkeypatch.setenv("BOT_EXECUTOR_HEARTBEAT", "")

    settings = ExecutorSettings.from_env()
    assert settings.signal_db == tmp_path / "signal.executor-test.db"
    assert settings.account_id == "test-account-executor-test"

    store = SignalStore(settings.signal_db)
    store.connect()
    gateway = FakeFuturesGateway()
    gateway.mark_price_value = 101.0
    try:
        worker = ExecutionWorker(
            settings,
            store,
            gateway=gateway,
            clock_ms=lambda: 1_000_000,
        )

        controller = ExecutionTestMode(worker)
        controller.run_sequence()
        controller.run_sequence()

        executions = store.conn.execute(
            """
            SELECT s.event, s.action, s.reason, x.status, x.order_status
            FROM signals AS s
            JOIN signal_execution AS x USING (signal_id)
            ORDER BY s.id
            """
        ).fetchall()
        assert executions == [
            ("ENTRY", "OPEN", "EXECUTOR_TEST_ENTRY", "FILLED", "FILLED"),
            ("EXIT", "PARTIAL_CLOSE", "PARTIAL_TP_1R", "FILLED", "FILLED"),
            ("EXIT", "CLOSE", "EXECUTOR_TEST_CLOSE", "FILLED", "FILLED"),
            ("ENTRY", "OPEN", "EXECUTOR_TEST_ENTRY", "FILLED", "FILLED"),
            (
                "EXIT",
                "PARTIAL_CLOSE",
                "EXECUTOR_TEST_PARTIAL",
                "FILLED",
                "FILLED",
            ),
            ("EXIT", "CLOSE", "EXECUTOR_TEST_CLOSE", "FILLED", "FILLED"),
        ]
        assert len(gateway.orders) == 6
        assert [order["side"] for order in gateway.orders] == [
            "BUY",
            "SELL",
            "SELL",
            "SELL",
            "BUY",
            "BUY",
        ]
        assert [order["reduce_only"] for order in gateway.orders] == [
            False,
            True,
            True,
            False,
            True,
            True,
        ]
        assert [client_id[2:4] for client_id, _ in gateway.stop_placements] == [
            "sl",
            "be",
            "sl",
        ]
        assert gateway.position_amt == pytest.approx(0.0)
        assert controller.next_side == "LONG"
        assert not (tmp_path / "signal.db").exists()
    finally:
        store.close()


def test_test_mode_stops_after_confirmed_protective_stop_fill(tmp_path):
    from live.bot.config import STOP_EVENT

    store = SignalStore(tmp_path / "test-signal.db")
    store.connect()
    gateway = FakeFuturesGateway()
    try:
        entry = _entry_event()
        entry["reason"] = "EXECUTOR_TEST_ENTRY"
        normalized = store._normalize_event(entry)
        store.insert([entry])
        store.conn.execute(
            "INSERT INTO signal_execution(account_id, signal_id) VALUES (?, ?)",
            ("test-account", normalized["signal_id"]),
        )
        store.conn.commit()
        store.update_execution(
            "test-account",
            normalized["signal_id"],
            "FILLED",
            order_status="FILLED",
            filled_qty=0.1,
            client_order_id="b1_test-stop-entry",
        )
        store.save_execution_adjustment(
            "test-account",
            "BTCUSDT",
            normalized["position_id"],
            "stop-order:99",
            0.1,
            99.0,
            "BINANCE_PROTECTIVE_STOP",
            1_000_000,
        )
        worker = ExecutionWorker(
            _settings(
                tmp_path,
                enabled=True,
                test_mode=True,
                api_key="test-key",
                api_secret="test-secret",
            ),
            store,
            gateway=gateway,
        )
        worker._rules_cache["BTCUSDT"] = worker._symbol_rules("BTCUSDT")
        controller = ExecutionTestMode(worker)
        STOP_EVENT.clear()

        assert controller._stop_closed_test_position(
            normalized["position_id"], "LONG"
        )

        assert STOP_EVENT.is_set()
        assert controller.next_side == "SHORT"
        assert gateway.orders == []
        assert gateway.stops == {}
    finally:
        STOP_EVENT.clear()
        store.close()


def test_test_mode_keeps_original_stop_if_breakeven_would_trigger(tmp_path):
    store = SignalStore(tmp_path / "test-signal.db")
    store.connect()
    gateway = FakeFuturesGateway()
    gateway.mark_price_value = 99.5
    try:
        worker = ExecutionWorker(
            _settings(
                tmp_path,
                enabled=True,
                test_mode=True,
                api_key="test-key",
                api_secret="test-secret",
                leverage=10,
            ),
            store,
            gateway=gateway,
            clock_ms=lambda: 1_000_000,
        )

        ExecutionTestMode(worker).run_sequence()

        assert gateway.position_amt == pytest.approx(0.0)
        assert [client_id[2:4] for client_id, _ in gateway.stop_placements] == [
            "sl",
        ]
        partial = store.conn.execute(
            """
            SELECT x.status, x.result_json
            FROM signal_execution AS x
            JOIN signals AS s USING(signal_id)
            WHERE s.reason = 'PARTIAL_TP_1R'
            """
        ).fetchone()
        assert partial[0] == "FILLED"
        assert "breakeven_stop_skipped" in partial[1]
    finally:
        store.close()


def test_test_mode_restores_original_stop_if_breakeven_order_is_rejected(tmp_path):
    store = SignalStore(tmp_path / "test-signal.db")
    store.connect()
    gateway = FakeFuturesGateway()
    gateway.mark_price_value = 101.0
    gateway.fail_next_breakeven = True
    try:
        worker = ExecutionWorker(
            _settings(
                tmp_path,
                enabled=True,
                test_mode=True,
                api_key="test-key",
                api_secret="test-secret",
                leverage=10,
            ),
            store,
            gateway=gateway,
            clock_ms=lambda: 1_000_000,
        )

        ExecutionTestMode(worker).run_sequence()

        assert gateway.position_amt == pytest.approx(0.0)
        assert [client_id[2:4] for client_id, _ in gateway.stop_placements] == [
            "sl", "sl",
        ]
        partial = store.conn.execute(
            """
            SELECT x.status, x.result_json
            FROM signal_execution AS x
            JOIN signals AS s USING(signal_id)
            WHERE s.reason = 'PARTIAL_TP_1R'
            """
        ).fetchone()
        assert partial[0] == "FILLED"
        assert "breakeven_stop_skipped" in partial[1]
    finally:
        store.close()


def test_test_mode_refuses_minimum_quantity_over_margin_cap(tmp_path):
    store = SignalStore(tmp_path / "test-signal.db")
    store.connect()
    gateway = FakeFuturesGateway()
    try:
        worker = ExecutionWorker(
            _settings(
                tmp_path,
                enabled=True,
                test_mode=True,
                api_key="test-key",
                api_secret="test-secret",
                leverage=10,
            ),
            store,
            gateway=gateway,
        )
        worker._rules_cache["BTCUSDT"] = worker._symbol_rules("BTCUSDT")

        with pytest.raises(ValueError, match="límite es 11.00 USDT"):
            calculate_test_entry_quantity(
                worker._rules_cache["BTCUSDT"],
                Decimal(100000), Decimal(100), Decimal(10)
            )

        assert gateway.orders == []
    finally:
        store.close()


def test_test_mode_accepts_minimum_partial_order_within_eleven_usdt_cap(tmp_path):
    store = SignalStore(tmp_path / "test-signal.db")
    store.connect()
    try:
        worker = ExecutionWorker(
            _settings(
                tmp_path,
                enabled=True,
                test_mode=True,
                api_key="test-key",
                api_secret="test-secret",
                leverage=10,
            ),
            store,
            gateway=FakeFuturesGateway(),
        )
        worker._rules_cache["BTCUSDT"] = {
            "step_size": Decimal("0.001"),
            "min_qty": Decimal("0.001"),
            "max_qty": Decimal(1000),
            "min_notional": Decimal(0),
        }

        quantity = calculate_test_entry_quantity(
            worker._rules_cache["BTCUSDT"],
            Decimal(52500), Decimal(100), Decimal(10)
        )

        assert quantity == Decimal("0.002")
        assert quantity * Decimal(52500) / Decimal(10) == Decimal("10.5")
    finally:
        store.close()


def test_test_mode_requires_trading_explicitly_enabled(tmp_path):
    store = SignalStore(tmp_path / "test-signal.db")
    store.connect()
    try:
        with pytest.raises(
            RuntimeError, match="BOT_BINANCE_TRADING_ENABLED=true"
        ):
            ExecutionWorker(
                _settings(tmp_path, test_mode=True),
                store,
                gateway=FakeFuturesGateway(),
            )
    finally:
        store.close()


def test_test_mode_recovers_prepared_entry_after_confirming_not_found(tmp_path):
    store = SignalStore(tmp_path / "test-signal.db")
    store.connect()
    gateway = FakeFuturesGateway()
    try:
        event = _entry_event()
        event["reason"] = "EXECUTOR_TEST_ENTRY"
        normalized = store._normalize_event(event)
        store.insert([event])
        store.conn.execute(
            "INSERT INTO signal_execution(account_id, signal_id) VALUES (?, ?)",
            ("test-account", normalized["signal_id"]),
        )
        store.conn.commit()
        store.update_execution(
            "test-account",
            normalized["signal_id"],
            "RECONCILIATION_REQUIRED",
            order_status="PREPARED",
            client_order_id="b1_old-test-order",
            error="No se pudo consultar el client_order_id: (-2013, not found)",
        )
        worker = ExecutionWorker(
            _settings(
                tmp_path,
                enabled=True,
                test_mode=True,
                api_key="test-key",
                api_secret="test-secret",
            ),
            store,
            gateway=gateway,
            clock_ms=lambda: 1_000_000,
        )

        ExecutionTestMode(worker).recover_inflight()

        state = store.get_execution_state(
            "test-account", normalized["signal_id"]
        )
        assert state["status"] == "REJECTED"
        assert state["order_status"] == "NOT_FOUND"
        assert "no llegó a Binance" in state["error"]
        assert gateway.orders == []
    finally:
        store.close()


def test_test_mode_restores_stop_for_confirmed_fill_without_resubmitting(tmp_path):
    store = SignalStore(tmp_path / "test-signal.db")
    store.connect()
    gateway = FakeFuturesGateway(position_amt=0.1)
    client_order_id = "b1_confirmed-test-fill"
    gateway.order_results[client_order_id] = {
        "status": "FILLED",
        "orderId": 123,
        "executedQty": "0.1",
        "avgPrice": "100",
    }
    try:
        event = _entry_event()
        event["reason"] = "EXECUTOR_TEST_ENTRY"
        normalized = store._normalize_event(event)
        store.insert([event])
        store.conn.execute(
            "INSERT INTO signal_execution(account_id, signal_id) VALUES (?, ?)",
            ("test-account", normalized["signal_id"]),
        )
        store.conn.commit()
        store.update_execution(
            "test-account",
            normalized["signal_id"],
            "RECONCILIATION_REQUIRED",
            order_status="FILLED",
            client_order_id=client_order_id,
            exchange_order_id="123",
            filled_qty=0.1,
            average_fill_price=100,
            result={"order": gateway.order_results[client_order_id]},
            error="stop rechazado por precisión",
        )
        worker = ExecutionWorker(
            _settings(
                tmp_path,
                enabled=True,
                test_mode=True,
                api_key="test-key",
                api_secret="test-secret",
                leverage=10,
            ),
            store,
            gateway=gateway,
            clock_ms=lambda: 1_000_000,
        )
        worker._rules_cache["BTCUSDT"] = {
            "step_size": Decimal("0.01"),
            "min_qty": Decimal("0.01"),
            "max_qty": Decimal(1000),
            "min_notional": Decimal(5),
            "tick_size": Decimal("0.1"),
        }

        recovered = ExecutionTestMode(worker).recover_inflight()

        assert recovered == {
            "position_id": normalized["position_id"],
            "side": "LONG",
        }
        assert gateway.orders == []
        assert len(gateway.stops) == 1
        assert next(iter(gateway.stops.values()))["trigger_price"] == "99"
        state = store.get_execution_state(
            "test-account", normalized["signal_id"]
        )
        assert state["status"] == "FILLED"
        assert state["error"] is None
    finally:
        store.close()


def test_test_mode_reconciles_confirmed_fill_when_position_is_already_flat(tmp_path):
    store = SignalStore(tmp_path / "test-signal.db")
    store.connect()
    gateway = FakeFuturesGateway()
    client_order_id = "b1_already-flat-test-order"
    gateway.order_results[client_order_id] = {
        "status": "FILLED",
        "orderId": 124,
        "executedQty": "0.1",
        "avgPrice": "100",
    }
    try:
        event = _entry_event()
        event["reason"] = "EXECUTOR_TEST_ENTRY"
        normalized = store._normalize_event(event)
        store.insert([event])
        store.conn.execute(
            "INSERT INTO signal_execution(account_id, signal_id) VALUES (?, ?)",
            ("test-account", normalized["signal_id"]),
        )
        store.conn.commit()
        store.update_execution(
            "test-account",
            normalized["signal_id"],
            "RECONCILIATION_REQUIRED",
            order_status="FILLED",
            client_order_id=client_order_id,
            exchange_order_id="124",
            filled_qty=0.1,
            average_fill_price=100,
            result={"order": gateway.order_results[client_order_id]},
            error="stop rechazado",
        )
        worker = ExecutionWorker(
            _settings(
                tmp_path,
                enabled=True,
                test_mode=True,
                api_key="test-key",
                api_secret="test-secret",
            ),
            store,
            gateway=gateway,
            clock_ms=lambda: 1_000_000,
        )
        worker._rules_cache["BTCUSDT"] = {
            "step_size": Decimal("0.01"),
            "min_qty": Decimal("0.01"),
            "max_qty": Decimal(1000),
            "min_notional": Decimal(5),
            "tick_size": Decimal("0.1"),
        }

        recovered = ExecutionTestMode(worker).recover_inflight()

        assert recovered == {
            "position_id": normalized["position_id"],
            "side": "LONG",
            "already_flat": True,
        }
        assert gateway.orders == []
        assert store.get_active_execution_positions(
            "test-account", "BTCUSDT"
        ) == []
        assert store.get_execution_state(
            "test-account", normalized["signal_id"]
        )["status"] == "FILLED"
    finally:
        store.close()


def test_test_mode_recovers_filled_position_and_confirms_protective_stop(tmp_path):
    store = SignalStore(tmp_path / "test-signal.db")
    store.connect()
    gateway = FakeFuturesGateway(position_amt=0.1)
    try:
        event = _entry_event()
        event["reason"] = "EXECUTOR_TEST_ENTRY"
        normalized = store._normalize_event(event)
        store.insert([event])
        store.conn.execute(
            "INSERT INTO signal_execution(account_id, signal_id) VALUES (?, ?)",
            ("test-account", normalized["signal_id"]),
        )
        store.conn.commit()
        store.update_execution(
            "test-account",
            normalized["signal_id"],
            "FILLED",
            order_status="FILLED",
            client_order_id="b1_test-entry",
            filled_qty=0.1,
            average_fill_price=100,
        )
        worker = ExecutionWorker(
            _settings(
                tmp_path,
                enabled=True,
                test_mode=True,
                api_key="test-key",
                api_secret="test-secret",
            ),
            store,
            gateway=gateway,
        )
        worker._rules_cache["BTCUSDT"] = {
            "step_size": Decimal("0.01"),
            "min_qty": Decimal("0.01"),
            "max_qty": Decimal(1000),
            "min_notional": Decimal(5),
            "tick_size": Decimal("0.1"),
        }
        snapshot = worker._account_snapshot(force=True)
        active = store.get_active_execution_positions(
            "test-account", "BTCUSDT"
        )

        recovered = ExecutionTestMode(worker).recover_active_position(
            active[0], snapshot
        )

        assert recovered["position_id"] == normalized["position_id"]
        assert recovered["side"] == "LONG"
        assert recovered["stop_variant"] == "sl"
        assert recovered["partial_completed"] is False
        assert len(gateway.stops) == 1
        assert next(iter(gateway.stops.values()))["trigger_price"] == "99"
    finally:
        store.close()


@pytest.mark.parametrize(
    ("partial_reason", "mark_price"),
    [
        ("PARTIAL_TP_1R", 99.5),
        ("EXECUTOR_TEST_PARTIAL", 101.0),
    ],
)
def test_test_mode_recovers_filled_partial_without_resubmitting_it(
    tmp_path, partial_reason, mark_price
):
    store = SignalStore(tmp_path / "test-signal.db")
    store.connect()
    gateway = FakeFuturesGateway(position_amt=0.05)
    gateway.mark_price_value = mark_price
    client_order_id = "b1_confirmed-partial"
    gateway.order_results[client_order_id] = {
        "status": "FILLED",
        "orderId": 125,
        "executedQty": "0.05",
        "avgPrice": "100",
    }
    try:
        entry = _entry_event()
        entry["reason"] = "EXECUTOR_TEST_ENTRY"
        entry_payload = json.loads(entry["payload"])
        entry_payload["sizing"]["move_stop_to_breakeven"] = True
        entry["payload"] = json.dumps(entry_payload)
        entry_normalized = store._normalize_event(entry)
        store.insert([entry])
        store.conn.execute(
            "INSERT INTO signal_execution(account_id, signal_id) VALUES (?, ?)",
            ("test-account", entry_normalized["signal_id"]),
        )
        store.conn.commit()
        store.update_execution(
            "test-account",
            entry_normalized["signal_id"],
            "FILLED",
            order_status="FILLED",
            filled_qty=0.1,
            client_order_id="b1_confirmed-entry",
        )

        partial = {
            **entry,
            "event": "EXIT",
            "action": "PARTIAL_CLOSE",
            "reason": partial_reason,
            "payload": json.dumps(
                {
                    "action": "PARTIAL_CLOSE",
                    "position_id": entry["position_id"],
                    "close_pct": 50.0,
                    "sizing": entry_payload["sizing"],
                }
            ),
        }
        partial_normalized = store._normalize_event(partial)
        store.insert([partial])
        store.conn.execute(
            "INSERT INTO signal_execution(account_id, signal_id) VALUES (?, ?)",
            ("test-account", partial_normalized["signal_id"]),
        )
        store.conn.commit()
        store.update_execution(
            "test-account",
            partial_normalized["signal_id"],
            "RECONCILIATION_REQUIRED",
            order_status="FILLED",
            client_order_id=client_order_id,
            exchange_order_id="125",
            filled_qty=0.05,
            result={"order": gateway.order_results[client_order_id]},
            error="breakeven stop rejected",
        )
        worker = ExecutionWorker(
            _settings(
                tmp_path,
                enabled=True,
                test_mode=True,
                api_key="test-key",
                api_secret="test-secret",
            ),
            store,
            gateway=gateway,
            clock_ms=lambda: 1_000_000,
        )
        worker._rules_cache["BTCUSDT"] = {
            "step_size": Decimal("0.01"),
            "min_qty": Decimal("0.01"),
            "max_qty": Decimal(1000),
            "min_notional": Decimal(5),
            "tick_size": Decimal("0.1"),
        }

        recovered = ExecutionTestMode(worker).recover_inflight()

        assert recovered["position_id"] == entry["position_id"]
        assert recovered["side"] == "LONG"
        assert recovered["partial_completed"] is True
        assert recovered["stop_variant"] == "sl"
        assert gateway.orders == []
        assert len(gateway.stops) == 1
        state = store.get_execution_state(
            "test-account", partial_normalized["signal_id"]
        )
        assert state["status"] == "FILLED"
        assert state["error"] is None
    finally:
        store.close()


def test_test_mode_recovers_filled_full_close_after_stop_was_already_removed(
    tmp_path,
):
    store = SignalStore(tmp_path / "test-signal.db")
    store.connect()
    gateway = FakeFuturesGateway()
    close_client_id = "b1_confirmed-test-close"
    gateway.order_results[close_client_id] = {
        "status": "FILLED",
        "orderId": 126,
        "executedQty": "0.05",
        "avgPrice": "100",
    }
    try:
        entry = _entry_event()
        entry["reason"] = "EXECUTOR_TEST_ENTRY"
        entry_normalized = store._normalize_event(entry)
        store.insert([entry])
        store.conn.execute(
            "INSERT INTO signal_execution(account_id, signal_id) VALUES (?, ?)",
            ("test-account", entry_normalized["signal_id"]),
        )
        store.conn.commit()
        store.update_execution(
            "test-account",
            entry_normalized["signal_id"],
            "FILLED",
            order_status="FILLED",
            filled_qty=0.1,
            client_order_id="b1_test-close-entry",
        )

        partial = {
            **entry,
            "event": "EXIT",
            "action": "PARTIAL_CLOSE",
            "reason": "PARTIAL_TP_1R",
            "payload": json.dumps(
                {
                    "action": "PARTIAL_CLOSE",
                    "position_id": entry["position_id"],
                    "close_pct": 50.0,
                    "sizing": json.loads(entry["payload"])["sizing"],
                }
            ),
        }
        partial_normalized = store._normalize_event(partial)
        store.insert([partial])
        store.conn.execute(
            "INSERT INTO signal_execution(account_id, signal_id) VALUES (?, ?)",
            ("test-account", partial_normalized["signal_id"]),
        )
        store.conn.commit()
        store.update_execution(
            "test-account",
            partial_normalized["signal_id"],
            "FILLED",
            order_status="FILLED",
            filled_qty=0.05,
            client_order_id="b1_test-close-partial",
        )

        close = {
            **entry,
            "event": "EXIT",
            "action": "CLOSE",
            "reason": "EXECUTOR_TEST_CLOSE",
            "payload": json.dumps(
                {
                    "action": "CLOSE",
                    "position_id": entry["position_id"],
                    "close_pct": 100.0,
                }
            ),
        }
        close_normalized = store._normalize_event(close)
        store.insert([close])
        store.conn.execute(
            "INSERT INTO signal_execution(account_id, signal_id) VALUES (?, ?)",
            ("test-account", close_normalized["signal_id"]),
        )
        store.conn.commit()
        store.update_execution(
            "test-account",
            close_normalized["signal_id"],
            "RECONCILIATION_REQUIRED",
            order_status="FILLED",
            client_order_id=close_client_id,
            exchange_order_id="126",
            filled_qty=0.05,
            error="stop was already removed",
        )
        worker = ExecutionWorker(
            _settings(
                tmp_path,
                enabled=True,
                test_mode=True,
                api_key="test-key",
                api_secret="test-secret",
            ),
            store,
            gateway=gateway,
            clock_ms=lambda: 1_000_000,
        )
        worker._rules_cache["BTCUSDT"] = {
            "step_size": Decimal("0.01"),
            "min_qty": Decimal("0.01"),
            "max_qty": Decimal(1000),
            "min_notional": Decimal(5),
            "tick_size": Decimal("0.1"),
        }

        recovered = ExecutionTestMode(worker).recover_inflight()

        assert recovered["already_flat"] is True
        assert recovered["partial_completed"] is True
        assert gateway.orders == []
        assert store.get_execution_state(
            "test-account", close_normalized["signal_id"]
        )["status"] == "FILLED"
    finally:
        store.close()


def test_heartbeat_falls_back_beside_signal_db_when_configured_path_is_unwritable(
    tmp_path,
):
    store = SignalStore(tmp_path / "signal.db")
    store.connect()
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("block", encoding="utf-8")
    fallback = tmp_path / "executor.heartbeat"
    try:
        worker = ExecutionWorker(
            _settings(
                tmp_path,
                heartbeat_path=blocked_parent / "executor.heartbeat",
            ),
            store,
            gateway=FakeFuturesGateway(),
        )

        worker._mark_heartbeat()

        assert fallback.is_file()
        assert worker._heartbeat_path == fallback
    finally:
        store.close()


def test_offline_executor_selftest_matrix_passes():
    assert run_executor_self_tests() >= 6
