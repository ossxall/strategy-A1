"""Consumidor de señales para Binance USDⓈ-M Futures (apagado por defecto)."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import signal
import socket
import threading
import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ..config import (
    DEFAULT_SIGNAL_DB,
    STOP_EVENT,
    env_bool,
    env_float,
    env_int,
    env_str,
    logger,
    resolve_path,
    utc_iso,
)
from ..storage import SignalStore
from .binance_usdm import BinanceOrderNotFound, BinanceUsdmClient
from .test_mode_policy import (
    TEST_MODE_MAX_LEVERAGE,
    calculate_test_entry_quantity,
    validate_test_order_notional,
)
from .status import ExecutionHealth, start_execution_status_server

LOG = logging.getLogger("bot1.executor")

PROTECTIVE_STOP_CONFIRM_TIMEOUT_SECONDS = 5.0


def _norm_key(value: str) -> str:
    return "".join(char.lower() for char in value if char.isalnum())


def _mapping(data: Any) -> dict | None:
    if isinstance(data, dict):
        return data
    if hasattr(data, "model_dump"):
        return data.model_dump(by_alias=True, mode="json")
    if hasattr(data, "dict") and callable(data.dict):
        return data.dict(by_alias=True)
    return None


def _get(data: Any, *names: str, default=None):
    data = _mapping(data)
    if data is None:
        return default
    normalized = {_norm_key(str(key)): value for key, value in data.items()}
    for name in names:
        key = _norm_key(name)
        if key in normalized:
            return normalized[key]
    return default


def _items(data: Any, *container_names: str) -> list[dict]:
    if isinstance(data, list):
        return [_mapping(item) or {} for item in data]
    data = _mapping(data)
    if data is not None:
        nested = _get(data, *container_names)
        if isinstance(nested, list):
            return [_mapping(item) or {} for item in nested]
        return [data]
    return []


def _float(value: Any, default: float | None = None) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _decimal(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} no es numérico") from exc
    if not result.is_finite():
        raise ValueError(f"{label} no es finito")
    return result


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("stepSize debe ser positivo")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


@dataclass(frozen=True)
class ExecutorSettings:
    enabled: bool
    test_mode: bool
    environment: str
    allow_live_trading: bool
    account_id: str
    api_key: str
    api_secret: str
    symbol: str
    poll_seconds: float
    account_poll_seconds: float
    lease_seconds: int
    max_entry_age_seconds: int
    initial_backfill_seconds: int
    max_leverage: int
    set_leverage: bool
    max_equity_fraction: float
    max_risk_pct: float
    heartbeat_path: Path | None
    signal_db: Path
    production_audit_enabled: bool = False
    production_audit_api_key: str = ""
    production_audit_api_secret: str = ""
    http_host: str = "127.0.0.1"
    http_port: int = 8081
    http_max_clients: int = 8
    http_request_timeout: float = 1.0

    @classmethod
    def from_env(cls) -> ExecutorSettings:
        environment = env_str("BINANCE_ENVIRONMENT", "testnet").strip().lower()
        if environment == "mainnet":
            environment = "production"
        test_mode = env_bool("EXECUTOR_TEST_MODE", False)
        default_account = f"binance-{environment}"
        account_id = env_str("BINANCE_ACCOUNT_ID", default_account).strip()
        signal_db = resolve_path(env_str("SIGNAL_DB", DEFAULT_SIGNAL_DB))
        if test_mode:
            account_id = f"{account_id}-executor-test"
            signal_db = signal_db.with_name(
                f"{signal_db.stem}.executor-test{signal_db.suffix}"
            )
        heartbeat = env_str(
            "EXECUTOR_HEARTBEAT", "data/executor.heartbeat"
        ).strip()
        return cls(
            enabled=env_bool("BINANCE_TRADING_ENABLED", False),
            test_mode=test_mode,
            environment=environment,
            allow_live_trading=env_bool("BINANCE_ALLOW_LIVE_TRADING", False),
            account_id=account_id,
            api_key=env_str("BINANCE_API_KEY", "").strip(),
            api_secret=env_str("BINANCE_API_SECRET", "").strip(),
            symbol=(
                "BTCUSDT"
                if test_mode
                else env_str("SYMBOL", "BTCUSDT").strip().upper()
            ),
            poll_seconds=max(0.1, env_float("EXECUTOR_POLL_SECONDS", 1.0)),
            account_poll_seconds=max(
                1.0, env_float("EXECUTOR_ACCOUNT_POLL_SECONDS", 15.0)
            ),
            lease_seconds=max(5, env_int("EXECUTOR_LEASE_SECONDS", 30)),
            max_entry_age_seconds=max(
                1, env_int("EXECUTOR_MAX_ENTRY_AGE_SECONDS", 90)
            ),
            initial_backfill_seconds=max(
                0, env_int("EXECUTOR_INITIAL_BACKFILL_SECONDS", 90)
            ),
            max_leverage=(
                TEST_MODE_MAX_LEVERAGE
                if test_mode
                else max(1, env_int("BINANCE_MAX_LEVERAGE", 10))
            ),
            set_leverage=(
                True if test_mode else env_bool("BINANCE_SET_LEVERAGE", True)
            ),
            max_equity_fraction=min(
                1.0, max(0.01, env_float("BINANCE_MAX_EQUITY_FRACTION", 0.95))
            ),
            max_risk_pct=min(0.10, max(0.0001, env_float("BINANCE_MAX_RISK_PCT", 0.03))),
            heartbeat_path=resolve_path(heartbeat) if heartbeat else None,
            signal_db=signal_db,
            production_audit_enabled=env_bool(
                "EXECUTOR_PRODUCTION_AUDIT", False
            ),
            production_audit_api_key=env_str(
                "BINANCE_PRODUCTION_AUDIT_API_KEY", ""
            ).strip(),
            production_audit_api_secret=env_str(
                "BINANCE_PRODUCTION_AUDIT_API_SECRET", ""
            ).strip(),
            http_host=env_str("EXECUTOR_HTTP_HOST", "127.0.0.1").strip(),
            http_port=max(0, env_int("EXECUTOR_HTTP_PORT", 8081)),
            http_max_clients=max(1, env_int("EXECUTOR_HTTP_MAX_CLIENTS", 8)),
            http_request_timeout=max(
                0.1, env_float("EXECUTOR_HTTP_REQUEST_TIMEOUT", 1.0)
            ),
        )


class ExecutionWorker:
    """Consume señales en orden, aplica límites de cuenta y reconcilia Futures."""

    def __init__(
        self,
        settings: ExecutorSettings,
        store: SignalStore,
        gateway: Any | None = None,
        *,
        clock_ms=None,
    ):
        self.settings = settings
        self.store = store
        self.gateway = gateway
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}"
        self._started = False
        self._activation_cutoff_ms: int | None = None
        self._last_account_poll = 0.0
        self._rules_cache: dict[str, dict] = {}
        self._heartbeat_path = self.settings.heartbeat_path
        self.health = ExecutionHealth(self.settings)
        self.health.set_worker_id(self.worker_id)

        if self.settings.environment not in {"testnet", "production"}:
            raise ValueError("BOT_BINANCE_ENVIRONMENT debe ser testnet o production")
        if self.settings.test_mode and self.settings.environment != "testnet":
            raise RuntimeError(
                "EXECUTOR_TEST_MODE solo puede enviar órdenes a testnet; "
                "production se admite únicamente para auditoría de solo lectura."
            )
        if self.settings.production_audit_enabled and not self.settings.test_mode:
            raise RuntimeError(
                "BOT_EXECUTOR_PRODUCTION_AUDIT requiere BOT_EXECUTOR_TEST_MODE=true"
            )
        if self.settings.test_mode and not self.settings.enabled:
            raise RuntimeError(
                "BOT_EXECUTOR_TEST_MODE requiere BOT_BINANCE_TRADING_ENABLED=true"
            )
        if self.settings.enabled:
            if self.settings.environment == "production" \
                    and not self.settings.allow_live_trading:
                raise RuntimeError(
                    "Producción bloqueada: configura "
                    "BOT_BINANCE_ALLOW_LIVE_TRADING=true explícitamente."
                )
            if self.gateway is None:
                self.gateway = BinanceUsdmClient(
                    self.settings.api_key,
                    self.settings.api_secret,
                    self.settings.environment,
                )

    def _mark_heartbeat(self) -> None:
        path = self._heartbeat_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(utc_iso(), encoding="utf-8")
        except OSError:
            fallback = self.settings.signal_db.parent / path.name
            if fallback == path:
                LOG.exception("No se pudo actualizar heartbeat del executor")
                return
            try:
                fallback.parent.mkdir(parents=True, exist_ok=True)
                fallback.write_text(utc_iso(), encoding="utf-8")
            except OSError:
                LOG.exception("No se pudo actualizar heartbeat del executor")
                return
            self._heartbeat_path = fallback
            LOG.warning(
                "Heartbeat no disponible en %s; se usa %s",
                path,
                fallback,
            )

    def _initialize(self) -> None:
        if not self.settings.enabled or self._started:
            return
        self._activation_cutoff_ms = self.store.initialize_signal_consumer(
            self.settings.account_id,
            self.clock_ms(),
            self.settings.initial_backfill_seconds * 1000,
        )
        mode = self.gateway.position_mode()
        dual_side = _get(mode, "dualSidePosition", "dual_side_position")
        if isinstance(dual_side, str):
            dual_side = dual_side.strip().lower() == "true"
        if dual_side is None:
            raise RuntimeError("No se pudo verificar el modo de posición de Futures")
        if bool(dual_side):
            raise RuntimeError(
                "El executor solo admite One-way Mode; Hedge Mode se rechaza "
                "para evitar cierres/aperturas ambiguos."
            )
        self._rules_cache[self.settings.symbol] = self._symbol_rules(
            self.settings.symbol
        )
        self._refresh_account_snapshot()
        self._started = True
        self.health.record_worker_initialized(True)
        LOG.warning(
            "Executor conectado a Binance %s para %s; el envío de órdenes está "
            "habilitado explícitamente.",
            self.settings.environment,
            self.settings.symbol,
        )

    def _symbol_rules(self, symbol: str) -> dict:
        response = self.gateway.exchange_information()
        symbols = _items(response, "symbols")
        item = next(
            (candidate for candidate in symbols
             if str(_get(candidate, "symbol", default="")).upper() == symbol),
            None,
        )
        if item is None:
            raise ValueError(f"{symbol} no aparece como contrato en Binance")
        filters = _items(item, "filters")
        by_type = {
            str(_get(filter_item, "filterType", "filter_type", default="")).upper():
            filter_item
            for filter_item in filters
        }
        market_lot = by_type.get("MARKET_LOT_SIZE") or by_type.get("LOT_SIZE")
        if market_lot is None:
            raise ValueError(f"Binance no publica LOT_SIZE para {symbol}")
        min_notional_filter = (
            by_type.get("MIN_NOTIONAL") or by_type.get("NOTIONAL") or {}
        )
        price_filter = by_type.get("PRICE_FILTER")
        if price_filter is None:
            raise ValueError(f"Binance no publica PRICE_FILTER para {symbol}")
        return {
            "step_size": _decimal(
                _get(market_lot, "stepSize", "step_size"), "stepSize"
            ),
            "min_qty": _decimal(_get(market_lot, "minQty", "min_qty"), "minQty"),
            "max_qty": _decimal(
                _get(market_lot, "maxQty", "max_qty", default="0"),
                "maxQty",
            ),
            "min_notional": _decimal(
                _get(
                    min_notional_filter,
                    "notional",
                    "minNotional",
                    "min_notional",
                    default="0",
                ),
                "minNotional",
            ),
            "tick_size": _decimal(
                _get(price_filter, "tickSize", "tick_size"), "tickSize"
            ),
        }

    def _refresh_account_snapshot(self) -> dict:
        now_ms = self.clock_ms()
        try:
            account = self.gateway.account_information()
            balances = _items(
                self.gateway.account_balance(), "assets", "balances"
            )
            usdt = next(
                (row for row in balances
                 if str(_get(row, "asset", default="")).upper() == "USDT"),
                {},
            )
            wallet = _float(
                _get(account, "totalMarginBalance", "totalWalletBalance"),
                _float(_get(usdt, "balance", "walletBalance"), 0.0),
            )
            available = _float(
                _get(account, "availableBalance"),
                _float(_get(usdt, "availableBalance", "available_balance"), 0.0),
            )
            positions = _items(self.gateway.positions(), "positions")
            active_positions = [
                {
                    "symbol": str(_get(row, "symbol", default="")),
                    "position_amt": _float(
                        _get(row, "positionAmt", "position_amt"), 0.0
                    ),
                    "entry_price": _float(
                        _get(row, "entryPrice", "entry_price"), 0.0
                    ),
                    "mark_price": _float(
                        _get(row, "markPrice", "mark_price"), 0.0
                    ),
                    "unrealized_pnl": _float(
                        _get(row, "unRealizedProfit", "unrealizedProfit"), 0.0
                    ),
                    "leverage": _float(_get(row, "leverage"), 0.0),
                }
                for row in positions
                if abs(
                    _float(_get(row, "positionAmt", "position_amt"), 0.0) or 0.0
                ) > 0
            ]
            open_orders = _items(
                self.gateway.open_orders(self.settings.symbol), "orders"
            )
            open_algo_orders = _items(
                self.gateway.open_algo_orders(self.settings.symbol), "orders"
            )
        except Exception as exc:
            self.health.record_account_snapshot(error=str(exc))
            self.store.save_account_snapshot(
                self.settings.account_id,
                self.settings.environment,
                now_ms,
                wallet_balance=None,
                available_balance=None,
                positions=[],
                open_orders=[],
                error=str(exc),
            )
            raise

        snapshot = {
            "wallet_balance": wallet,
            "available_balance": available,
            "positions": active_positions,
            "open_orders": open_orders,
            "open_algo_orders": open_algo_orders,
        }
        self.health.record_account_snapshot(snapshot)
        self.store.save_account_snapshot(
            self.settings.account_id,
            self.settings.environment,
            now_ms,
            wallet_balance=wallet,
            available_balance=available,
            positions=active_positions,
            open_orders=[
                {"kind": "regular", **order} for order in open_orders
            ] + [
                {"kind": "conditional", **order} for order in open_algo_orders
            ],
        )
        self._last_account_poll = time.monotonic()
        return snapshot

    def _account_snapshot(self, *, force: bool = False) -> dict:
        elapsed = time.monotonic() - self._last_account_poll
        if force or elapsed >= self.settings.account_poll_seconds:
            return self._refresh_account_snapshot()
        row = self.store.conn.execute(
            """
            SELECT wallet_balance, available_balance, positions_json,
                   open_orders_json, last_error
            FROM binance_account_snapshot WHERE account_id = ?
            """,
            (self.settings.account_id,),
        ).fetchone()
        if row is None:
            return self._refresh_account_snapshot()
        return {
            "wallet_balance": row[0],
            "available_balance": row[1],
            "positions": json.loads(row[2]),
            "open_orders": json.loads(row[3]),
            "open_algo_orders": [],
            "last_error": row[4],
        }

    def _account_consistency_error(self, snapshot: dict) -> str | None:
        if snapshot.get("last_error"):
            return f"Última consulta de Binance falló: {snapshot['last_error']}"
        if snapshot.get("reconciliation_error"):
            return str(snapshot["reconciliation_error"])
        logical = self.store.get_active_execution_positions(
            self.settings.account_id, self.settings.symbol
        )
        actual = self._position_for_symbol(snapshot)
        if len(logical) > 1:
            return "Ledger registra más de una posición activa para el símbolo"
        if not logical and actual is None:
            return None
        if not logical:
            return "Posición Binance sin ENTRY ejecutada en el ledger"
        expected = logical[0]
        if actual is None:
            return "Ledger registra posición activa, pero Binance está flat"
        actual_side = "LONG" if actual["position_amt"] > 0 else "SHORT"
        tolerance = self._rules_cache[self.settings.symbol]["step_size"] / Decimal(2)
        actual_qty = abs(_decimal(actual["position_amt"], "positionAmt"))
        expected_qty = _decimal(expected["open_qty"], "ledger open_qty")
        if actual_side != expected["side"] or abs(actual_qty - expected_qty) > tolerance:
            return "Dirección/cantidad Binance difiere de las ejecuciones confirmadas"
        return None

    def _reconcile_protective_stops(self, snapshot: dict) -> dict:
        """Registra fills de stops condicionales que cerraron fuera del worker."""
        actual = self._position_for_symbol(snapshot)
        rules = self._rules_cache[self.settings.symbol]
        tolerance = rules["step_size"] / Decimal(2)
        for logical in self.store.get_active_execution_positions(
            self.settings.account_id, self.settings.symbol
        ):
            expected_qty = _decimal(logical["open_qty"], "ledger open_qty")
            if actual is not None:
                actual_qty = abs(
                    _decimal(actual["position_amt"], "positionAmt")
                )
                actual_side = "LONG" if actual["position_amt"] > 0 else "SHORT"
                if (
                    actual_side == logical["side"]
                    and abs(actual_qty - expected_qty) <= tolerance
                ):
                    continue

            for variant in ("sl", "be"):
                algo_id = self._algo_order_id(
                    logical["position_id"], variant
                )
                try:
                    algo = self.gateway.query_protective_stop(algo_id)
                except Exception as exc:  # noqa: BLE001
                    LOG.debug("No se encontró stop condicional %s: %s", algo_id, exc)
                    algo = None
                if algo is None:
                    continue
                child_order_id = _get(algo, "actualOrderId", "actual_order_id")
                if not child_order_id:
                    continue
                try:
                    order = self.gateway.query_order(
                        self.settings.symbol,
                        order_id=int(child_order_id),
                    )
                except Exception:
                    LOG.exception(
                        "No se pudo consultar el fill del stop %s", algo_id
                    )
                    continue
                order_status = str(
                    _get(order, "status", "orderStatus", default="")
                ).upper()
                filled_qty = _float(
                    _get(order, "executedQty", "executed_qty"), 0.0
                ) or 0.0
                if order_status not in {"FILLED", "PARTIALLY_FILLED"} \
                        or filled_qty <= 0:
                    continue
                self.store.save_execution_adjustment(
                    self.settings.account_id,
                    self.settings.symbol,
                    logical["position_id"],
                    f"stop-order:{child_order_id}",
                    filled_qty,
                    _float(_get(order, "avgPrice", "averagePrice")),
                    "BINANCE_PROTECTIVE_STOP",
                    self.clock_ms(),
                )
                logical_after = self.store.get_position_execution_state(
                    self.settings.account_id,
                    self.settings.symbol,
                    logical["position_id"],
                )
                LOG.warning(
                    "Stop de Binance reconciliado: position_id=%s qty=%s",
                    logical["position_id"],
                    filled_qty,
                )
                snapshot = self._account_snapshot(force=True)
                actual = self._position_for_symbol(snapshot)
                if order_status == "PARTIALLY_FILLED" \
                        and logical_after["open_qty"] > float(tolerance):
                    snapshot["reconciliation_error"] = (
                        "Stop condicional parcialmente ejecutado; verificar "
                        "protección del remanente"
                    )
                break
        return snapshot

    def _position_for_symbol(self, snapshot: dict) -> dict | None:
        return next(
            (
                position
                for position in snapshot["positions"]
                if position["symbol"].upper() == self.settings.symbol
                and abs(position["position_amt"]) > 0
            ),
            None,
        )

    def _client_order_id(self, signal_id: str) -> str:
        digest = hashlib.sha256(
            f"{self.settings.account_id}:{signal_id}".encode()
        ).hexdigest()[:32]
        return f"b1_{digest}"

    def _algo_order_id(self, position_id: int, variant: str = "sl") -> str:
        digest = hashlib.sha256(
            (
                f"{self.settings.account_id}:{self.settings.symbol}:"
                f"{position_id}:{variant}"
            ).encode()
        ).hexdigest()[:30]
        return f"b1{variant[:2]}_{digest}"

    def _entry_quantity(self, signal: dict, snapshot: dict, mark_price: float) -> Decimal:
        payload = signal["payload"]
        sizing = payload.get("sizing")
        if not isinstance(sizing, dict):
            raise TypeError("ENTRY sin payload.sizing; se rechaza por seguridad")
        risk_pct = _decimal(sizing.get("risk_pct"), "risk_pct")
        multiplier = _decimal(sizing.get("risk_multiplier"), "risk_multiplier")
        stop_pct = _decimal(sizing.get("stop_loss_pct"), "stop_loss_pct")
        strategy_leverage = _decimal(sizing.get("max_leverage"), "max_leverage")
        commission_bps = _decimal(sizing.get("commission_bps", 0), "commission_bps")
        slippage_bps = _decimal(sizing.get("slippage_bps", 0), "slippage_bps")
        if risk_pct <= 0 or multiplier <= 0 or strategy_leverage <= 0:
            raise ValueError("Parámetros de riesgo inválidos")

        wallet_equity = _decimal(snapshot["wallet_balance"], "equity")
        available = _decimal(snapshot["available_balance"], "available_balance")
        if wallet_equity <= 0 or available <= 0:
            raise ValueError("No hay equity/margen USDT disponible")
        price = _decimal(mark_price, "mark_price")
        if price <= 0:
            raise ValueError("mark_price debe ser positivo")

        risk_pct = min(risk_pct * multiplier, _decimal(self.settings.max_risk_pct, "max risk"))
        risk_cash = wallet_equity * risk_pct
        side = signal["side"]
        if stop_pct > 0:
            raw_stop = price * (
                Decimal(1) - stop_pct
                if side == "LONG"
                else Decimal(1) + stop_pct
            )
            slip = slippage_bps / Decimal(10000)
            stop_execution = raw_stop * (
                Decimal(1) - slip if side == "LONG" else Decimal(1) + slip
            )
            risk_per_unit = abs(stop_execution - price) + (
                price + abs(stop_execution)
            ) * commission_bps / Decimal(10000)
            if risk_per_unit <= 0:
                raise ValueError("risk_per_unit inválido")
            qty_by_risk = risk_cash / risk_per_unit
        else:
            qty_by_risk = Decimal("Infinity")

        symbol_config = _items(
            self.gateway.symbol_configuration(self.settings.symbol),
            "symbols",
        )
        config_row = symbol_config[0] if symbol_config else {}
        account_leverage = _float(_get(config_row, "leverage"))
        if self.settings.set_leverage:
            target = min(
                self.settings.max_leverage,
                int(strategy_leverage.to_integral_value(rounding=ROUND_DOWN)),
            )
            if target < 1:
                raise ValueError("Leverage objetivo debe ser al menos 1x")
            if account_leverage is None or int(account_leverage) != target:
                changed = self.gateway.change_leverage(
                    self.settings.symbol, target
                )
                confirmed = _float(_get(changed, "leverage"), None)
                if confirmed is not None and int(confirmed) != target:
                    raise RuntimeError(
                        f"Binance confirmó leverage {confirmed}, se solicitó {target}"
                    )
            effective_leverage = Decimal(target)
        else:
            if account_leverage is None or account_leverage < 1:
                raise ValueError(
                    "No se pudo verificar el leverage configurado en Binance"
                )
            effective_leverage = min(
                strategy_leverage,
                Decimal(self.settings.max_leverage),
                _decimal(account_leverage, "account leverage"),
            )

        if self.settings.test_mode:
            return calculate_test_entry_quantity(
                self._rules_cache[self.settings.symbol],
                price,
                available,
                effective_leverage,
            )

        exposure_fraction = _decimal(
            self.settings.max_equity_fraction, "max_equity_fraction"
        )
        max_notional = min(wallet_equity, available) * effective_leverage * exposure_fraction
        qty_by_leverage = max_notional / price
        quantity = min(qty_by_risk, qty_by_leverage)
        rules = self._rules_cache[self.settings.symbol]
        quantity = _floor_to_step(quantity, rules["step_size"])
        if quantity < rules["min_qty"]:
            raise ValueError("Cantidad resultante por debajo de minQty")
        if rules["max_qty"] > 0 and quantity > rules["max_qty"]:
            quantity = _floor_to_step(rules["max_qty"], rules["step_size"])
        if rules["min_notional"] > 0 and quantity * price < rules["min_notional"]:
            raise ValueError("Notional resultante por debajo del mínimo de Binance")
        return quantity

    def _protective_stop(
        self, signal: dict, snapshot: dict, *, breakeven: bool = False
    ) -> str:
        position_id = int(signal["position_id"])
        position = self._position_for_symbol(snapshot)
        if position is None:
            raise RuntimeError("No hay posición para proteger")
        entry_price = _decimal(position["entry_price"], "entry_price")
        sizing = signal["payload"].get("sizing") or {}
        stop_pct = Decimal(0) if breakeven else _decimal(
            sizing.get("stop_loss_pct"), "stop_loss_pct"
        )
        if stop_pct <= 0 and not breakeven:
            return ""
        trigger = (
            entry_price
            if breakeven
            else entry_price * (
                Decimal(1) - stop_pct
                if signal["side"] == "LONG"
                else Decimal(1) + stop_pct
            )
        )
        tick_size = self._rules_cache[self.settings.symbol]["tick_size"]
        if tick_size <= 0:
            raise ValueError("tickSize debe ser positivo para el stop protector")
        trigger_steps = (trigger / tick_size).to_integral_value(
            rounding=ROUND_DOWN if signal["side"] == "LONG" else ROUND_UP
        )
        trigger = trigger_steps * tick_size
        algo_id = self._algo_order_id(
            position_id, "be" if breakeven else "sl"
        )
        result = self.gateway.place_protective_stop(
            symbol=self.settings.symbol,
            side="SELL" if signal["side"] == "LONG" else "BUY",
            trigger_price=_decimal_text(trigger),
            client_algo_id=algo_id,
        )
        response_id = str(
            _get(result, "clientAlgoId", "client_algo_id", default="")
        )
        if response_id and response_id != algo_id:
            raise RuntimeError(
                "Binance devolvió un clientAlgoId distinto al solicitado"
            )
        response_status = str(
            _get(result, "algoStatus", "status", default="")
        ).upper()
        if response_status in {"CANCELED", "EXPIRED", "REJECTED", "FINISHED"}:
            raise RuntimeError(
                f"Binance rechazó el stop protector ({response_status})"
            )
        return algo_id

    def _find_open_protective_stop(self, client_algo_id: str) -> dict | None:
        orders = _items(
            self.gateway.open_algo_orders(self.settings.symbol), "orders"
        )
        return next(
            (
                order
                for order in orders
                if str(
                    _get(order, "clientAlgoId", "client_algo_id", default="")
                ) == client_algo_id
            ),
            None,
        )

    def _verify_protective_stop(self, client_algo_id: str) -> dict:
        deadline = time.monotonic() + PROTECTIVE_STOP_CONFIRM_TIMEOUT_SECONDS
        last_error: Exception | None = None
        while True:
            try:
                existing = self.gateway.query_protective_stop(client_algo_id)
                status = str(
                    _get(existing, "algoStatus", "status", default="")
                ).upper()
                if status not in {
                    "CANCELED",
                    "EXPIRED",
                    "REJECTED",
                    "FINISHED",
                }:
                    return _mapping(existing) or {"status": status}
                last_error = RuntimeError(
                    f"El stop terminó con estado {status}"
                )
            except BinanceOrderNotFound as exc:
                last_error = exc
            except Exception as exc:  # noqa: BLE001 - se comprueba el listado activo
                last_error = exc

            try:
                listed = self._find_open_protective_stop(client_algo_id)
            except Exception as exc:  # noqa: BLE001 - API de listado transitoria
                last_error = exc
            else:
                if listed is not None:
                    status = str(
                        _get(listed, "algoStatus", "status", default="")
                    ).upper()
                    if status not in {
                        "CANCELED",
                        "EXPIRED",
                        "REJECTED",
                        "FINISHED",
                    }:
                        return listed

            if time.monotonic() >= deadline or STOP_EVENT.is_set():
                raise RuntimeError(
                    f"No se confirmó el stop protector {client_algo_id} "
                    f"en Binance: {last_error}"
                ) from last_error
            STOP_EVENT.wait(0.5)

    def _ensure_protective_stop(
        self, signal: dict, snapshot: dict, *, breakeven: bool = False
    ) -> str:
        position_id = int(signal["position_id"])
        algo_id = self._algo_order_id(
            position_id, "be" if breakeven else "sl"
        )
        try:
            existing = self.gateway.query_protective_stop(algo_id)
            algo_status = str(
                _get(existing, "algoStatus", "status", default="")
            ).upper()
            if algo_status not in {"CANCELED", "EXPIRED", "REJECTED", "FINISHED"}:
                return algo_id
        except BinanceOrderNotFound:
            existing = self._find_open_protective_stop(algo_id)
            if existing is not None:
                return algo_id
        except Exception as exc:
            existing = self._find_open_protective_stop(algo_id)
            if existing is not None:
                return algo_id
            raise RuntimeError(
                "No se pudo consultar el stop previo ni el listado de stops; "
                "no se enviará otro stop a ciegas."
            ) from exc
        stop_id = self._protective_stop(
            signal, snapshot, breakeven=breakeven
        )
        self._verify_protective_stop(stop_id)
        return stop_id

    def _cancel_position_stop(self, position_id: int, variant: str) -> None:
        algo_id = self._algo_order_id(position_id, variant)
        try:
            existing = self.gateway.query_protective_stop(algo_id)
        except BinanceOrderNotFound:
            existing = self._find_open_protective_stop(algo_id)
            if existing is None:
                return
        except Exception:
            existing = self._find_open_protective_stop(algo_id)
            if existing is None:
                raise
        status = str(_get(existing, "algoStatus", "status", default="")).upper()
        if status in {"CANCELED", "EXPIRED", "REJECTED", "FINISHED"}:
            return
        try:
            self.gateway.cancel_protective_stop(algo_id)
        except BinanceOrderNotFound:
            if self._find_open_protective_stop(algo_id) is None:
                LOG.debug(
                    "Stop %s ya no figura abierto tras solicitar cancelación",
                    algo_id,
                )
                return
            raise

    def _cancel_position_stops(self, position_id: int) -> None:
        for variant in ("sl", "be"):
            self._cancel_position_stop(position_id, variant)

    def _update_state(
        self,
        signal: dict,
        status: str,
        *,
        client_order_id: str | None = None,
        order_status: str | None = None,
        exchange_order_id: str | None = None,
        filled_qty: float | None = None,
        avg_price: float | None = None,
        result: dict | None = None,
        error: str | None = None,
    ) -> None:
        self.store.update_execution(
            self.settings.account_id,
            signal["signal_id"],
            status,
            order_status=order_status,
            client_order_id=client_order_id,
            exchange_order_id=exchange_order_id,
            filled_qty=filled_qty,
            average_fill_price=avg_price,
            result=result,
            error=error,
            reconciled_at_ms=self.clock_ms(),
        )
        self.health.record_execution(
            signal,
            status,
            order_status=order_status,
            error=error,
        )

    def _record_order_result(
        self,
        signal: dict,
        client_order_id: str,
        result: dict,
        requested_quantity: Decimal,
        *,
        is_entry: bool,
    ) -> bool:
        status = str(_get(result, "status", "orderStatus", default="")).upper()
        executed = _decimal(
            _get(result, "executedQty", "executed_qty", default="0"),
            "executedQty",
        )
        avg_price = _float(
            _get(result, "avgPrice", "averagePrice", "avg_price"), None
        )
        exchange_order_id = _get(result, "orderId", "order_id")
        if status == "FILLED" or (
            requested_quantity > 0 and executed >= requested_quantity
        ):
            final_status = "FILLED"
        elif executed > 0 or status == "PARTIALLY_FILLED":
            final_status = "PARTIALLY_FILLED"
        elif status in {"REJECTED", "EXPIRED", "CANCELED"}:
            final_status = "REJECTED"
        else:
            final_status = "SUBMITTED"

        result_record = {"order": result}
        if final_status in {"FILLED", "PARTIALLY_FILLED"} and is_entry:
            snapshot = self._account_snapshot(force=True)
            try:
                stop_id = self._ensure_protective_stop(signal, snapshot)
            except Exception as exc:
                LOG.exception("No se pudo proteger la posición después de ENTRY")
                self._update_state(
                    signal,
                    "RECONCILIATION_REQUIRED",
                    client_order_id=client_order_id,
                    order_status=status or final_status,
                    exchange_order_id=str(exchange_order_id or ""),
                    filled_qty=float(executed),
                    avg_price=avg_price,
                    result={**result_record, "protection_error": str(exc)},
                    error=f"ENTRY ejecutada; stop no confirmado: {exc}",
                )
                return False
            result_record["protective_stop_client_algo_id"] = stop_id

        if final_status == "FILLED" and not is_entry \
                and signal["action"] == "CLOSE":
            try:
                self._cancel_position_stops(int(signal["position_id"]))
            except Exception as exc:
                LOG.exception("No se pudo cancelar el stop al cerrar")
                self._update_state(
                    signal,
                    "RECONCILIATION_REQUIRED",
                    client_order_id=client_order_id,
                    order_status=status or final_status,
                    exchange_order_id=str(exchange_order_id or ""),
                    filled_qty=float(executed),
                    avg_price=avg_price,
                    result={**result_record, "stop_cancel_error": str(exc)},
                    error=f"Posición cerrada; stop pendiente de reconciliación: {exc}",
                )
                return False

        if final_status == "FILLED" and not is_entry \
                and signal["reason"].startswith("PARTIAL_TP_"):
            sizing = signal["payload"].get("sizing") or {}
            if sizing.get("move_stop_to_breakeven"):
                try:
                    snapshot = self._account_snapshot(force=True)
                    position = self._position_for_symbol(snapshot)
                    if position is None:
                        raise RuntimeError(
                            "No queda posición para proteger tras el parcial"
                        )
                    sl_id = self._ensure_protective_stop(signal, snapshot)
                    self._verify_protective_stop(sl_id)
                    entry_price = _decimal(
                        position["entry_price"], "entry_price"
                    )
                    mark_data = _items(
                        self.gateway.mark_price(self.settings.symbol), "data"
                    )
                    mark_value = _float(
                        _get(
                            mark_data[0] if mark_data else {},
                            "markPrice",
                            "mark_price",
                        )
                    )
                    if mark_value is None or mark_value <= 0:
                        raise ValueError(
                            "markPrice inválido al evaluar breakeven"
                        )
                    mark_price = _decimal(mark_value, "mark_price")
                    breakeven_reached = (
                        mark_price > entry_price
                        if signal["side"] == "LONG"
                        else mark_price < entry_price
                    )
                    if not breakeven_reached:
                        result_record["breakeven_stop_skipped"] = (
                            "Mark price ya cruzó el nivel de entrada; "
                            "se conserva el stop-loss original."
                        )
                        result_record["protective_stop_client_algo_id"] = sl_id
                        LOG.warning(
                            "Parcial ejecutado; se conserva SL porque breakeven "
                            "dispararía inmediatamente para %s",
                            signal["side"],
                        )
                    else:
                        try:
                            self._cancel_position_stop(
                                int(signal["position_id"]), "sl"
                            )
                        except Exception as exc:  # noqa: BLE001 - conserva SL ante fallo de cancelación
                            self._verify_protective_stop(sl_id)
                            result_record["breakeven_stop_skipped"] = str(exc)
                            result_record[
                                "protective_stop_client_algo_id"
                            ] = sl_id
                            LOG.warning(
                                "No se pudo cancelar el SL para moverlo a "
                                "breakeven; se conserva el SL: %s",
                                exc,
                            )
                        else:
                            try:
                                be_id = self._protective_stop(
                                    signal, snapshot, breakeven=True
                                )
                                self._verify_protective_stop(be_id)
                            except Exception as exc:  # noqa: BLE001 - repone protección primaria
                                try:
                                    restore_snapshot = self._account_snapshot(
                                        force=True
                                    )
                                    self._ensure_protective_stop(
                                        signal, restore_snapshot
                                    )
                                except Exception as restore_exc:  # noqa: BLE001
                                    raise RuntimeError(
                                        "Falló el stop a breakeven y no se "
                                        f"pudo restaurar el SL: {restore_exc}"
                                    ) from exc
                                result_record["breakeven_stop_skipped"] = str(
                                    exc
                                )
                                result_record[
                                    "protective_stop_client_algo_id"
                                ] = sl_id
                                LOG.warning(
                                    "No se pudo mover el stop a breakeven; "
                                    "se restauró el SL original: %s",
                                    exc,
                                )
                            else:
                                result_record[
                                    "protective_stop_client_algo_id"
                                ] = be_id
                except Exception as exc:
                    LOG.exception("No se pudo mover el stop a breakeven")
                    self._update_state(
                        signal,
                        "RECONCILIATION_REQUIRED",
                        client_order_id=client_order_id,
                        order_status=status or final_status,
                        exchange_order_id=str(exchange_order_id or ""),
                        filled_qty=float(executed),
                        avg_price=avg_price,
                        result={**result_record, "breakeven_stop_error": str(exc)},
                        error=f"Parcial ejecutado; stop a breakeven no confirmado: {exc}",
                    )
                    return False

        if final_status in {"FILLED", "PARTIALLY_FILLED"}:
            # A following signal may be processed immediately (as in test mode).
            # Do not compare the updated ledger against the pre-order snapshot.
            self._last_account_poll = 0.0

        self._update_state(
            signal,
            final_status,
            client_order_id=client_order_id,
            order_status=status or final_status,
            exchange_order_id=str(exchange_order_id or ""),
            filled_qty=float(executed),
            avg_price=avg_price,
            result=result_record,
        )
        return final_status == "FILLED"

    def _submit_market(
        self,
        signal: dict,
        *,
        side: str,
        quantity: Decimal,
        reduce_only: bool,
        is_entry: bool,
    ) -> bool:
        if self.settings.test_mode:
            mark_data = _items(
                self.gateway.mark_price(self.settings.symbol), "data"
            )
            mark_price = _float(
                _get(mark_data[0] if mark_data else {}, "markPrice", "mark_price")
            )
            if mark_price is None or mark_price <= 0:
                raise ValueError("EXECUTOR_TEST_MODE: markPrice inválido")
            validate_test_order_notional(
                quantity,
                _decimal(mark_price, "mark_price"),
            )

        client_order_id = self._client_order_id(signal["signal_id"])
        intent = {
            "symbol": self.settings.symbol,
            "side": side,
            "quantity": _decimal_text(quantity),
            "reduce_only": reduce_only,
        }
        execution = self.store.get_execution_state(
            self.settings.account_id, signal["signal_id"]
        )
        if execution and execution["client_order_id"] \
                and execution["client_order_id"] != client_order_id:
            self._update_state(
                signal,
                "RECONCILIATION_REQUIRED",
                error="client_order_id persistido no coincide con la señal",
            )
            return False
        previous_order_status = execution["order_status"] if execution else None
        if previous_order_status not in {"PREPARED", "SUBMITTING"}:
            previous_order_status = "PREPARED"
        self.store.record_order_intent(
            self.settings.account_id,
            signal["signal_id"],
            client_order_id,
            previous_order_status,
            intent,
        )

        try:
            result = self.gateway.query_order(
                self.settings.symbol, client_order_id
            )
        except BinanceOrderNotFound:
            if previous_order_status == "SUBMITTING":
                self._update_state(
                    signal,
                    "RECONCILIATION_REQUIRED",
                    client_order_id=client_order_id,
                    error=(
                        "Orden previamente SUBMITTING no aparece aún en Binance; "
                        "no se reenvía para evitar duplicados"
                    ),
                )
                return False
            self.store.record_order_intent(
                self.settings.account_id,
                signal["signal_id"],
                client_order_id,
                "SUBMITTING",
                intent,
            )
            try:
                result = self.gateway.place_market_order(
                    symbol=self.settings.symbol,
                    side=side,
                    quantity=_decimal_text(quantity),
                    client_order_id=client_order_id,
                    reduce_only=reduce_only,
                )
            except Exception as exc:
                LOG.exception(
                    "Respuesta incierta de Binance para %s",
                    signal["signal_id"],
                )
                try:
                    result = self.gateway.query_order(
                        self.settings.symbol, client_order_id
                    )
                except Exception as query_exc:  # noqa: BLE001
                    self._update_state(
                        signal,
                        "RECONCILIATION_REQUIRED",
                        client_order_id=client_order_id,
                        result={"submission_error": str(exc)},
                        error=(
                            "Resultado de orden incierto; query_order también falló: "
                            f"{query_exc}"
                        ),
                    )
                    return False
        except Exception as exc:  # noqa: BLE001 - consulta remota incierta
            self._update_state(
                signal,
                "RECONCILIATION_REQUIRED",
                client_order_id=client_order_id,
                result={"query_error": str(exc)},
                error=f"No se pudo consultar el client_order_id: {exc}",
            )
            return False
        return self._record_order_result(
            signal,
            client_order_id,
            result,
            quantity,
            is_entry=is_entry,
        )

    def _resume_uncertain_order(self, signal: dict, *, is_entry: bool) -> bool:
        """Reconcilia una submission anterior antes de mirar posición/reenviar."""
        execution = self.store.get_execution_state(
            self.settings.account_id, signal["signal_id"]
        )
        if execution is None or execution["order_status"] not in {
            "PREPARED",
            "SUBMITTING",
        }:
            return False
        client_order_id = execution["client_order_id"]
        intent = execution["result"].get("intent", {})
        if not client_order_id or not intent.get("quantity"):
            self._update_state(
                signal,
                "RECONCILIATION_REQUIRED",
                error="Intento persistido sin client_order_id o quantity",
            )
            return True
        try:
            order = self.gateway.query_order(
                self.settings.symbol, client_order_id
            )
        except BinanceOrderNotFound:
            if execution["order_status"] == "PREPARED":
                return False
            self._update_state(
                signal,
                "RECONCILIATION_REQUIRED",
                client_order_id=client_order_id,
                error=(
                    "Submission previa incierta y orden no visible; no se "
                    "reenvía automáticamente"
                ),
            )
            return True
        except Exception as exc:  # noqa: BLE001 - no se reenvía ante incertidumbre
            self._update_state(
                signal,
                "RECONCILIATION_REQUIRED",
                client_order_id=client_order_id,
                error=f"No se pudo resolver submission previa: {exc}",
            )
            return True
        self._record_order_result(
            signal,
            client_order_id,
            order,
            _decimal(intent["quantity"], "intent quantity"),
            is_entry=is_entry,
        )
        return True

    def _execute_entry(self, signal: dict) -> None:
        now_ms = self.clock_ms()
        age_seconds = max(0.0, (now_ms - int(signal["signal_time"])) / 1000)
        if age_seconds > self.settings.max_entry_age_seconds:
            self._update_state(
                signal,
                "EXPIRED",
                error=f"ENTRY vencida ({age_seconds:.1f}s)",
            )
            return
        if self._resume_uncertain_order(signal, is_entry=True):
            return

        snapshot = self._account_snapshot(force=True)
        position = self._position_for_symbol(snapshot)
        active_positions = self.store.get_active_execution_positions(
            self.settings.account_id, self.settings.symbol
        )
        if active_positions:
            self._update_state(
                signal,
                "RECONCILIATION_REQUIRED",
                error="ENTRY bloqueada: el ledger ya registra una posición activa",
                result={"active_positions": active_positions},
            )
            return
        if position is not None:
            self._update_state(
                signal,
                "RECONCILIATION_REQUIRED",
                error=(
                    "ENTRY omitida: ya existe una posición Binance sin "
                    "correlación segura con esta señal"
                ),
                result={"position": position},
            )
            return

        mark = _items(
            self.gateway.mark_price(self.settings.symbol), "data"
        )
        mark_data = mark[0] if mark else self.gateway.mark_price(self.settings.symbol)
        mark_price = _float(_get(mark_data, "markPrice", "mark_price"))
        if mark_price is None or mark_price <= 0:
            raise ValueError("Binance devolvió markPrice inválido")
        quantity = self._entry_quantity(signal, snapshot, mark_price)
        completed = self._submit_market(
            signal,
            side="BUY" if signal["side"] == "LONG" else "SELL",
            quantity=quantity,
            reduce_only=False,
            is_entry=True,
        )
        if not completed:
            return
        LOG.info(
            "ENTRY %s %s qty=%s signal_id=%s",
            signal["side"],
            self.settings.symbol,
            _decimal_text(quantity),
            signal["signal_id"],
        )

    def _execute_exit(self, signal: dict) -> None:
        if self._resume_uncertain_order(signal, is_entry=False):
            return
        snapshot = self._reconcile_protective_stops(
            self._account_snapshot(force=True)
        )
        if snapshot.get("reconciliation_error"):
            self._update_state(
                signal,
                "RECONCILIATION_REQUIRED",
                error=snapshot["reconciliation_error"],
            )
            return
        logical = self.store.get_position_execution_state(
            self.settings.account_id,
            self.settings.symbol,
            int(signal["position_id"]),
        )
        if not logical["has_entry"] or logical["open_qty"] <= 0:
            self._update_state(
                signal,
                "EXPIRED",
                result={"noop": "No existe ENTRY ejecutada para position_id"},
            )
            return
        if logical["side"] != signal["side"]:
            self._update_state(
                signal,
                "RECONCILIATION_REQUIRED",
                error="La dirección EXIT no coincide con la ENTRY ejecutada",
                result={"logical_position": logical},
            )
            return

        position = self._position_for_symbol(snapshot)
        if position is None:
            self._update_state(
                signal,
                "RECONCILIATION_REQUIRED",
                error="El ledger espera una posición, pero Binance está flat",
                result={"logical_position": logical},
            )
            return

        actual_side = "LONG" if position["position_amt"] > 0 else "SHORT"
        rules = self._rules_cache[self.settings.symbol]
        expected_qty = _decimal(logical["open_qty"], "ledger open_qty")
        actual_qty = abs(_decimal(position["position_amt"], "positionAmt"))
        quantity_tolerance = rules["step_size"] / Decimal(2)
        if actual_side != logical["side"] or abs(actual_qty - expected_qty) > quantity_tolerance:
            self._update_state(
                signal,
                "RECONCILIATION_REQUIRED",
                error="Cantidad/dirección real difiere del ledger de estrategia",
                result={
                    "position": position,
                    "logical_position": logical,
                },
            )
            return

        payload = signal["payload"]
        pct = 100.0 if signal["action"] == "CLOSE" else _float(
            _get(payload, "close_pct"), None
        )
        if pct is None or pct <= 0 or pct > 100:
            self._update_state(
                signal, "REJECTED", error="close_pct fuera de (0, 100]"
            )
            return
        quantity = _floor_to_step(
            expected_qty
            * _decimal(pct, "close_pct")
            / Decimal(100),
            rules["step_size"],
        )
        if quantity < rules["min_qty"]:
            self._update_state(
                signal,
                "REJECTED",
                error="Cierre menor que minQty; requiere reconciliación manual",
                result={"position": position, "requested_close_pct": pct},
            )
            return
        completed = self._submit_market(
            signal,
            side="SELL" if actual_side == "LONG" else "BUY",
            quantity=quantity,
            reduce_only=True,
            is_entry=False,
        )
        if completed:
            LOG.info(
                "EXIT %s %s close_pct=%.4f qty=%s signal_id=%s",
                actual_side,
                self.settings.symbol,
                pct,
                _decimal_text(quantity),
                signal["signal_id"],
            )

    def _reconcile_inflight(self) -> bool:
        """Retorna True mientras haya una orden o discrepancia por resolver."""
        inflight = self.store.list_inflight_executions(self.settings.account_id)
        blocked = False
        for signal_row in inflight:
            if signal_row["execution_status"] == "RECONCILIATION_REQUIRED":
                blocked = True
                continue
            client_order_id = signal_row["client_order_id"]
            if not client_order_id:
                self.store.update_execution(
                    self.settings.account_id,
                    signal_row["signal_id"],
                    "RECONCILIATION_REQUIRED",
                    error="Ejecución SUBMITTED sin client_order_id",
                    reconciled_at_ms=self.clock_ms(),
                )
                blocked = True
                continue
            try:
                order = self.gateway.query_order(
                    signal_row["symbol"], client_order_id
                )
            except Exception:
                LOG.exception("No se pudo reconciliar orden %s", client_order_id)
                blocked = True
                continue
            order_status = str(
                _get(order, "status", "orderStatus", default="")
            ).upper()
            if order_status == "FILLED":
                stop_id = None
                try:
                    if signal_row["event"] == "ENTRY":
                        snapshot = self._account_snapshot(force=True)
                        stop_id = self._ensure_protective_stop(
                            signal_row, snapshot
                        )
                    elif signal_row["action"] == "CLOSE":
                        self._cancel_position_stops(int(signal_row["position_id"]))
                    elif signal_row["reason"].startswith("PARTIAL_TP_") \
                            and (signal_row["payload"].get("sizing") or {}).get(
                                "move_stop_to_breakeven"
                            ):
                        self._cancel_position_stops(
                            int(signal_row["position_id"])
                        )
                        snapshot = self._account_snapshot(force=True)
                        stop_id = (
                            self._ensure_protective_stop(
                                signal_row, snapshot, breakeven=True
                            )
                            if self._position_for_symbol(snapshot) is not None
                            else None
                        )
                    else:
                        stop_id = None
                except Exception as exc:  # noqa: BLE001 - requiere reconciliación
                    self.store.update_execution(
                        self.settings.account_id,
                        signal_row["signal_id"],
                        "RECONCILIATION_REQUIRED",
                        order_status=order_status,
                        result={"order": order, "protection_error": str(exc)},
                        error=f"Fill confirmado, pero la protección no: {exc}",
                        reconciled_at_ms=self.clock_ms(),
                    )
                    blocked = True
                    continue
                self.store.update_execution(
                    self.settings.account_id,
                    signal_row["signal_id"],
                    "FILLED",
                    order_status=order_status,
                    exchange_order_id=str(
                        _get(order, "orderId", "order_id", default="") or ""
                    ),
                    filled_qty=_float(
                        _get(order, "executedQty", "executed_qty"), 0.0
                    ),
                    average_fill_price=_float(
                        _get(order, "avgPrice", "averagePrice")
                    ),
                    result={
                        "order": order,
                        **(
                            {"protective_stop_client_algo_id": stop_id}
                            if stop_id
                            else {}
                        ),
                    },
                    reconciled_at_ms=self.clock_ms(),
                )
                self._last_account_poll = 0.0
            elif order_status in {"CANCELED", "EXPIRED", "REJECTED"}:
                self.store.update_execution(
                    self.settings.account_id,
                    signal_row["signal_id"],
                    "REJECTED",
                    order_status=order_status,
                    result=order,
                    reconciled_at_ms=self.clock_ms(),
                )
            else:
                stop_id = None
                if order_status == "PARTIALLY_FILLED" \
                        and signal_row["event"] == "ENTRY":
                    try:
                        snapshot = self._account_snapshot(force=True)
                        stop_id = self._ensure_protective_stop(
                            signal_row, snapshot
                        )
                    except Exception as exc:  # noqa: BLE001 - requiere intervención
                        self.store.update_execution(
                            self.settings.account_id,
                            signal_row["signal_id"],
                            "RECONCILIATION_REQUIRED",
                            order_status=order_status,
                            result={"order": order, "protection_error": str(exc)},
                            error=f"ENTRY parcial sin stop confirmado: {exc}",
                            reconciled_at_ms=self.clock_ms(),
                        )
                        blocked = True
                        continue
                self.store.update_execution(
                    self.settings.account_id,
                    signal_row["signal_id"],
                    "PARTIALLY_FILLED"
                    if order_status == "PARTIALLY_FILLED"
                    else "SUBMITTED",
                    order_status=order_status or "UNKNOWN",
                    filled_qty=_float(
                        _get(order, "executedQty", "executed_qty"), 0.0
                    ),
                    result={
                        "order": order,
                        **(
                            {"protective_stop_client_algo_id": stop_id}
                            if stop_id
                            else {}
                        ),
                    },
                    reconciled_at_ms=self.clock_ms(),
                )
                self._last_account_poll = 0.0
                blocked = True
        return blocked

    def run_once(self) -> None:
        self.health.begin_cycle()
        try:
            self._run_once()
        except Exception as exc:
            self.health.finish_cycle(status="error", error=str(exc))
            raise
        else:
            self.health.record_worker_initialized(self._started)
            self.health.finish_cycle(status="ok")

    def _run_once(self) -> None:
        self._mark_heartbeat()
        if not self.settings.enabled:
            LOG.debug(
                "BOT_BINANCE_TRADING_ENABLED=false; no se reclaman ni ejecutan señales"
            )
            return
        self._initialize()
        if self._reconcile_inflight():
            self.health.record_reconciliation_block(True)
            self.store.set_account_snapshot_error(
                self.settings.account_id,
                "Hay órdenes pendientes o requieren reconciliación",
            )
            return
        self.health.record_reconciliation_block(False)
        snapshot = self._reconcile_protective_stops(self._account_snapshot())
        consistency_error = self._account_consistency_error(snapshot)
        self.health.record_account_consistency(consistency_error)
        self.store.set_account_snapshot_error(
            self.settings.account_id, consistency_error
        )
        if consistency_error:
            LOG.error("Cuenta Binance fuera de sincronía: %s", consistency_error)
            return
        signal = self.store.claim_next_signal(
            self.settings.account_id,
            self.worker_id,
            self.clock_ms(),
            self.settings.lease_seconds * 1000,
            activation_cutoff_ms=self._activation_cutoff_ms,
        )
        if signal is None:
            return
        self.health.record_signal(signal)
        if int(signal["schema_version"]) < 3:
            self._update_state(
                signal, "REJECTED", error="schema_version de señal no compatible"
            )
            return
        if signal["position_id"] is None:
            self._update_state(
                signal, "REJECTED", error="Señal sin position_id correlacionable"
            )
            return
        if signal["event"] == "ENTRY" and not isinstance(
            signal["payload"].get("sizing"), dict
        ):
            self._update_state(
                signal,
                "REJECTED",
                error="ENTRY antigua sin metadatos de sizing v3",
            )
            return
        if signal["symbol"] != self.settings.symbol:
            self._update_state(
                signal,
                "REJECTED",
                error=f"Símbolo fuera de whitelist: {signal['symbol']}",
            )
            return
        try:
            if signal["event"] == "ENTRY" and signal["action"] == "OPEN":
                self._execute_entry(signal)
            elif signal["event"] == "EXIT" \
                    and signal["action"] in {"CLOSE", "PARTIAL_CLOSE"}:
                self._execute_exit(signal)
            else:
                self._update_state(
                    signal,
                    "REJECTED",
                    error="Combinación event/action no soportada",
                )
        except (TypeError, ValueError) as exc:
            LOG.warning(
                "Señal rechazada por validación signal_id=%s: %s",
                signal["signal_id"],
                exc,
            )
            self._update_state(signal, "REJECTED", error=str(exc))
        except Exception as exc:
            LOG.exception("Error al consumir signal_id=%s", signal["signal_id"])
            try:
                self._update_state(
                    signal,
                    "RECONCILIATION_REQUIRED",
                    error=str(exc),
                )
            except Exception:
                LOG.exception("No se pudo guardar el estado de reconciliación")


def main() -> int:
    settings = ExecutorSettings.from_env()
    store = SignalStore(settings.signal_db)
    store.connect()
    try:
        signal.signal(signal.SIGINT, lambda *_: STOP_EVENT.set())
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, lambda *_: STOP_EVENT.set())
        worker = ExecutionWorker(settings, store)
        health = worker.health
        http_server = None
        http_thread = None
        health.mark_running()
        if settings.http_port > 0:
            try:
                http_server, http_thread = start_execution_status_server(
                    health,
                    settings.http_host,
                    settings.http_port,
                    max_clients=settings.http_max_clients,
                    request_timeout=settings.http_request_timeout,
                    worker_thread=threading.current_thread(),
                )
            except OSError:
                LOG.exception(
                    "No se pudo abrir el endpoint del executor en %s:%d; "
                    "el worker sigue sin él.",
                    settings.http_host,
                    settings.http_port,
                )
        if not settings.enabled:
            logger.warning(
                "Executor iniciado en modo seguro: "
                "BOT_BINANCE_TRADING_ENABLED=false; no se enviarán órdenes."
            )
        try:
            if settings.test_mode:
                from .test_mode import run_test_mode

                return run_test_mode(worker)
            while not STOP_EVENT.is_set():
                started = time.monotonic()
                try:
                    worker.run_once()
                except Exception:
                    LOG.exception(
                        "Ciclo del executor falló; no se reintenta una orden a ciegas"
                    )
                delay = max(
                    0.0, settings.poll_seconds - (time.monotonic() - started)
                )
                STOP_EVENT.wait(delay)
        finally:
            health.mark_stopped()
            if http_server is not None:
                http_server.shutdown()
                http_server.server_close()
            if http_thread is not None:
                http_thread.join(timeout=2)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
