"""Read-only production readiness checks for executor test mode."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .binance_usdm import BinanceUsdmClient
from .test_mode_policy import (
    TEST_MODE_MAX_LEVERAGE,
    calculate_test_entry_quantity,
    validate_test_order_notional,
)
from .worker import ExecutorSettings, _decimal, _float, _get, _items


class BinanceUsdmReadOnlyAudit:
    """Expose account and market reads only; deliberately omit order methods."""

    def __init__(self, api_key: str, api_secret: str):
        self._client = BinanceUsdmClient(api_key, api_secret, "production")

    def exchange_information(self) -> Any:
        return self._client.exchange_information()

    def mark_price(self, symbol: str) -> Any:
        return self._client.mark_price(symbol)

    def account_balance(self) -> Any:
        return self._client.account_balance()

    def account_information(self) -> Any:
        return self._client.account_information()

    def positions(self, symbol: str) -> Any:
        return self._client.positions(symbol)

    def open_orders(self, symbol: str) -> Any:
        return self._client.open_orders(symbol)

    def open_algo_orders(self, symbol: str) -> Any:
        return self._client.open_algo_orders(symbol)

    def position_mode(self) -> Any:
        return self._client.position_mode()

    def symbol_configuration(self, symbol: str) -> Any:
        return self._client.symbol_configuration(symbol)


def _symbol_rules(gateway: Any, symbol: str) -> dict:
    symbols = _items(gateway.exchange_information(), "symbols")
    item = next(
        (
            candidate
            for candidate in symbols
            if str(_get(candidate, "symbol", default="")).upper() == symbol
        ),
        None,
    )
    if item is None:
        raise ValueError(f"{symbol} no aparece como contrato en Binance production")

    filters = _items(item, "filters")
    by_type = {
        str(_get(row, "filterType", "filter_type", default="")).upper(): row
        for row in filters
    }
    lot = by_type.get("MARKET_LOT_SIZE") or by_type.get("LOT_SIZE")
    price = by_type.get("PRICE_FILTER")
    if lot is None or price is None:
        raise ValueError(f"Filtros LOT_SIZE/PRICE_FILTER incompletos para {symbol}")
    notional_filter = by_type.get("MIN_NOTIONAL") or by_type.get("NOTIONAL") or {}
    return {
        "step_size": _decimal(
            _get(lot, "stepSize", "step_size"), "stepSize"
        ),
        "min_qty": _decimal(_get(lot, "minQty", "min_qty"), "minQty"),
        "max_qty": _decimal(
            _get(lot, "maxQty", "max_qty", default="0"), "maxQty"
        ),
        "min_notional": _decimal(
            _get(
                notional_filter,
                "notional",
                "minNotional",
                "min_notional",
                default="0",
            ),
            "minNotional",
        ),
        "tick_size": _decimal(
            _get(price, "tickSize", "tick_size"), "tickSize"
        ),
    }


def _one_way_mode(gateway: Any) -> None:
    mode = gateway.position_mode()
    dual_side = _get(mode, "dualSidePosition", "dual_side_position")
    if isinstance(dual_side, str):
        dual_side = dual_side.strip().lower() == "true"
    if dual_side is None:
        raise RuntimeError("No se pudo verificar el modo de posición de production")
    if bool(dual_side):
        raise RuntimeError("La cuenta production está en Hedge Mode, no One-way Mode")


def run_production_read_only_audit(
    settings: ExecutorSettings,
    *,
    gateway: Any | None = None,
) -> dict:
    """Check production account readiness without placing or changing orders."""
    if not settings.test_mode or not settings.production_audit_enabled:
        raise RuntimeError("La auditoría production solo lectura no está habilitada")
    if settings.environment != "testnet":
        raise RuntimeError(
            "El executor de prueba debe seguir conectado a testnet durante la auditoría"
        )
    if not settings.production_audit_api_key or not settings.production_audit_api_secret:
        raise ValueError(
            "Faltan BOT_BINANCE_PRODUCTION_AUDIT_API_KEY/"
            "BOT_BINANCE_PRODUCTION_AUDIT_API_SECRET"
        )

    gateway = gateway or BinanceUsdmReadOnlyAudit(
        settings.production_audit_api_key,
        settings.production_audit_api_secret,
    )
    symbol = settings.symbol

    _one_way_mode(gateway)
    rules = _symbol_rules(gateway, symbol)
    if rules["step_size"] <= 0 or rules["min_qty"] <= 0 or rules["tick_size"] <= 0:
        raise ValueError(f"Filtros de cantidad/precio inválidos para {symbol}")

    account = gateway.account_information()
    balances = _items(gateway.account_balance(), "assets", "balances")
    usdt = next(
        (
            row
            for row in balances
            if str(_get(row, "asset", default="")).upper() == "USDT"
        ),
        {},
    )
    available_value = _float(
        _get(account, "availableBalance"),
        _float(_get(usdt, "availableBalance", "available_balance"), 0.0),
    )
    wallet_value = _float(
        _get(account, "totalMarginBalance", "totalWalletBalance"),
        _float(_get(usdt, "balance", "walletBalance"), 0.0),
    )
    if available_value is None or available_value <= 0:
        raise ValueError("La cuenta production no tiene margen USDT disponible")
    if wallet_value is None or wallet_value <= 0:
        raise ValueError("No se pudo confirmar equity USDT positivo en production")

    mark_rows = _items(gateway.mark_price(symbol), "data")
    mark = mark_rows[0] if mark_rows else gateway.mark_price(symbol)
    mark_price_value = _float(_get(mark, "markPrice", "mark_price"))
    if mark_price_value is None or mark_price_value <= 0:
        raise ValueError(f"Mark price production inválido para {symbol}")
    mark_price = _decimal(mark_price_value, "mark_price")

    positions = _items(gateway.positions(symbol), "positions")
    active_positions = [
        row
        for row in positions
        if abs(
            _float(_get(row, "positionAmt", "position_amt"), 0.0) or 0.0
        )
        > 0
        and str(_get(row, "symbol", default=symbol)).upper() == symbol
    ]
    open_orders = _items(gateway.open_orders(symbol), "orders")
    open_algo_orders = _items(gateway.open_algo_orders(symbol), "orders")
    if active_positions or open_orders or open_algo_orders:
        raise RuntimeError(
            f"{symbol} en production no está flat y sin órdenes abiertas; "
            "auditoría detenida sin modificar la cuenta."
        )

    quantity = calculate_test_entry_quantity(
        rules,
        mark_price,
        _decimal(available_value, "available_balance"),
        Decimal(TEST_MODE_MAX_LEVERAGE),
    )
    validate_test_order_notional(quantity, mark_price)
    config_rows = _items(gateway.symbol_configuration(symbol), "symbols")
    leverage = _float(_get(config_rows[0], "leverage")) if config_rows else None
    notional = quantity * mark_price
    estimated_margin = notional / Decimal(TEST_MODE_MAX_LEVERAGE)
    return {
        "environment": "production",
        "symbol": symbol,
        "position_mode": "ONE_WAY",
        "flat": True,
        "open_orders": 0,
        "open_algo_orders": 0,
        "wallet_balance_usdt": wallet_value,
        "available_balance_usdt": available_value,
        "mark_price": float(mark_price),
        "configured_leverage": leverage,
        "test_quantity": format(quantity.normalize(), "f"),
        "test_notional_usdt": float(notional),
        "estimated_margin_usdt": float(estimated_margin),
        "orders_sent": 0,
    }
