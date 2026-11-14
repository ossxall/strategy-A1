"""Envía una orden manual BTCUSDT USDⓈ-M con stop y siete TPs parciales.

Edita los parámetros de operación de abajo antes de ejecutar.
"""

from __future__ import annotations

import os
import sys
import time
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
LIVE_DIR = REPO_DIR / "live"
if str(LIVE_DIR) not in sys.path:
    sys.path.insert(0, str(LIVE_DIR))

from bot.binance_usdm import BinanceUsdmClient, response_data

SYMBOL = "BTCUSDT"
LEVERAGE = 10
STOP_DISTANCE = Decimal("0.01")  # 1,0 %
TP_STEP = Decimal("0.005")  # 0,5 %
TP_COUNT = 7

# Parámetros de operación: cambia estos valores directamente en el archivo.
SIDE = "LONG"  # "LONG" o "SHORT"
CAPITAL_USDT = Decimal(60)  # margen aislado objetivo; a 10x ≈ 100 USDT nominales
USE_LIVE = False  # False = Futures Testnet; True = producción (requiere confirmación)


def as_decimal(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} no es un número válido: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"{label} debe ser finito")
    return result


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def round_to_tick(value: Decimal, tick: Decimal, rounding: str) -> Decimal:
    if tick <= 0:
        raise ValueError("El tickSize recibido de Binance no es válido")
    units = (value / tick).to_integral_value(rounding=rounding)
    return units * tick


def as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(by_alias=True, mode="json")
    if hasattr(value, "dict") and callable(value.dict):
        return value.dict(by_alias=True)
    raise ValueError(f"Respuesta inesperada del SDK: {type(value).__name__}")


def as_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [as_mapping(row) for row in value]
    if isinstance(value, dict):
        # Algunos endpoints envuelven listas bajo una clave cuyo nombre varía.
        for key in ("data", "orders", "positions", "symbols"):
            rows = value.get(key)
            if isinstance(rows, list):
                return [as_mapping(row) for row in rows]
    raise ValueError("Binance devolvió un formato de lista inesperado")


