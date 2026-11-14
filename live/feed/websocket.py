"""Conexión Binance WebSocket, watchdog y reconexión automática."""

import random
import threading
import time
from datetime import datetime, timezone

import websocket

from .candles import normalize_candle
from .config import (
    INTERVAL,
    MAX_BACKOFF_SECONDS,
    SYMBOL,
    WS_BASE_URL,
    WS_MAX_RECOVERY_SECONDS,
    WS_PING_INTERVAL,
    WS_PING_TIMEOUT,
    WS_STABLE_SECONDS,
    WS_WATCHDOG_SECONDS,
    logger,
    utc_iso,
)
from .database import save_candle, set_state
from .state import STATE


def ws_url():
    stream = f"{SYMBOL.lower()}@kline_{INTERVAL}"
    return f"{WS_BASE_URL}/{stream}"


def on_message(ws, message):
    STATE.last_ws_message_monotonic = time.monotonic()
    STATE.last_ws_message_utc = utc_iso()
    try:
        import json

        payload = json.loads(message)
        data = payload.get("data", payload)
        kline = data.get("k")
        if not kline:
            return
        candle = normalize_candle(
            open_time=kline["t"],
            close_time=kline["T"],
            open_price=kline["o"],
            high=kline["h"],
            low=kline["l"],
            close_price=kline["c"],
            volume=kline["v"],
            quote_volume=kline.get("q", 0),
            trades=kline.get("n", 0),
            taker_buy_base=kline.get("V", 0),
            taker_buy_quote=kline.get("Q", 0),
            is_closed=kline.get("x", False),
            source="websocket",
        )
        save_candle(candle)
        if candle["is_closed"]:
            logger.info(
                "Vela cerrada %s %s close=%s",
                SYMBOL,
                datetime.fromtimestamp(
                    candle["open_time"] / 1000, timezone.utc
                ).isoformat(),
                candle["close"],
            )
    except Exception:  # noqa: BLE001 - los mensajes no válidos no matan el WS
        logger.exception("No se pudo procesar mensaje WS")


def on_open(ws):
    STATE.ws_opened_monotonic = time.monotonic()
    STATE.last_ws_message_monotonic = STATE.ws_opened_monotonic
    STATE.last_ws_message_utc = utc_iso()
    STATE.ws_connected.set()
    set_state("websocket_status", "connected")
    logger.info("WebSocket conectado: %s", SYMBOL)


def on_error(ws, error):
    STATE.ws_last_error = str(error)
    STATE.ws_last_error_utc = utc_iso()
    logger.warning("Error WebSocket: %s", error)
    set_state("websocket_status", "error")


def on_close(ws, status_code, message):
    STATE.ws_connected.clear()
    if not STATE.stop_event.is_set():
        set_state("websocket_status", "disconnected")
    logger.warning("WebSocket cerrado: código=%s motivo=%s", status_code, message)


def watchdog_worker():
    """
    Cierra el WebSocket si deja de recibir mensajes.
    El bucle principal detectará el cierre y reconectará.
    """
    while not STATE.stop_event.wait(5):
        with STATE.ws_lock:
            ws = STATE.active_ws
        if ws is None or not STATE.ws_connected.is_set():
            continue
        last_message = STATE.last_ws_message_monotonic
        if not last_message:
            continue
        idle_seconds = time.monotonic() - last_message
        if idle_seconds >= WS_WATCHDOG_SECONDS:
            logger.error(
                "Watchdog: %.0fs sin mensajes; cerrando WebSocket para reconectar.",
                idle_seconds,
            )
            set_state("websocket_status", "stale")
            STATE.ws_connected.clear()
            try:
                ws.close()
            except Exception:  # noqa: BLE001 - cierre de socket best-effort
                logger.exception("No se pudo cerrar WS inactivo")


def run_websocket_forever():
    """
    Reconecta con backoff exponencial.
    Si no consigue mantener una conexión funcional durante
    el periodo de recuperación, devuelve False.
    """
    backoff = 1
    recovery_started = time.monotonic()
    STATE.threads["watchdog"] = threading.Thread(
        target=watchdog_worker, name="watchdog", daemon=True
    )
    STATE.threads["watchdog"].start()
    try:
        while not STATE.stop_event.is_set():
            ws = websocket.WebSocketApp(
                ws_url(),
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            with STATE.ws_lock:
                STATE.active_ws = ws
            connection_started = time.monotonic()
            try:
                ws.run_forever(
                    ping_interval=WS_PING_INTERVAL, ping_timeout=WS_PING_TIMEOUT
                )
            except Exception:  # noqa: BLE001 - reconexión tras fallo de librería
                logger.exception("Excepción en WebSocket")
            finally:
                STATE.ws_connected.clear()
                with STATE.ws_lock:
                    if STATE.active_ws is ws:
                        STATE.active_ws = None
                try:
                    ws.close()
                except Exception:  # noqa: BLE001 - cierre best-effort
                    logger.debug("Error secundario cerrando WebSocket", exc_info=True)
            if STATE.stop_event.is_set():
                break
            connection_duration = time.monotonic() - connection_started
            if connection_duration >= WS_STABLE_SECONDS:
                recovery_started = time.monotonic()
                backoff = 1
            failed_for = time.monotonic() - recovery_started
            if WS_MAX_RECOVERY_SECONDS > 0 and failed_for >= WS_MAX_RECOVERY_SECONDS:
                logger.critical(
                    "Sin recuperación estable durante %s s.", int(failed_for)
                )
                set_state("websocket_status", "failed")
                return False
            delay = min(backoff, MAX_BACKOFF_SECONDS)
            delay *= random.uniform(0.8, 1.2)
            STATE.ws_reconnect_attempts += 1
            STATE.ws_last_reconnect_utc = utc_iso()
            STATE.ws_last_reconnect_delay_seconds = round(delay, 3)
            logger.warning(
                "Reconexión en %.1f s; tiempo de recuperación %.0f s", delay, failed_for
            )
            set_state("websocket_status", "reconnecting")
            if STATE.stop_event.wait(delay):
                break
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
    finally:
        STATE.stop_event.set()
        with STATE.ws_lock:
            ws = STATE.active_ws
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001 - cierre best-effort
                logger.debug("Error cerrando WebSocket al parar", exc_info=True)
        STATE.threads["watchdog"].join(timeout=5)
    return True
