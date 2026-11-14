"""Consumidor de señales para Binance USDⓈ-M Futures (apagado por defecto)."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import signal
import socket
import sys
import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "live" / "bot").is_dir():
        sys.path.insert(0, str(source_root))
        from live.bot.binance_usdm import BinanceOrderNotFound, BinanceUsdmClient
        from live.bot.config import (
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
        from live.bot.storage import SignalStore
    else:  # Imagen Docker: bot/ vive en el directorio de trabajo.
        app_dir = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(app_dir.parent))
        sys.path.insert(0, str(app_dir))
        from bot.binance_usdm import BinanceOrderNotFound, BinanceUsdmClient
        from bot.config import (
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
        from bot.storage import SignalStore
else:
    from .binance_usdm import BinanceOrderNotFound, BinanceUsdmClient
    from .config import (
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
    from .storage import SignalStore

LOG = logging.getLogger("bot1.executor")

TEST_MODE_MAX_MARGIN_USDT = Decimal(11)
TEST_MODE_MAX_LEVERAGE = 10
TEST_MODE_STOP_LOSS_PCT = 0.01
TEST_MODE_PAUSE_SECONDS = 10
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
        heartbeat = env_str("EXECUTOR_HEARTBEAT", "/data/executor.heartbeat").strip()
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
            heartbeat_path=Path(heartbeat) if heartbeat else None,
            signal_db=signal_db,
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
        self._test_position_sequence = time.time_ns() // 1000
        self._test_next_side = "LONG"

        if self.settings.environment not in {"testnet", "production"}:
            raise ValueError("BINANCE_ENVIRONMENT debe ser testnet o production")
        if self.settings.test_mode and not self.settings.enabled:
            raise RuntimeError(
                "EXECUTOR_TEST_MODE requiere BINANCE_TRADING_ENABLED=true"
            )
        if self.settings.enabled:
            if self.settings.environment == "production" \
                    and not self.settings.allow_live_trading:
                raise RuntimeError(
                    "Producción bloqueada: configura "
                    "BINANCE_ALLOW_LIVE_TRADING=true explícitamente."
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
            return self._test_mode_entry_quantity(
                price, available, effective_leverage
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

    def _test_mode_entry_quantity(
        self,
        price: Decimal,
        available: Decimal,
        leverage: Decimal,
    ) -> Decimal:
        """Tamaño mínimo para que la secuencia también pueda cerrar un 50%."""
        rules = self._rules_cache[self.settings.symbol]
        step = rules["step_size"]
        min_close_qty = max(
            rules["min_qty"],
            rules["min_notional"] / price if rules["min_notional"] > 0 else Decimal(0),
        )
        close_steps = (min_close_qty / step).to_integral_value(rounding=ROUND_UP)
        close_qty = close_steps * step
        quantity = close_qty * 2
        notional = quantity * price
        margin = notional / leverage
        if rules["max_qty"] > 0 and quantity > rules["max_qty"]:
            raise ValueError(
                "EXECUTOR_TEST_MODE: cantidad mínima para el cierre parcial "
                "supera maxQty"
            )
        if margin > TEST_MODE_MAX_MARGIN_USDT:
            raise ValueError(
                "EXECUTOR_TEST_MODE: los filtros de BTCUSDT requieren "
                f"{margin:.2f} USDT de margen para probar un cierre parcial; "
                f"el límite es {TEST_MODE_MAX_MARGIN_USDT:.2f} USDT. "
                "No se envió ninguna orden."
            )
        if margin > available:
            raise ValueError(
                "EXECUTOR_TEST_MODE: margen disponible insuficiente para el "
                "tamaño mínimo de prueba"
            )
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
            order_notional = quantity * _decimal(mark_price, "mark_price")
            max_notional = (
                TEST_MODE_MAX_MARGIN_USDT * TEST_MODE_MAX_LEVERAGE
            )
            if order_notional > max_notional:
                raise ValueError(
                    "EXECUTOR_TEST_MODE: la orden supera el equivalente a "
                    f"{TEST_MODE_MAX_MARGIN_USDT:.2f} USDT de margen a "
                    f"{TEST_MODE_MAX_LEVERAGE}x; no se envió."
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

    def _test_signal(
        self,
        *,
        event: str,
        action: str,
        side: str,
        position_id: int,
        reason: str,
        close_pct: float | None = None,
    ) -> dict:
        now_ms = self.clock_ms()
        sizing = {
            "risk_pct": 0.01,
            "risk_multiplier": 1.0,
            "stop_loss_pct": TEST_MODE_STOP_LOSS_PCT,
            "max_leverage": TEST_MODE_MAX_LEVERAGE,
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
        raw = {
            "schema_version": 3,
            "symbol": "BTCUSDT",
            "interval": "test",
            "timeframe": "test",
            "bar_time": now_ms,
            "signal_time": now_ms,
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
            "created_at": utc_iso(),
            "payload": payload,
        }
        normalized = self.store._normalize_event(raw)
        if self.store.insert([raw]) != 1:
            raise RuntimeError(
                f"EXECUTOR_TEST_MODE: no se pudo insertar señal {event}/{action}"
            )
        return normalized

    def _run_test_signal(self, **kwargs) -> dict:
        test_signal = self._test_signal(**kwargs)
        self.run_once()
        state = self.store.get_execution_state(
            self.settings.account_id, test_signal["signal_id"]
        )
        if state is None or state["status"] != "FILLED":
            status = state["status"] if state else "sin estado"
            detail = f": {state['error']}" if state and state.get("error") else ""
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: secuencia detenida; "
                f"{test_signal['event']}/{test_signal['action']} terminó en "
                f"{status}{detail}"
            )
        return state

    def _test_entry_signal(self, position_id: int) -> dict:
        cursor = self.store.conn.execute(
            """
            SELECT s.*
            FROM signals AS s
            JOIN signal_execution AS x ON x.signal_id = s.signal_id
            WHERE x.account_id = ?
              AND s.symbol = ?
              AND s.position_id = ?
              AND s.event = 'ENTRY'
              AND s.reason = 'EXECUTOR_TEST_ENTRY'
            ORDER BY s.id DESC
            LIMIT 1
            """,
            (
                self.settings.account_id,
                self.settings.symbol,
                int(position_id),
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: posición activa sin ENTRY test correlacionable."
            )
        names = [column[0] for column in cursor.description]
        signal = dict(zip(names, row))
        signal["payload"] = (
            json.loads(signal["payload"])
            if isinstance(signal["payload"], str)
            else signal["payload"]
        )
        return signal

    def _recover_test_active_position(
        self, active: dict, snapshot: dict
    ) -> dict:
        position_id = int(active["position_id"])
        exits = self.store.conn.execute(
            """
            SELECT s.signal_id, s.action, s.reason, x.status,
                   x.order_status, x.filled_qty
            FROM signals AS s
            JOIN signal_execution AS x ON x.signal_id = s.signal_id
            WHERE x.account_id = ?
              AND s.symbol = ?
              AND s.position_id = ?
              AND s.event = 'EXIT'
            ORDER BY s.id
            """,
            (self.settings.account_id, self.settings.symbol, position_id),
        ).fetchall()
        partial_execution = None
        if exits:
            if (
                len(exits) == 1
                and exits[0][1] == "PARTIAL_CLOSE"
                and exits[0][2] == "PARTIAL_TP_1R"
                and exits[0][3] in {"FILLED", "RECONCILIATION_REQUIRED"}
                and exits[0][4] == "FILLED"
                and float(exits[0][5] or 0) > 0
            ):
                partial_execution = {
                    "signal_id": exits[0][0],
                    "filled_qty": float(exits[0][5]),
                    "status": exits[0][3],
                }
            else:
                raise RuntimeError(
                    "EXECUTOR_TEST_MODE: posición con salidas previas pendiente "
                    "de reconciliación manual."
                )

        signal = self._test_entry_signal(position_id)
        position = self._position_for_symbol(snapshot)
        if snapshot.get("open_orders"):
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: hay órdenes MARKET pendientes; "
                "no se recuperará la posición automáticamente."
            )
        if position is None:
            if snapshot.get("open_algo_orders"):
                raise RuntimeError(
                    "EXECUTOR_TEST_MODE: cuenta flat con stops abiertos; "
                    "requiere revisión manual."
                )
            open_qty = _decimal(active["open_qty"], "open_qty")
            self.store.save_execution_adjustment(
                self.settings.account_id,
                self.settings.symbol,
                position_id,
                f"test-flat-recovery:{position_id}",
                float(open_qty),
                None,
                "EXECUTOR_TEST_MODE: ENTRY test previa ya está flat",
                self.clock_ms(),
            )
            LOG.warning(
                "EXECUTOR_TEST_MODE: posición test %s ya está flat; "
                "ledger reconciliado sin enviar órdenes.",
                position_id,
            )
            return {
                "position_id": position_id,
                "side": active["side"],
                "already_flat": True,
                "partial_completed": partial_execution is not None,
            }

        actual_side = "LONG" if position["position_amt"] > 0 else "SHORT"
        actual_qty = abs(_decimal(position["position_amt"], "positionAmt"))
        expected_qty = _decimal(active["open_qty"], "open_qty")
        tolerance = self._rules_cache[self.settings.symbol]["step_size"] / 2
        if (
            actual_side != active["side"]
            or abs(actual_qty - expected_qty) > tolerance
        ):
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: posición activa no coincide con el ledger; "
                "requiere reconciliación manual."
            )

        stop_variant = "sl"
        if (
            partial_execution is not None
            and (signal["payload"].get("sizing") or {}).get(
                "move_stop_to_breakeven"
            )
        ):
            mark_data = _items(
                self.gateway.mark_price(self.settings.symbol), "data"
            )
            mark_value = _float(
                _get(mark_data[0] if mark_data else {}, "markPrice", "mark_price")
            )
            if mark_value is None or mark_value <= 0:
                raise ValueError(
                    "EXECUTOR_TEST_MODE: markPrice inválido al recuperar parcial"
                )
            entry_price = _decimal(position["entry_price"], "entry_price")
            mark_price = _decimal(mark_value, "mark_price")
            breakeven_is_valid = (
                mark_price > entry_price
                if active["side"] == "LONG"
                else mark_price < entry_price
            )
            if breakeven_is_valid:
                stop_variant = "be"

        stop_ids = {
            self._algo_order_id(position_id, variant)
            for variant in ("sl", "be")
        }
        open_stop_ids = {
            str(_get(order, "clientAlgoId", "client_algo_id", default=""))
            for order in snapshot.get("open_algo_orders", [])
        }
        if open_stop_ids - stop_ids:
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: hay stops ajenos a la posición test; "
                "requiere revisión manual."
            )
        stop_id = self._ensure_protective_stop(
            signal, snapshot, breakeven=stop_variant == "be"
        )
        self._verify_protective_stop(stop_id)
        self._cancel_position_stop(
            position_id, "sl" if stop_variant == "be" else "be"
        )
        LOG.warning(
            "EXECUTOR_TEST_MODE: posición test %s recuperada y stop confirmado.",
            position_id,
        )
        return {
            "position_id": position_id,
            "side": active["side"],
            "partial_completed": partial_execution is not None,
            "stop_variant": stop_variant,
        }

    def _recover_test_partial_inflight(self, execution: dict) -> dict:
        if (
            execution["action"] != "PARTIAL_CLOSE"
            or execution["reason"] != "PARTIAL_TP_1R"
            or execution["order_status"] != "FILLED"
            or not execution["client_order_id"]
            or float(execution["filled_qty"] or 0) <= 0
        ):
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: parcial previo no verificable; "
                "requiere reconciliación manual."
            )
        order = self.gateway.query_order(
            self.settings.symbol, execution["client_order_id"]
        )
        order_status = str(_get(order, "status", "orderStatus", default="")).upper()
        order_qty = _decimal(
            _get(order, "executedQty", "executed_qty", default="0"),
            "executedQty",
        )
        expected_qty = _decimal(execution["filled_qty"], "filled_qty")
        tolerance = self._rules_cache[self.settings.symbol]["step_size"] / 2
        if order_status != "FILLED" or abs(order_qty - expected_qty) > tolerance:
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: el parcial confirmado por Binance no "
                "coincide con el ledger."
            )

        active_positions = self.store.get_active_execution_positions(
            self.settings.account_id, self.settings.symbol
        )
        if (
            len(active_positions) != 1
            or active_positions[0]["position_id"] != int(execution["position_id"])
        ):
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: no se pudo correlacionar el remanente "
                "del parcial con Binance."
            )
        snapshot = self._account_snapshot(force=True)
        recovered = self._recover_test_active_position(
            active_positions[0], snapshot
        )
        result = {
            "order": order,
            "recovered_partial": True,
        }
        if recovered.get("already_flat"):
            result["recovered_flat"] = True
        else:
            result["protective_stop_client_algo_id"] = self._algo_order_id(
                int(execution["position_id"]),
                recovered.get("stop_variant", "sl"),
            )
        average_price = _float(
            _get(order, "avgPrice", "averagePrice", "avg_price")
        )
        self.store.update_execution(
            self.settings.account_id,
            execution["signal_id"],
            "FILLED",
            order_status="FILLED",
            client_order_id=execution["client_order_id"],
            exchange_order_id=str(_get(order, "orderId", "order_id", default="")),
            filled_qty=float(order_qty),
            average_fill_price=average_price,
            result=result,
            error=None,
            reconciled_at_ms=self.clock_ms(),
        )
        LOG.warning(
            "EXECUTOR_TEST_MODE: parcial previo %s confirmado; se evita "
            "reenviar el cierre.",
            execution["signal_id"],
        )
        return {
            **recovered,
            "partial_completed": True,
        }

    def _recover_test_close_inflight(self, execution: dict) -> dict:
        if (
            execution["action"] != "CLOSE"
            or execution["reason"] != "EXECUTOR_TEST_CLOSE"
            or execution["order_status"] != "FILLED"
            or not execution["client_order_id"]
            or float(execution["filled_qty"] or 0) <= 0
        ):
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: cierre previo no verificable; "
                "requiere reconciliación manual."
            )
        order = self.gateway.query_order(
            self.settings.symbol, execution["client_order_id"]
        )
        order_status = str(_get(order, "status", "orderStatus", default="")).upper()
        order_qty = _decimal(
            _get(order, "executedQty", "executed_qty", default="0"),
            "executedQty",
        )
        expected_qty = _decimal(execution["filled_qty"], "filled_qty")
        tolerance = self._rules_cache[self.settings.symbol]["step_size"] / 2
        if order_status != "FILLED" or abs(order_qty - expected_qty) > tolerance:
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: el cierre confirmado por Binance no "
                "coincide con el ledger."
            )

        snapshot = self._account_snapshot(force=True)
        if (
            self._position_for_symbol(snapshot) is not None
            or snapshot.get("open_orders")
            or snapshot.get("open_algo_orders")
            or self.store.get_active_execution_positions(
                self.settings.account_id, self.settings.symbol
            )
        ):
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: el cierre previo no dejó la cuenta "
                "flat y sin órdenes; requiere reconciliación manual."
            )
        average_price = _float(
            _get(order, "avgPrice", "averagePrice", "avg_price")
        )
        self.store.update_execution(
            self.settings.account_id,
            execution["signal_id"],
            "FILLED",
            order_status="FILLED",
            client_order_id=execution["client_order_id"],
            exchange_order_id=str(_get(order, "orderId", "order_id", default="")),
            filled_qty=float(order_qty),
            average_fill_price=average_price,
            result={"order": order, "recovered_close": True},
            error=None,
            reconciled_at_ms=self.clock_ms(),
        )
        LOG.warning(
            "EXECUTOR_TEST_MODE: cierre previo %s confirmado; cuenta flat.",
            execution["signal_id"],
        )
        return {
            "position_id": int(execution["position_id"]),
            "side": execution["side"],
            "already_flat": True,
            "partial_completed": True,
        }

    def _resolve_finished_test_breakeven(
        self, position_id: int, side: str
    ) -> bool:
        """Reconciles a BE stop that finished between placement and verification."""
        snapshot = self._reconcile_protective_stops(
            self._account_snapshot(force=True)
        )
        if snapshot.get("reconciliation_error"):
            raise RuntimeError(str(snapshot["reconciliation_error"]))
        active = self.store.get_active_execution_positions(
            self.settings.account_id, self.settings.symbol
        )
        position = self._position_for_symbol(snapshot)
        if position is None:
            if len(active) == 1 and active[0]["position_id"] == position_id:
                recovery = self._recover_test_active_position(
                    active[0], snapshot
                )
                if not recovery.get("already_flat"):
                    raise RuntimeError(
                        "El stop a breakeven terminó, pero el ledger no quedó flat."
                    )
                active = self.store.get_active_execution_positions(
                    self.settings.account_id, self.settings.symbol
                )
            if not active:
                LOG.warning(
                    "EXECUTOR_TEST_MODE: stop a breakeven cerró la posición; "
                    "ciclo completado."
                )
                return True
            raise RuntimeError(
                "El stop terminó pero queda una posición lógica sin fill "
                "reconciliado."
            )
        if len(active) != 1 or active[0]["position_id"] != position_id:
            raise RuntimeError(
                "Stop a breakeven terminó con una posición no correlacionada."
            )
        actual_side = "LONG" if position["position_amt"] > 0 else "SHORT"
        expected_qty = _decimal(active[0]["open_qty"], "open_qty")
        actual_qty = abs(_decimal(position["position_amt"], "positionAmt"))
        tolerance = self._rules_cache[self.settings.symbol]["step_size"] / 2
        if actual_side != side or abs(actual_qty - expected_qty) > tolerance:
            raise RuntimeError(
                "Posición Binance difiere del ledger tras finalizar breakeven."
            )
        entry_signal = self._test_entry_signal(position_id)
        sl_id = self._ensure_protective_stop(entry_signal, snapshot)
        self._verify_protective_stop(sl_id)
        LOG.warning(
            "EXECUTOR_TEST_MODE: BE terminó con remanente; SL original "
            "confirmado para cerrar el ciclo."
        )
        return False

    def _recover_test_inflight(self) -> dict | None:
        """Recupera un ENTRY test solo tras verificarlo con Binance."""
        inflight = self.store.list_inflight_executions(
            self.settings.account_id
        )
        if not inflight:
            return None
        if len(inflight) != 1:
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: hay varias ejecuciones pendientes; "
                "requieren reconciliación manual."
            )
        execution = inflight[0]
        if execution["execution_status"] != "RECONCILIATION_REQUIRED":
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: hay una ejecución pendiente; "
                "requiere reconciliación manual."
            )
        if execution["event"] == "EXIT":
            if execution["action"] == "PARTIAL_CLOSE":
                return self._recover_test_partial_inflight(execution)
            if execution["action"] == "CLOSE":
                return self._recover_test_close_inflight(execution)
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: acción EXIT previa no recuperable."
            )
        if (
            execution["event"] != "ENTRY"
            or execution["reason"] != "EXECUTOR_TEST_ENTRY"
            or not execution["client_order_id"]
        ):
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: la ejecución previa no puede "
                "recuperarse automáticamente; requiere reconciliación manual."
            )

        if execution["order_status"] == "PREPARED":
            try:
                self.gateway.query_order(
                    self.settings.symbol, execution["client_order_id"]
                )
            except BinanceOrderNotFound:
                self.store.update_execution(
                    self.settings.account_id,
                    execution["signal_id"],
                    "REJECTED",
                    order_status="NOT_FOUND",
                    client_order_id=execution["client_order_id"],
                    error=(
                        "Test mode: se verificó que el probe PREPARED no llegó "
                        "a Binance; se libera para una nueva secuencia."
                    ),
                    reconciled_at_ms=self.clock_ms(),
                )
                return None
            except Exception as exc:
                raise RuntimeError(
                    "EXECUTOR_TEST_MODE: no se pudo verificar la orden previa; "
                    "no se enviarán nuevas órdenes."
                ) from exc
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: Binance encontró la orden PREPARED; "
                "no se reenviará ni se abrirá otra posición."
            )

        if (
            execution["order_status"] != "FILLED"
            or float(execution["filled_qty"] or 0) <= 0
        ):
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: la ENTRY previa no tiene un fill "
                "confirmado; requiere reconciliación manual."
            )

        expected_qty = _decimal(execution["filled_qty"], "filled_qty")
        order = self.gateway.query_order(
            self.settings.symbol, execution["client_order_id"]
        )
        order_status = str(_get(order, "status", "orderStatus", default="")).upper()
        order_qty = _decimal(
            _get(order, "executedQty", "executed_qty", default="0"),
            "executedQty",
        )
        tolerance = self._rules_cache[self.settings.symbol]["step_size"] / 2
        if order_status != "FILLED" or abs(order_qty - expected_qty) > tolerance:
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: la orden confirmada por Binance no "
                "coincide con el fill persistido."
            )

        snapshot = self._account_snapshot(force=True)
        position = self._position_for_symbol(snapshot)
        if snapshot.get("open_orders"):
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: no se puede verificar la posición previa "
                "de Binance; no se enviarán nuevas órdenes."
            )
        if position is None:
            if snapshot.get("open_algo_orders"):
                raise RuntimeError(
                    "EXECUTOR_TEST_MODE: la cuenta está flat pero mantiene "
                    "órdenes condicionales; requiere revisión manual."
                )
            average_price = _float(
                _get(order, "avgPrice", "averagePrice", "avg_price")
            )
            self.store.save_execution_adjustment(
                self.settings.account_id,
                self.settings.symbol,
                int(execution["position_id"]),
                f"test-flat-recovery:{execution['signal_id']}",
                float(order_qty),
                average_price,
                "EXECUTOR_TEST_MODE: fill previo confirmado y cuenta flat al recuperar",
                self.clock_ms(),
            )
            self.store.update_execution(
                self.settings.account_id,
                execution["signal_id"],
                "FILLED",
                order_status="FILLED",
                client_order_id=execution["client_order_id"],
                exchange_order_id=str(
                    _get(order, "orderId", "order_id", default="")
                ),
                filled_qty=float(order_qty),
                average_fill_price=average_price,
                result={"order": order, "recovered_flat": True},
                error=None,
                reconciled_at_ms=self.clock_ms(),
            )
            LOG.warning(
                "EXECUTOR_TEST_MODE: posición previa %s ya está flat; "
                "fill reconciliado sin enviar órdenes.",
                execution["position_id"],
            )
            return {
                "position_id": int(execution["position_id"]),
                "side": execution["side"],
                "already_flat": True,
            }

        actual_side = "LONG" if position["position_amt"] > 0 else "SHORT"
        actual_qty = abs(_decimal(position["position_amt"], "positionAmt"))
        if (
            actual_side != execution["side"]
            or abs(actual_qty - expected_qty) > tolerance
        ):
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: el fill previo no coincide con la "
                "posición real; requiere reconciliación manual."
            )

        recovered = self._record_order_result(
            execution,
            execution["client_order_id"],
            order,
            expected_qty,
            is_entry=True,
        )
        state = self.store.get_execution_state(
            self.settings.account_id, execution["signal_id"]
        )
        if not recovered or state is None or state["status"] != "FILLED":
            detail = f": {state['error']}" if state and state.get("error") else ""
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: se verificó la ENTRY previa, pero no se "
                f"pudo protegerla{detail}"
            )
        return {
            "position_id": int(execution["position_id"]),
            "side": execution["side"],
        }

    def run_test_sequence(self) -> None:
        """Ejecuta un ciclo real acotado: entrada, parcial, breakeven y cierre."""
        if not self.settings.test_mode or not self.settings.enabled:
            raise RuntimeError("La secuencia requiere EXECUTOR_TEST_MODE y trading activo")
        self._initialize()
        recovered = self._recover_test_inflight()
        snapshot = self._account_snapshot(force=True)
        if recovered is None:
            active_positions = self.store.get_active_execution_positions(
                self.settings.account_id, "BTCUSDT"
            )
            if len(active_positions) > 1:
                raise RuntimeError(
                    "EXECUTOR_TEST_MODE: ledger tiene varias posiciones activas; "
                    "requiere reconciliación manual."
                )
            if active_positions:
                recovered = self._recover_test_active_position(
                    active_positions[0], snapshot
                )
        if recovered and recovered.get("already_flat"):
            self._test_next_side = (
                "SHORT" if recovered["side"] == "LONG" else "LONG"
            )
            STOP_EVENT.set()
            LOG.warning(
                "EXECUTOR_TEST_MODE: reconciliación flat completada; "
                "modo detenido para no reabrir otra posición automáticamente."
            )
            return
        if recovered is None:
            if (
                self._position_for_symbol(snapshot) is not None
                or self.store.get_active_execution_positions(
                    self.settings.account_id, "BTCUSDT"
                )
                or snapshot.get("open_orders")
                or snapshot.get("open_algo_orders")
            ):
                raise RuntimeError(
                    "EXECUTOR_TEST_MODE: requiere BTCUSDT flat y sin órdenes "
                    "abiertas; no se colocó ninguna orden."
                )

            mark_data = _items(self.gateway.mark_price("BTCUSDT"), "data")
            mark_price = _float(
                _get(mark_data[0] if mark_data else {}, "markPrice", "mark_price")
            )
            if mark_price is None or mark_price <= 0:
                raise ValueError("EXECUTOR_TEST_MODE: markPrice inválido")
            planned_quantity = self._test_mode_entry_quantity(
                _decimal(mark_price, "mark_price"),
                _decimal(snapshot["available_balance"], "available_balance"),
                Decimal(TEST_MODE_MAX_LEVERAGE),
            )
            LOG.info(
                "EXECUTOR_TEST_MODE preflight: qty=%s notional≈%.2f USDT "
                "margen≈%.2f USDT",
                _decimal_text(planned_quantity),
                float(planned_quantity * _decimal(mark_price, "mark_price")),
                float(
                    planned_quantity
                    * _decimal(mark_price, "mark_price")
                    / Decimal(TEST_MODE_MAX_LEVERAGE)
                ),
            )

            self._test_position_sequence += 1
            position_id = self._test_position_sequence
            side = self._test_next_side
            entry_state = self._run_test_signal(
                event="ENTRY",
                action="OPEN",
                side=side,
                position_id=position_id,
                reason="EXECUTOR_TEST_ENTRY",
            )
            if entry_state["filled_qty"] <= 0:
                raise RuntimeError(
                    "EXECUTOR_TEST_MODE: ENTRY no confirmó cantidad"
                )
        else:
            position_id = recovered["position_id"]
            side = recovered["side"]

        stop_variant = (
            recovered.get("stop_variant", "sl") if recovered else "sl"
        )
        stop_id = self._algo_order_id(position_id, stop_variant)
        try:
            self._verify_protective_stop(stop_id)
        except RuntimeError:
            if stop_variant != "be":
                raise
            if self._resolve_finished_test_breakeven(position_id, side):
                self._test_next_side = (
                    "SHORT" if side == "LONG" else "LONG"
                )
                return
            stop_id = self._algo_order_id(position_id, "sl")
            self._verify_protective_stop(stop_id)

        partial_completed = bool(
            recovered and recovered.get("partial_completed")
        )
        if not partial_completed:
            partial_state = self._run_test_signal(
                event="EXIT",
                action="PARTIAL_CLOSE",
                side=side,
                position_id=position_id,
                reason="PARTIAL_TP_1R",
                close_pct=50.0,
            )
            partial_result = partial_state.get("result") or {}
            stop_id = partial_result.get(
                "protective_stop_client_algo_id"
            ) or self._algo_order_id(position_id, "sl")
            stop_variant = (
                "be"
                if stop_id == self._algo_order_id(position_id, "be")
                else "sl"
            )
        else:
            stop_variant = recovered.get("stop_variant", "sl")
            stop_id = self._algo_order_id(position_id, stop_variant)
        try:
            self._verify_protective_stop(stop_id)
        except RuntimeError:
            if stop_variant != "be":
                raise
            if self._resolve_finished_test_breakeven(position_id, side):
                self._test_next_side = (
                    "SHORT" if side == "LONG" else "LONG"
                )
                return
            stop_id = self._algo_order_id(position_id, "sl")
            self._verify_protective_stop(stop_id)

        self._run_test_signal(
            event="EXIT",
            action="CLOSE",
            side=side,
            position_id=position_id,
            reason="EXECUTOR_TEST_CLOSE",
            close_pct=100.0,
        )
        final_snapshot = self._account_snapshot(force=True)
        if (
            self._position_for_symbol(final_snapshot) is not None
            or final_snapshot.get("open_algo_orders")
        ):
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: cierre final o cancelación de stops no confirmados"
            )
        self._test_next_side = "SHORT" if side == "LONG" else "LONG"
        LOG.warning(
            "EXECUTOR_TEST_MODE completado para %s %s; siguiente ciclo en %ss",
            side,
            self.settings.symbol,
            TEST_MODE_PAUSE_SECONDS,
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
                blocked = True
        return blocked

    def run_once(self) -> None:
        self._mark_heartbeat()
        if not self.settings.enabled:
            LOG.debug(
                "BINANCE_TRADING_ENABLED=false; no se reclaman ni ejecutan señales"
            )
            return
        self._initialize()
        if self._reconcile_inflight():
            self.store.set_account_snapshot_error(
                self.settings.account_id,
                "Hay órdenes pendientes o requieren reconciliación",
            )
            return
        snapshot = self._reconcile_protective_stops(self._account_snapshot())
        consistency_error = self._account_consistency_error(snapshot)
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
        if not settings.enabled:
            logger.warning(
                "Executor iniciado en modo seguro: "
                "BINANCE_TRADING_ENABLED=false; no se enviarán órdenes."
            )
        if settings.test_mode:
            try:
                if __package__:
                    from .executor_selftest import run_executor_self_tests
                else:
                    source_root = Path(__file__).resolve().parents[2]
                    if (source_root / "live" / "bot").is_dir():
                        from live.bot.executor_selftest import (
                            run_executor_self_tests,
                        )
                    else:
                        from bot.executor_selftest import run_executor_self_tests
                selftest_count = run_executor_self_tests()
            except Exception:
                LOG.exception(
                    "Autopruebas offline del executor fallaron; "
                    "no se iniciará la secuencia Binance."
                )
                return 1
            LOG.warning(
                "Autopruebas offline del executor superadas (%s grupos); "
                "no enviaron órdenes.",
                selftest_count,
            )
            LOG.warning(
                "EXECUTOR_TEST_MODE activo: BTCUSDT, tope de margen %s USDT "
                "por orden a un máximo de %sx, stop-loss %s%%. "
                "El flujo alterna LONG/SHORT y se detiene antes de repetir "
                "el patrón.",
                TEST_MODE_MAX_MARGIN_USDT,
                TEST_MODE_MAX_LEVERAGE,
                TEST_MODE_STOP_LOSS_PCT * 100,
            )
            completed_sides: set[str] = set()
            while not STOP_EVENT.is_set():
                side = worker._test_next_side
                if side in completed_sides:
                    LOG.warning(
                        "EXECUTOR_TEST_MODE: el siguiente ciclo repetiría %s; "
                        "modo detenido.",
                        side,
                    )
                    break
                try:
                    worker.run_test_sequence()
                except Exception:
                    LOG.exception(
                        "EXECUTOR_TEST_MODE detenido; no se inicia otra secuencia"
                    )
                    return 1
                if STOP_EVENT.is_set():
                    break
                completed_sides.add(side)
                if worker._test_next_side in completed_sides:
                    LOG.warning(
                        "EXECUTOR_TEST_MODE: ciclo LONG/SHORT completado; "
                        "detenido antes de repetir %s.",
                        worker._test_next_side,
                    )
                    break
                STOP_EVENT.wait(TEST_MODE_PAUSE_SECONDS)
        else:
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
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
