"""Adaptador REST oficial de Binance USDⓈ-M Futures."""

from __future__ import annotations

from typing import Any


class BinanceOrderNotFound(LookupError):
    """El clientOrderId consultado no existe en Binance."""


def _is_order_not_found(exc: Exception) -> bool:
    if (
        exc.__class__.__name__ == "NotFoundError"
        or getattr(exc, "code", None) in {-2013, -2011}
        or getattr(exc, "error_code", None) in {-2013, -2011}
    ):
        return True

    args = exc.args
    if len(args) == 1 and isinstance(args[0], tuple):
        args = args[0]
    if len(args) >= 2:
        try:
            code = int(args[0])
        except (TypeError, ValueError):
            code = None
        message = str(args[1]).lower()
        if (
            (code == -2013 and "order does not exist" in message)
            or (code == -2011 and "unknown order" in message)
        ):
            return True
    return False


def response_data(response: Any) -> Any:
    """Extrae datos del wrapper del SDK y normaliza modelos Pydantic."""
    data = response.data() if callable(getattr(response, "data", None)) else response
    to_dict = getattr(data, "to_dict", None)
    if callable(to_dict):
        # Los modelos oneOf del SDK necesitan to_dict() para desempaquetar
        # actual_instance; model_dump() devuelve el envoltorio interno.
        return to_dict()
    if hasattr(data, "model_dump"):
        return data.model_dump(by_alias=True, mode="json")
    if hasattr(data, "dict") and callable(data.dict):
        return data.dict(by_alias=True)
    return data


class BinanceUsdmClient:
    """Thin adapter: todo acceso a USDⓈ-M Futures pasa por el SDK oficial."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        environment: str,
        *,
        client: Any | None = None,
    ):
        self.environment = environment.lower()
        if self.environment not in {"testnet", "production"}:
            raise ValueError("BINANCE_ENVIRONMENT debe ser testnet o production")
        if client is not None:
            self.client = client
            return
        if not api_key or not api_secret:
            raise ValueError("Faltan BINANCE_API_KEY/BINANCE_API_SECRET")

        try:
            from binance_common.configuration import ConfigurationRestAPI
            from binance_common.constants import (
                DERIVATIVES_TRADING_USDS_FUTURES_REST_API_PROD_URL,
                DERIVATIVES_TRADING_USDS_FUTURES_REST_API_TESTNET_URL,
            )
            from binance_sdk_derivatives_trading_usds_futures.derivatives_trading_usds_futures import (
                DerivativesTradingUsdsFutures,
            )
        except ImportError as exc:
            raise RuntimeError(
                "Instala binance-sdk-derivatives-trading-usds-futures"
            ) from exc

        base_path = (
            DERIVATIVES_TRADING_USDS_FUTURES_REST_API_TESTNET_URL
            if self.environment == "testnet"
            else DERIVATIVES_TRADING_USDS_FUTURES_REST_API_PROD_URL
        )
        configuration = ConfigurationRestAPI(
            api_key=api_key,
            api_secret=api_secret,
            base_path=base_path,
            timeout=5_000,
            retries=0,
        )
        self.client = DerivativesTradingUsdsFutures(config_rest_api=configuration)

    @property
    def rest(self):
        return self.client.rest_api

    def exchange_information(self) -> Any:
        return response_data(self.rest.exchange_information())

    def mark_price(self, symbol: str) -> Any:
        return response_data(self.rest.mark_price(symbol=symbol))

    def account_balance(self) -> Any:
        return response_data(self.rest.futures_account_balance_v3())

    def account_information(self) -> Any:
        return response_data(self.rest.account_information_v3())

    def positions(self, symbol: str | None = None) -> Any:
        return response_data(self.rest.position_information_v3(symbol=symbol))

    def open_orders(self, symbol: str) -> Any:
        return response_data(self.rest.current_all_open_orders(symbol=symbol))

    def symbol_configuration(self, symbol: str) -> Any:
        return response_data(self.rest.symbol_configuration(symbol=symbol))

    def position_mode(self) -> Any:
        return response_data(self.rest.get_current_position_mode())

    def change_leverage(self, symbol: str, leverage: int) -> Any:
        return response_data(
            self.rest.change_initial_leverage(symbol=symbol, leverage=leverage)
        )

    def place_market_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: str,
        client_order_id: str,
        reduce_only: bool,
    ) -> Any:
        return response_data(
            self.rest.new_order(
                symbol=symbol,
                side=side,
                type="MARKET",
                quantity=float(quantity),
                new_client_order_id=client_order_id,
                new_order_resp_type="RESULT",
                reduce_only="true" if reduce_only else None,
            )
        )

    def query_order(
        self,
        symbol: str,
        client_order_id: str | None = None,
        order_id: int | None = None,
    ) -> Any:
        try:
            response = self.rest.query_order(
                symbol=symbol,
                orig_client_order_id=client_order_id,
                order_id=order_id,
            )
        except Exception as exc:
            if _is_order_not_found(exc):
                raise BinanceOrderNotFound(
                    f"Orden Binance no encontrada: {client_order_id or order_id}"
                ) from exc
            raise
        return response_data(response)

    def place_protective_stop(
        self,
        *,
        symbol: str,
        side: str,
        trigger_price: str,
        client_algo_id: str,
    ) -> Any:
        return response_data(
            self.rest.new_algo_order(
                algo_type="CONDITIONAL",
                symbol=symbol,
                side=side,
                type="STOP_MARKET",
                trigger_price=float(trigger_price),
                working_type="MARK_PRICE",
                close_position="true",
                client_algo_id=client_algo_id,
            )
        )

    def cancel_protective_stop(self, client_algo_id: str) -> Any:
        try:
            response = self.rest.cancel_algo_order(
                client_algo_id=client_algo_id
            )
        except Exception as exc:
            if _is_order_not_found(exc):
                raise BinanceOrderNotFound(
                    f"Stop Binance ya no está abierto: {client_algo_id}"
                ) from exc
            raise
        return response_data(response)

    def query_protective_stop(self, client_algo_id: str) -> Any:
        try:
            response = self.rest.query_algo_order(
                client_algo_id=client_algo_id
            )
        except Exception as exc:
            if _is_order_not_found(exc):
                raise BinanceOrderNotFound(
                    f"Stop Binance no encontrado: {client_algo_id}"
                ) from exc
            raise
        return response_data(response)

    def open_algo_orders(self, symbol: str) -> Any:
        return response_data(self.rest.current_all_algo_open_orders(symbol=symbol))
