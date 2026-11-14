"""Límites de sizing y órdenes específicos del modo de prueba."""

from __future__ import annotations

from decimal import ROUND_UP, Decimal

TEST_MODE_MAX_MARGIN_USDT = Decimal(11)
TEST_MODE_MAX_LEVERAGE = 10
TEST_MODE_STOP_LOSS_PCT = 0.01
TEST_MODE_PAUSE_SECONDS = 10


def calculate_test_entry_quantity(
    rules: dict,
    price: Decimal,
    available: Decimal,
    leverage: Decimal,
) -> Decimal:
    """Calcula el mínimo tamaño que permite validar un cierre parcial del 50%."""
    step = rules["step_size"]
    min_close_qty = max(
        rules["min_qty"],
        rules["min_notional"] / price
        if rules["min_notional"] > 0
        else Decimal(0),
    )
    close_steps = (min_close_qty / step).to_integral_value(rounding=ROUND_UP)
    quantity = close_steps * step * 2
    margin = quantity * price / leverage
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


def validate_test_order_notional(quantity: Decimal, mark_price: Decimal) -> None:
    """Aplica el tope de notional a cada orden emitida durante el test mode."""
    max_notional = TEST_MODE_MAX_MARGIN_USDT * TEST_MODE_MAX_LEVERAGE
    order_notional = quantity * mark_price
    if order_notional > max_notional:
        raise ValueError(
            "EXECUTOR_TEST_MODE: la orden supera el equivalente a "
            f"{TEST_MODE_MAX_MARGIN_USDT:.2f} USDT de margen a "
            f"{TEST_MODE_MAX_LEVERAGE}x; no se envió."
        )