def symbol_rules(
    client: BinanceUsdmClient,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    info = as_mapping(client.exchange_information())
    symbols = info.get("symbols")
    if not isinstance(symbols, list):
        raise TypeError("No se encontraron símbolos en exchange_information")

    row = next(
        (
            as_mapping(item)
            for item in symbols
            if as_mapping(item).get("symbol") == SYMBOL
        ),
        None,
    )
    if row is None:
        raise ValueError(f"{SYMBOL} no está disponible en este entorno de Binance")

    filters = {
        str(as_mapping(item).get("filterType")): as_mapping(item)
        for item in row.get("filters", [])
    }
    price_filter = filters.get("PRICE_FILTER")
    lot_filter = filters.get("LOT_SIZE")
    market_lot_filter = filters.get("MARKET_LOT_SIZE", lot_filter)
    if not price_filter or not lot_filter or not market_lot_filter:
        raise ValueError(
            f"Binance no devolvió filtros de precio/cantidad para {SYMBOL}"
        )

    tick = as_decimal(price_filter.get("tickSize"), "tickSize")
    # Se usa el filtro más restrictivo para que la cantidad sirva tanto para
    # la entrada de mercado como para los cierres condicionales.
    step = max(
        as_decimal(lot_filter.get("stepSize"), "LOT_SIZE.stepSize"),
        as_decimal(market_lot_filter.get("stepSize"), "MARKET_LOT_SIZE.stepSize"),
    )
    min_qty = max(
        as_decimal(lot_filter.get("minQty", "0"), "LOT_SIZE.minQty"),
        as_decimal(market_lot_filter.get("minQty", "0"), "MARKET_LOT_SIZE.minQty"),
    )
    min_notional = Decimal(0)
    for filter_name in ("MIN_NOTIONAL", "NOTIONAL"):
        notional_filter = filters.get(filter_name)
        if notional_filter:
            candidate = notional_filter.get(
                "notional", notional_filter.get("minNotional", "0")
            )
            min_notional = max(
                min_notional, as_decimal(candidate, f"{filter_name}.notional")
            )
    return tick, step, min_qty, min_notional


def get_mark_price(client: BinanceUsdmClient) -> Decimal:
    response = client.mark_price(SYMBOL)
    if isinstance(response, list):
        response = next(
            (row for row in response if as_mapping(row).get("symbol") == SYMBOL),
            None,
        )
    data = as_mapping(response)
    price = data.get("markPrice", data.get("mark_price"))
    result = as_decimal(price, "mark price")
    if result <= 0:
        raise ValueError("Binance devolvió un mark price no positivo")
    return result


def ensure_one_way_and_flat(client: BinanceUsdmClient) -> None:
    mode = as_mapping(client.position_mode())
    dual_side = mode.get("dualSidePosition", mode.get("dual_side_position"))
    if isinstance(dual_side, str):
        dual_side = dual_side.lower() == "true"
    if dual_side is not False:
        raise RuntimeError(
            "Este script requiere modo One-way (dualSidePosition=false); "
            "no se enviaron órdenes."
        )

    for position in as_rows(client.positions(SYMBOL)):
        amount = as_decimal(position.get("positionAmt", "0"), "positionAmt")
        if amount != 0:
            raise RuntimeError(
                f"Ya existe una posición {SYMBOL} abierta ({amount} BTC); "
                "se cancela para no mezclar operaciones."
            )

    for method_name in ("open_orders", "open_algo_orders"):
        orders = as_rows(getattr(client, method_name)(SYMBOL))
        if orders:
            raise RuntimeError(
                f"Hay órdenes {SYMBOL} abiertas ({method_name}); "
                "revísalas antes de operar."
            )


def quantity_for_capital(
    capital: Decimal,
    mark_price: Decimal,
    step: Decimal,
    min_qty: Decimal,
    min_notional: Decimal,
) -> Decimal:
    """Calcula BTC para el margen objetivo, redondeando sin excederlo."""
    target_notional = capital * LEVERAGE
    quantity = (target_notional / mark_price / step).to_integral_value(
        rounding=ROUND_DOWN
    ) * step
    if quantity < min_qty or (min_notional and quantity * mark_price < min_notional):
        min_required_qty = max(min_qty, min_notional / mark_price)
        min_required_qty = (min_required_qty / step).to_integral_value(
            rounding=ROUND_UP
        ) * step
        min_required_capital = min_required_qty * mark_price / LEVERAGE
        raise ValueError(
            f"{capital} USDT de margen no alcanza los mínimos actuales de Binance. "
            f"El mínimo estimado es {min_required_capital:.4f} USDT de margen "
            f"(cantidad {min_required_qty} BTC). No se envió la entrada."
        )
    return quantity


def plan_take_profits(
    quantity: Decimal,
    entry_price: Decimal,
    side: str,
    tick: Decimal,
    step: Decimal,
    min_qty: Decimal,
) -> list[tuple[Decimal, Decimal]]:
    """Reparte en TPs reduce-only; aplica cantidad mínima y stepSize, no MIN_NOTIONAL."""
    total_steps = int(quantity / step)
    for count in range(min(TP_COUNT, total_steps), 0, -1):
        base_steps, remainder = divmod(total_steps, count)
        chunks = [
            step * (base_steps + (1 if index < remainder else 0))
            for index in range(count)
        ]
        plan: list[tuple[Decimal, Decimal]] = []
        valid = True
        for index, chunk in enumerate(chunks):
            movement = TP_STEP * (index + 1)
            raw_target = (
                entry_price * (Decimal(1) + movement)
                if side == "LONG"
                else entry_price * (Decimal(1) - movement)
            )
            target = round_to_tick(
                raw_target, tick, ROUND_DOWN if side == "LONG" else ROUND_UP
            )
            if chunk < min_qty:
                valid = False
                break
            plan.append((target, chunk))
        if valid and sum((chunk for _, chunk in plan), Decimal(0)) == quantity:
            return plan
    raise ValueError(
        "La posición no alcanza para colocar ni un TP válido con los filtros "
        "actuales de Binance."
    )


def set_isolated_margin(client: BinanceUsdmClient) -> None:
    config = client.symbol_configuration(SYMBOL)
    rows = as_rows(config) if isinstance(config, list) else [as_mapping(config)]
    symbol_config = next(
        (row for row in rows if row.get("symbol") == SYMBOL),
        None,
    )
    if symbol_config is None:
        raise RuntimeError(f"No se pudo consultar la configuración de {SYMBOL}")
    margin_type = str(
        symbol_config.get("marginType", symbol_config.get("margin_type", ""))
    ).upper()
    if margin_type == "ISOLATED":
        return
    if margin_type != "CROSSED":
        raise RuntimeError(f"Tipo de margen desconocido para {SYMBOL}: {margin_type!r}")
    response_data(client.rest.change_margin_type(symbol=SYMBOL, margin_type="ISOLATED"))


def check_confirm(
    environment: str,
    side: str,
    capital: Decimal,
    quantity: Decimal,
    notional: Decimal,
    tp_count: int,
) -> None:
    print(
        f"\nEntorno: {environment.upper()} | {SYMBOL} {side} | "
        f"margen aislado objetivo: {capital} USDT | "
        f"nominal estimado: {notional:.2f} USDT | "
        f"cantidad: {quantity} BTC | TPs parciales: {tp_count}"
    )
    if environment == "production":
        expected = "OPERAR BTCUSDT EN REAL"
        answer = input(f"Escribe «{expected}» para continuar: ").strip()
        if answer != expected:
            raise RuntimeError("Confirmación de producción incorrecta; no se operó.")
    else:
        answer = input("Escribe SI para enviar la orden a Testnet: ").strip().upper()
        if answer != "SI":
            raise RuntimeError("Operación cancelada; no se enviaron órdenes.")


def load_api_credentials() -> tuple[str, str]:
    dotenv_path = SCRIPT_DIR / ".env"
    if not dotenv_path.is_file():
        raise FileNotFoundError(f"No se encontró el archivo {dotenv_path}")
    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        raise RuntimeError(
            "Falta python-dotenv. Instálalo con: pip install -r live/requirements.txt"
        ) from exc

    if not load_dotenv(dotenv_path=dotenv_path, override=True):
        raise ValueError(f"No se pudieron cargar variables desde {dotenv_path}")

    api_key = os.getenv("BINANCE_API_KEY", "").strip()
    api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
    if not api_key or not api_secret:
        raise ValueError(
            f"Define BINANCE_API_KEY y BINANCE_API_SECRET en {dotenv_path}"
        )
    return api_key, api_secret


def place_algo_order(client: BinanceUsdmClient, **kwargs: Any) -> Any:
    return response_data(client.rest.new_algo_order(**kwargs))


def execute_trade(
    client: BinanceUsdmClient,
    side: str,
    capital: Decimal,
    tick: Decimal,
    step: Decimal,
    min_qty: Decimal,
    min_notional: Decimal,
) -> None:
    entry_side = "BUY" if side == "LONG" else "SELL"
    exit_side = "SELL" if side == "LONG" else "BUY"

    initial_mark = get_mark_price(client)
    quantity = quantity_for_capital(capital, initial_mark, step, min_qty, min_notional)
    initial_tps = plan_take_profits(quantity, initial_mark, side, tick, step, min_qty)
    initial_quantity = quantity
    check_confirm(
        client.environment,
        side,
        capital,
        quantity,
        quantity * initial_mark,
        len(initial_tps),
    )

    set_isolated_margin(client)
    client.change_leverage(SYMBOL, LEVERAGE)
    entry_reference = get_mark_price(client)
    quantity = quantity_for_capital(
        capital, entry_reference, step, min_qty, min_notional
    )
    pre_entry_tps = plan_take_profits(
        quantity, entry_reference, side, tick, step, min_qty
    )
    if quantity != initial_quantity or len(pre_entry_tps) != len(initial_tps):
        print(
            "Aviso: el precio se movió; el tamaño o el número de TPs cambió "
            "antes de la entrada."
        )
    if len(pre_entry_tps) < TP_COUNT:
        print(
            f"Con el margen y los filtros actuales se pueden colocar "
            f"{len(pre_entry_tps)} TPs parciales de {TP_COUNT}."
        )

    order_id = f"manual-{side.lower()}-{time.time_ns()}"
    entry = as_mapping(
        response_data(
            client.rest.new_order(
                symbol=SYMBOL,
                side=entry_side,
                type="MARKET",
                quantity=float(quantity),
                new_client_order_id=order_id,
                new_order_resp_type="RESULT",
            )
        )
    )
    executed = as_decimal(entry.get("executedQty") or quantity, "executedQty")
    if executed <= 0:
        raise RuntimeError(f"La orden de entrada no se ejecutó: {entry}")

    entry_price = as_decimal(entry.get("avgPrice") or "0", "avgPrice")
    if entry_price <= 0:
        entry_price = entry_reference
    stop_reference = entry_reference
    raw_stop = (
        stop_reference * (Decimal(1) - STOP_DISTANCE)
        if side == "LONG"
        else stop_reference * (Decimal(1) + STOP_DISTANCE)
    )
    stop_rounding = ROUND_UP if side == "LONG" else ROUND_DOWN
    stop_price = round_to_tick(raw_stop, tick, stop_rounding)

    print(f"\nEntrada ejecutada: {executed} BTC a ~{entry_price} USDT")
    print(
        f"Margen aislado estimado: {executed * entry_price / LEVERAGE:.4f} USDT "
        f"(nominal: {executed * entry_price:.2f} USDT)"
    )
    print(f"Stop (mark price ±{STOP_DISTANCE * 100}%): {stop_price} USDT")

    stop_id = f"manual-stop-{time.time_ns()}"
    try:
        place_algo_order(
            client,
            algo_type="CONDITIONAL",
            symbol=SYMBOL,
            side=exit_side,
            type="STOP_MARKET",
            trigger_price=float(stop_price),
            working_type="MARK_PRICE",
            close_position="true",
            client_algo_id=stop_id,
        )
    except Exception as exc:
        print(
            f"ERROR: no se pudo crear el stop ({exc}). Intentando cerrar la posición."
        )
        try:
            client.place_market_order(
                symbol=SYMBOL,
                side=exit_side,
                quantity=decimal_text(executed),
                client_order_id=f"manual-emergency-{time.time_ns()}",
                reduce_only=True,
            )
            print("La orden de cierre de emergencia fue enviada.")
        except Exception as close_exc:  # noqa: BLE001 - surface SDK errors after failed close
            print(
                "CRÍTICO: tampoco se pudo cerrar la posición automáticamente. "
                f"Intervén en Binance ahora: {close_exc}",
                file=sys.stderr,
            )
        raise RuntimeError("No se dejó abierta una operación sin stop.") from exc

    # El stop ya protege la posición antes de intentar crear los TPs parciales.
    executed = (executed / step).to_integral_value(rounding=ROUND_DOWN) * step
    tp_plan = plan_take_profits(executed, entry_price, side, tick, step, min_qty)
    try:
        for index, (target, chunk) in enumerate(tp_plan):
            print(f"TP{index + 1}: {target} USDT | cierre {chunk} BTC")
            tp_id = f"manual-tp{index + 1}-{time.time_ns()}"
            place_algo_order(
                client,
                algo_type="CONDITIONAL",
                symbol=SYMBOL,
                side=exit_side,
                type="TAKE_PROFIT_MARKET",
                quantity=float(chunk),
                trigger_price=float(target),
                reduce_only="true",
                working_type="MARK_PRICE",
                client_algo_id=tp_id,
            )
    except Exception as exc:
        print(
            f"ERROR al colocar un TP ({exc}). El stop sigue activo y se "
            "mantendrán los TPs ya aceptados; revisa las órdenes en Binance.",
            file=sys.stderr,
        )
        raise RuntimeError("Protección TP incompleta; revisa la cuenta.") from exc

    print(
        f"\nÓrdenes enviadas: entrada {order_id}, stop {stop_id}, {len(tp_plan)} TP(s)."
    )
    print("Comprueba en Binance que el stop y los TPs figuren como abiertos.")


def main() -> int:
    try:
        if len(sys.argv) > 1:
            raise ValueError(
                "Este script no acepta argumentos; edita SIDE, CAPITAL_USDT y "
                "USE_LIVE en el archivo."
            )
        side = SIDE.upper()
        if side not in {"LONG", "SHORT"}:
            raise ValueError('SIDE debe ser "LONG" o "SHORT"')
        capital = as_decimal(CAPITAL_USDT, "CAPITAL_USDT")
        if capital <= 0:
            raise ValueError("CAPITAL_USDT debe ser mayor que cero")

        # scripts/.env es la fuente de credenciales de este ejecutor.
        api_key, api_secret = load_api_credentials()

        environment = "production" if USE_LIVE else "testnet"
        client = BinanceUsdmClient(
            api_key=api_key,
            api_secret=api_secret,
            environment=environment,
        )

        ensure_one_way_and_flat(client)
        tick, step, min_qty, min_notional = symbol_rules(client)
        execute_trade(
            client,
            side,
            capital,
            tick,
            step,
            min_qty,
            min_notional,
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - present API/SDK failures to CLI users
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
