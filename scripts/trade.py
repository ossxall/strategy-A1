"""Simula BTCUSDT en mainnet; con --confirm envía MARKET y su stop."""

from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
LIVE_DIR = REPO_DIR / "live"
for directory in (REPO_DIR, LIVE_DIR):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

SYMBOL = "BTCUSDT"
SIDE = "LONG"  # "LONG" o "SHORT"
RISK_PCT = Decimal("0.01")
STOP_DISTANCE = Decimal("0.01")
LEVERAGE = 10
ENVIRONMENT = "production"


def as_decimal(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} no es un número válido: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"{label} debe ser finito")
    return result


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
        for key in ("data", "orders", "positions", "symbols", "assets", "balances"):
            rows = value.get(key)
            if isinstance(rows, list):
                return [as_mapping(row) for row in rows]
        if "symbol" in value:
            return [value]
    raise ValueError("Binance devolvió un formato de lista inesperado")


def first_value(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return value
    return None


def floor_to_step(quantity: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("Binance devolvió un stepSize no válido")
    return (quantity / step).to_integral_value(rounding=ROUND_DOWN) * step


def round_to_tick(price: Decimal, tick: Decimal, rounding: str) -> Decimal:
    if tick <= 0:
        raise ValueError("Binance devolvió un tickSize no válido")
    return (price / tick).to_integral_value(rounding=rounding) * tick


def load_script_env() -> dict[str, str]:
    dotenv_path = SCRIPT_DIR / ".env"
    try:
        from dotenv import dotenv_values
    except ImportError as exc:
        raise RuntimeError(
            "Falta python-dotenv. Instálalo con: pip install -r live/requirements.txt"
        ) from exc

    if not dotenv_path.is_file():
        raise FileNotFoundError(f"No se encontró el archivo {dotenv_path}")
    values = dotenv_values(dotenv_path)
    return {
        str(key): str(value)
        for key, value in values.items()
        if key and value is not None
    }


def load_api_credentials(env_values: dict[str, str]) -> tuple[str, str]:
    api_key = (
        env_values.get("BOT_BINANCE_API_KEY")
        or env_values.get("BINANCE_API_KEY")
        or ""
    ).strip()
    api_secret = (
        env_values.get("BOT_BINANCE_API_SECRET")
        or env_values.get("BINANCE_API_SECRET")
        or ""
    ).strip()
    if not api_key or not api_secret:
        raise ValueError(
            f"Define BOT_BINANCE_API_KEY y BOT_BINANCE_API_SECRET en "
            f"{SCRIPT_DIR / '.env'}"
        )
    return api_key, api_secret


def account_balances(client: BinanceUsdmClient) -> tuple[Decimal, Decimal]:
    account = as_mapping(client.account_information())
    balances = as_rows(client.account_balance())
    usdt = next(
        (row for row in balances if str(row.get("asset", "")).upper() == "USDT"),
        {},
    )

    wallet_value = first_value(
        account, "totalMarginBalance", "totalWalletBalance"
    )
    if wallet_value is None:
        wallet_value = first_value(usdt, "balance", "walletBalance")
    available_value = first_value(account, "availableBalance")
    if available_value is None:
        available_value = first_value(
            usdt, "availableBalance", "available_balance"
        )

    wallet = as_decimal(wallet_value, "wallet balance")
    available = as_decimal(available_value, "available balance")
    if wallet <= 0 or available <= 0:
        raise ValueError("La cuenta Futures no tiene saldo USDT disponible")
    return wallet, available


def mark_price(client: BinanceUsdmClient) -> Decimal:
    response = client.mark_price(SYMBOL)
    if isinstance(response, list):
        response = next(
            (
                row
                for row in response
                if as_mapping(row).get("symbol") == SYMBOL
            ),
            None,
        )
    data = as_mapping(response)
    price = as_decimal(
        first_value(data, "markPrice", "mark_price"), "mark price"
    )
    if price <= 0:
        raise ValueError("Binance devolvió un mark price no positivo")
    return price


def symbol_configuration(client: BinanceUsdmClient) -> dict[str, Any]:
    rows = as_rows(client.symbol_configuration(SYMBOL))
    row = next(
        (item for item in rows if str(item.get("symbol", "")).upper() == SYMBOL),
        None,
    )
    if row is None:
        raise ValueError(f"No se encontró la configuración de {SYMBOL}")
    return row


def symbol_rules(client: BinanceUsdmClient) -> dict[str, Decimal]:
    info = as_mapping(client.exchange_information())
    symbols = info.get("symbols")
    if not isinstance(symbols, list):
        raise TypeError("No se encontraron símbolos en exchange_information")
    symbol = next(
        (
            as_mapping(item)
            for item in symbols
            if str(as_mapping(item).get("symbol", "")).upper() == SYMBOL
        ),
        None,
    )
    if symbol is None:
        raise ValueError(f"{SYMBOL} no está disponible en Binance mainnet")

    filters = {
        str(as_mapping(item).get("filterType", "")).upper(): as_mapping(item)
        for item in symbol.get("filters", [])
    }
    lot = filters.get("MARKET_LOT_SIZE") or filters.get("LOT_SIZE")
    price_filter = filters.get("PRICE_FILTER")
    notional_filter = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL") or {}
    if not lot or not price_filter:
        raise ValueError(f"Faltan filtros de precio/cantidad para {SYMBOL}")

    return {
        "tick_size": as_decimal(price_filter.get("tickSize"), "tickSize"),
        "step_size": as_decimal(lot.get("stepSize"), "stepSize"),
        "min_qty": as_decimal(lot.get("minQty", "0"), "minQty"),
        "max_qty": as_decimal(lot.get("maxQty", "0"), "maxQty"),
        "min_notional": as_decimal(
            notional_filter.get(
                "notional", notional_filter.get("minNotional", "0")
            ),
            "minNotional",
        ),
    }


def require_one_way_and_flat(client: Any) -> None:
    mode = as_mapping(client.position_mode())
    dual_side = first_value(mode, "dualSidePosition", "dual_side_position")
    if isinstance(dual_side, str):
        dual_side = dual_side.lower() == "true"
    if dual_side is not False:
        raise RuntimeError("Se requiere modo One-way en Binance.")

    for position in as_rows(client.positions(SYMBOL)):
        amount = as_decimal(
            first_value(position, "positionAmt", "position_amt") or "0",
            "positionAmt",
        )
        if amount != 0:
            raise RuntimeError(
                f"Ya existe una posición abierta en {SYMBOL}; "
                "no se abre otra entrada."
            )

    for method_name in ("open_orders", "open_algo_orders"):
        if as_rows(getattr(client, method_name)(SYMBOL)):
            raise RuntimeError(
                f"Hay órdenes abiertas en {SYMBOL} ({method_name}); "
                "no se abre otra entrada."
            )


def simulate_entry(
    client: Any, *, will_send: bool = False
) -> dict[str, Any]:
    if SIDE.upper() not in {"LONG", "SHORT"}:
        raise ValueError('SIDE debe ser "LONG" o "SHORT"')
    if not 0 < RISK_PCT <= Decimal("0.20"):
        raise ValueError("RISK_PCT debe estar en (0, 0.20]")
    if not 0 < STOP_DISTANCE < 1:
        raise ValueError("STOP_DISTANCE debe estar en (0, 1)")

    wallet, available = account_balances(client)
    price = mark_price(client)
    rules = symbol_rules(client)
    config = symbol_configuration(client)
    current_leverage_raw = config.get("leverage")
    current_leverage = (
        as_decimal(current_leverage_raw, "leverage de Binance")
        if current_leverage_raw is not None
        else None
    )

    from strategy.config import Config

    strategy_config = Config()
    commission_bps = as_decimal(strategy_config.commission_bps, "commission_bps")
    slippage_bps = as_decimal(strategy_config.slippage_bps, "slippage_bps")

    risk_budget = available * RISK_PCT
    side = SIDE.upper()
    raw_stop = (
        price * (Decimal(1) - STOP_DISTANCE)
        if side == "LONG"
        else price * (Decimal(1) + STOP_DISTANCE)
    )
    slippage = slippage_bps / Decimal(10_000)
    stop_execution = raw_stop * (
        Decimal(1) - slippage if side == "LONG" else Decimal(1) + slippage
    )
    risk_per_unit = abs(stop_execution - price) + (
        price + abs(stop_execution)
    ) * commission_bps / Decimal(10_000)
    if risk_per_unit <= 0:
        raise ValueError("risk_per_unit no es positivo")

    quantity_by_risk = risk_budget / risk_per_unit
    effective_leverage = Decimal(LEVERAGE)
    max_notional = min(wallet, available) * effective_leverage
    quantity_by_leverage = max_notional / price
    quantity = floor_to_step(
        min(quantity_by_risk, quantity_by_leverage), rules["step_size"]
    )
    if rules["max_qty"] > 0 and quantity > rules["max_qty"]:
        quantity = floor_to_step(rules["max_qty"], rules["step_size"])
    if quantity < rules["min_qty"]:
        raise ValueError("La cantidad calculada está por debajo del minQty de Binance")
    if rules["min_notional"] > 0 and quantity * price < rules["min_notional"]:
        raise ValueError(
            "El nominal calculado está por debajo del mínimo de Binance"
        )

    trigger = round_to_tick(
        raw_stop,
        rules["tick_size"],
        ROUND_DOWN if side == "LONG" else ROUND_UP,
    )
    notional = quantity * price
    estimated_margin = notional / effective_leverage
    estimated_stop_loss = quantity * risk_per_unit

    print(
        "\nPLAN DE ENTRADA MAINNET"
        if will_send
        else "\nSIMULACIÓN SOLO LECTURA — BINANCE FUTURES MAINNET"
    )
    if will_send:
        print("Con --confirm se enviará una entrada MARKET y su stop protector.")
    else:
        print("No se enviará ninguna orden ni se cambiará el leverage/margen.")
    print(f"Símbolo/lado: {SYMBOL} {side}")
    print(
        f"Saldo wallet: {wallet:.4f} USDT | disponible: {available:.4f} USDT"
    )
    print(
        f"Risk pct: {RISK_PCT:.2%} | presupuesto de riesgo: "
        f"{risk_budget:.4f} USDT"
    )
    print(
        f"Leverage objetivo del script: {LEVERAGE}x | "
        f"leverage actual en Binance: "
        f"{current_leverage if current_leverage is not None else 'no disponible'}x"
    )
    if current_leverage is not None and current_leverage != effective_leverage:
        print(
            "Aviso: el executor cambiaría el leverage a su valor objetivo "
            "antes de enviar una entrada; esta simulación no lo modifica."
        )
    print(f"Mark price de referencia: {price:.8f} USDT")
    print(f"Cantidad estimada: {quantity} BTC")
    print(f"Nominal estimado: {notional:.4f} USDT")
    print(f"Margen inicial estimado: {estimated_margin:.4f} USDT")
    print(f"Stop estimado (1% desde la referencia): {trigger} USDT")
    print(f"Pérdida estimada al stop, con costes: {estimated_stop_loss:.4f} USDT")
    print(
        f"Filtros aplicados: stepSize={rules['step_size']}, "
        f"minQty={rules['min_qty']}, minNotional={rules['min_notional']}"
    )
    return {
        "side": side,
        "quantity": quantity,
        "mark_price": price,
        "stop_distance": STOP_DISTANCE,
        "tick_size": rules["tick_size"],
        "notional": notional,
    }


def require_isolated(client: Any) -> None:
    config = symbol_configuration(client)
    margin_type = str(
        first_value(config, "marginType", "margin_type") or ""
    ).upper()
    if margin_type != "ISOLATED":
        raise RuntimeError(
            f"{SYMBOL} debe estar ya en margen Isolated "
            f"(Binance informa {margin_type or 'desconocido'}); "
            "el script no cambiará el tipo de margen."
        )


def set_target_leverage(client: Any) -> None:
    config = symbol_configuration(client)
    current = first_value(config, "leverage")
    if current is not None and int(current) == LEVERAGE:
        print(f"Leverage de {SYMBOL} ya está en {LEVERAGE}x.")
        return

    result = as_mapping(client.change_leverage(SYMBOL, LEVERAGE))
    confirmed = first_value(result, "leverage")
    if confirmed is not None and int(confirmed) != LEVERAGE:
        raise RuntimeError(
            f"Binance confirmó leverage {confirmed}x; se solicitó {LEVERAGE}x."
        )
    print(f"Leverage de {SYMBOL} configurado a {LEVERAGE}x.")


def send_entry_and_stop(client: Any, plan: dict[str, Any]) -> None:
    side = plan["side"]
    entry_side = "BUY" if side == "LONG" else "SELL"
    exit_side = "SELL" if side == "LONG" else "BUY"
    entry_client_id = f"tb-entry-{time.time_ns()}"
    try:
        response = client.place_market_order(
            symbol=SYMBOL,
            side=entry_side,
            quantity=format(plan["quantity"], "f"),
            client_order_id=entry_client_id,
            reduce_only=False,
        )
    except Exception as exc:
        try:
            response = client.query_order(SYMBOL, entry_client_id)
        except Exception as query_exc:
            raise RuntimeError(
                "La entrada falló o su resultado es incierto. Revisa Binance "
                "antes de volver a ejecutar; clientOrderId="
                f"{entry_client_id}. Error entrada: {exc}; consulta: {query_exc}"
            ) from exc

    order = as_mapping(response)
    executed_raw = first_value(order, "executedQty", "executed_qty")
    executed = (
        as_decimal(executed_raw, "executedQty")
        if executed_raw is not None
        else Decimal(0)
    )
    actual_position = None
    if executed <= 0:
        actual_position = next(
            (
                row
                for row in as_rows(client.positions(SYMBOL))
                if as_decimal(
                    first_value(row, "positionAmt", "position_amt") or "0",
                    "positionAmt",
                )
                != 0
            ),
            None,
        )
        if actual_position is not None:
            executed = abs(
                as_decimal(
                    first_value(actual_position, "positionAmt", "position_amt"),
                    "positionAmt",
                )
            )
    if executed <= 0:
        raise RuntimeError(
            "Binance no confirmó una cantidad ejecutada; revisa la orden y la "
            "posición antes de reintentar."
        )
    fill_raw = first_value(order, "avgPrice", "averagePrice", "avg_price")
    if fill_raw is None or as_decimal(fill_raw, "avgPrice") <= 0:
        if actual_position is None:
            actual_position = next(
                (
                    row
                    for row in as_rows(client.positions(SYMBOL))
                    if as_decimal(
                        first_value(row, "positionAmt", "position_amt") or "0",
                        "positionAmt",
                    )
                    != 0
                ),
                None,
            )
        fill_raw = (
            first_value(actual_position, "entryPrice", "entry_price")
            if actual_position is not None
            else plan["mark_price"]
        )
    fill_price = as_decimal(fill_raw, "avgPrice")
    if fill_price <= 0:
        raise RuntimeError(
            "No se pudo determinar el precio de entrada para colocar el stop; "
            "revisa Binance inmediatamente."
        )
    raw_stop = (
        fill_price * (Decimal(1) - plan["stop_distance"])
        if side == "LONG"
        else fill_price * (Decimal(1) + plan["stop_distance"])
    )
    stop_price = round_to_tick(
        raw_stop,
        plan["tick_size"],
        ROUND_DOWN if side == "LONG" else ROUND_UP,
    )
    stop_client_id = f"tb-stop-{time.time_ns()}"
    try:
        client.rest.new_algo_order(
            algo_type="CONDITIONAL",
            symbol=SYMBOL,
            side=exit_side,
            type="STOP_MARKET",
            trigger_price=float(stop_price),
            working_type="MARK_PRICE",
            close_position="true",
            client_algo_id=stop_client_id,
        )
    except Exception as exc:
        print(
            f"CRÍTICO: falló el stop ({exc}); se solicita cierre MARKET "
            "reduce-only de emergencia.",
            file=sys.stderr,
        )
        try:
            client.place_market_order(
                symbol=SYMBOL,
                side=exit_side,
                quantity=format(executed, "f"),
                client_order_id=f"tb-emergency-{time.time_ns()}",
                reduce_only=True,
            )
        except Exception as close_exc:
            raise RuntimeError(
                "Fallaron el stop y el cierre de emergencia. "
                "Revisa Binance inmediatamente."
            ) from close_exc
        raise RuntimeError(
            "Falló el stop; se solicitó cierre de emergencia. "
            "Verifica que la posición esté plana."
        ) from exc

    print("\nENTRADA Y STOP ENVIADOS")
    print(f"clientOrderId: {entry_client_id}")
    print(f"Cantidad ejecutada: {executed} BTC")
    print(f"Precio promedio de entrada: {fill_price:.8f} USDT")
    print(f"Nominal ejecutado: {executed * fill_price:.4f} USDT")
    print(f"Stop protector (1% desde el fill): {stop_price} USDT")
    print(f"clientAlgoId del stop: {stop_client_id}")


def load_binance_client_class():
    module_path = LIVE_DIR / "bot" / "execution" / "binance_usdm.py"
    spec = importlib.util.spec_from_file_location(
        "_trade_btcusdt_binance_usdm", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"No se pudo cargar el adaptador Binance: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.BinanceUsdmClient


def main() -> int:
    try:
        parser = argparse.ArgumentParser(
            description=(
                "Simula una entrada BTCUSDT en mainnet; con --confirm "
                "configura 10x y envía MARKET con stop protector del 1%."
            )
        )
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="configura el leverage a 10x y envía la entrada real con stop",
        )
        args = parser.parse_args()

        env_values = load_script_env()
        api_key, api_secret = load_api_credentials(env_values)
        client_class = load_binance_client_class()
        client = client_class(
            api_key=api_key,
            api_secret=api_secret,
            environment=ENVIRONMENT,
        )
        require_one_way_and_flat(client)
        if args.confirm:
            require_isolated(client)
            set_target_leverage(client)
        plan = simulate_entry(client, will_send=args.confirm)
        if args.confirm:
            require_one_way_and_flat(client)
            send_entry_and_stop(client, plan)
        return 0
    except Exception as exc:  # noqa: BLE001 - surface read-only API failures to CLI
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
