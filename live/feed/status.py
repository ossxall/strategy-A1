"""Endpoint JSON de estado. Las peticiones solo leen la instantánea en memoria."""

import json
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .chart import CHART_CANDLE_LIMIT, CHART_ROUTE, ChartUnavailable, render_ohlcv_chart
from .config import (
    DB_PATH,
    GAP_SCAN_MINUTES,
    INTERVAL,
    INTERVAL_MS,
    MAX_BACKOFF_SECONDS,
    REST_PAGE_LIMIT,
    REST_REPAIR_INTERVAL,
    REST_TIMEOUT_CONNECT,
    REST_TIMEOUT_READ,
    START_DATE,
    STATUS_DB_PROBE_INTERVAL,
    STATUS_HOST,
    STATUS_MAX_CLIENTS,
    STATUS_PORT,
    STATUS_REQUEST_TIMEOUT,
    SYMBOL,
    WS_MAX_RECOVERY_SECONDS,
    WS_PING_INTERVAL,
    WS_PING_TIMEOUT,
    WS_WATCHDOG_SECONDS,
    logger,
    utc_iso,
)
from .state import STATE

HEALTH_ROUTE = "/live/feed/health"
STATUS_ROUTE = "/live/feed/status"
LEGACY_ROUTES = ("/health", "/status")
STATUS_ROUTES = (HEALTH_ROUTE, STATUS_ROUTE, CHART_ROUTE, *LEGACY_ROUTES)


def build_status_payload():
    """Construye una respuesta desde cachés; no hace I/O ni espera a DB_LOCK."""
    now_monotonic = time.monotonic()
    now_ms = int(time.time() * 1000)
    with STATE.status_lock:
        runtime = STATE.runtime_state.copy()
        backfill = STATE.backfill_diagnostics.copy()
        repair = STATE.repair_diagnostics.copy()
        database = STATE.database_diagnostics.copy()

    def age_seconds(monotonic_time):
        if monotonic_time is None or not monotonic_time:
            return None
        return round(max(0.0, now_monotonic - monotonic_time), 3)

    backfill_running_age = age_seconds(backfill.pop("_running_since_monotonic", None))
    repair_running_age = age_seconds(repair.pop("_running_since_monotonic", None))
    backfill["running_for_seconds"] = (
        backfill_running_age if backfill["status"] == "running" else None
    )
    repair["running_for_seconds"] = (
        repair_running_age if repair["status"] == "running" else None
    )
    websocket_message_age = age_seconds(STATE.last_ws_message_monotonic)
    database_check_age = age_seconds(database.pop("_checked_monotonic", None))
    latest_candle = database.get("latest_candle")
    candle_age = None
    if latest_candle is not None:
        candle_age = round(max(0, now_ms - latest_candle["open_time_ms"]) / 1000, 3)
    candle_max_age = max(300, 5 * INTERVAL_MS / 1000)
    websocket_max_age = WS_WATCHDOG_SECONDS
    database_max_age = max(30, 3 * STATUS_DB_PROBE_INTERVAL)
    backfill_thread_alive = bool(
        STATE.threads["backfill"] and STATE.threads["backfill"].is_alive()
    )
    repair_thread_alive = bool(
        STATE.threads["repair"] and STATE.threads["repair"].is_alive()
    )
    database_thread_alive = bool(
        STATE.threads["database_diagnostics"]
        and STATE.threads["database_diagnostics"].is_alive()
    )
    watchdog_thread_alive = bool(
        STATE.threads["watchdog"] and STATE.threads["watchdog"].is_alive()
    )
    status_server_alive = bool(
        STATE.threads["status_server"] and STATE.threads["status_server"].is_alive()
    )
    backfill_status = runtime.get("backfill_status", "pending")
    backfill_worker_ok = (
        backfill_thread_alive
        if backfill_status == "running"
        else backfill_status in ("completed", "pending")
    )
    websocket_connected = STATE.ws_connected.is_set()
    websocket_status = runtime.get("websocket_status", "starting")
    websocket_message_fresh = (
        websocket_message_age is not None and websocket_message_age <= websocket_max_age
    )
    database_probe_fresh = (
        database_check_age is not None and database_check_age <= database_max_age
    )
    database_available = bool(database.get("available"))
    latest_candle_present = latest_candle is not None
    latest_candle_fresh = candle_age is not None and candle_age <= candle_max_age
    checks = {
        "collector_running": runtime.get("collector_status") == "running",
        "websocket_connected": websocket_connected,
        "websocket_message_fresh": websocket_message_fresh,
        "database_probe_thread_alive": database_thread_alive,
        "database_probe_fresh": database_probe_fresh,
        "database_available": database_available,
        "latest_candle_present": latest_candle_present,
        "latest_candle_fresh": latest_candle_fresh,
        "backfill_worker_ok": backfill_worker_ok,
        "repair_worker_alive": repair_thread_alive,
        "repair_last_run_ok": repair.get("status") != "error",
        "watchdog_thread_alive": watchdog_thread_alive,
        "status_server_alive": status_server_alive,
    }
    critical_failure = (
        not checks["collector_running"]
        or not checks["database_probe_thread_alive"]
        or (not checks["database_probe_fresh"])
        or (not checks["database_available"])
        or (not checks["latest_candle_present"])
        or (not checks["latest_candle_fresh"])
        or (not checks["backfill_worker_ok"])
        or (not checks["repair_worker_alive"])
        or (not checks["watchdog_thread_alive"])
        or (not checks["status_server_alive"])
        or (backfill_status == "error")
        or (websocket_status == "failed")
    )
    if critical_failure:
        health = "unhealthy"
    elif (
        not websocket_connected
        or not websocket_message_fresh
        or websocket_status != "connected"
        or (backfill_status != "completed")
        or (not checks["repair_last_run_ok"])
    ):
        health = "degraded"
    else:
        health = "healthy"
    database["check_age_seconds"] = database_check_age
    database["latest_candle_age_seconds"] = candle_age
    database["latest_candle_max_age_seconds"] = candle_max_age
    return {
        "status": health,
        "healthy": health == "healthy",
        "checked_at": utc_iso(),
        "process": {
            "pid": os.getpid(),
            "uptime_seconds": round(now_monotonic - STATE.process_started_monotonic, 3),
        },
        "config": {
            "symbol": SYMBOL,
            "interval": INTERVAL,
            "db_path": DB_PATH,
            "start_date": START_DATE,
            "status_endpoint": {
                "host": STATUS_HOST,
                "port": STATUS_PORT,
                "routes": [HEALTH_ROUTE, STATUS_ROUTE, CHART_ROUTE],
                "chart_candle_limit": CHART_CANDLE_LIMIT,
            },
            "rest": {
                "timeout_connect_seconds": REST_TIMEOUT_CONNECT,
                "timeout_read_seconds": REST_TIMEOUT_READ,
                "page_limit": REST_PAGE_LIMIT,
                "repair_interval_seconds": REST_REPAIR_INTERVAL,
                "gap_scan_minutes": GAP_SCAN_MINUTES,
            },
            "health_thresholds": {
                "websocket_message_max_age_seconds": websocket_max_age,
                "database_probe_max_age_seconds": database_max_age,
                "latest_candle_max_age_seconds": candle_max_age,
            },
        },
        "collector": {
            "status": runtime.get("collector_status", "unknown"),
            "stop_requested": STATE.stop_event.is_set(),
        },
        "websocket": {
            "status": websocket_status,
            "connected": websocket_connected,
            "opened_at": utc_iso(
                datetime.fromtimestamp(
                    time.time() - max(0.0, now_monotonic - STATE.ws_opened_monotonic),
                    timezone.utc,
                )
            )
            if STATE.ws_opened_monotonic
            else None,
            "last_message_at": STATE.last_ws_message_utc,
            "last_message_age_seconds": websocket_message_age,
            "last_error": STATE.ws_last_error,
            "last_error_at": STATE.ws_last_error_utc,
            "watchdog_timeout_seconds": WS_WATCHDOG_SECONDS,
            "ping_interval_seconds": WS_PING_INTERVAL,
            "ping_timeout_seconds": WS_PING_TIMEOUT,
            "reconnect_attempts": STATE.ws_reconnect_attempts,
            "last_reconnect_at": STATE.ws_last_reconnect_utc,
            "last_reconnect_delay_seconds": STATE.ws_last_reconnect_delay_seconds,
            "max_backoff_seconds": MAX_BACKOFF_SECONDS,
            "max_recovery_seconds": WS_MAX_RECOVERY_SECONDS,
        },
        "backfill": backfill,
        "repair": {**repair, "worker_alive": repair_thread_alive},
        "database": database,
        "threads": {
            "backfill_alive": backfill_thread_alive,
            "repair_alive": repair_thread_alive,
            "database_diagnostics_alive": database_thread_alive,
            "websocket_watchdog_alive": watchdog_thread_alive,
            "status_server_alive": status_server_alive,
        },
        "checks": checks,
    }


class _StatusHTTPServer(ThreadingHTTPServer):
    """Concurrencia acotada para que clientes lentos no saturen el servidor."""

    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False
    request_queue_size = 32

    def __init__(self, address, handler, max_clients):
        self._client_slots = threading.BoundedSemaphore(max_clients)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self._client_slots.acquire(blocking=False):
            body = b'{"status":"busy","error":"demasiadas peticiones"}'
            try:
                request.settimeout(0.1)
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Cache-Control: no-store\r\n"
                    + f"Content-Length: {len(body)}\r\n".encode("ascii")
                    + b"Connection: close\r\n\r\n"
                    + body
                )
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._client_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._client_slots.release()

    def handle_error(self, request, client_address):
        logger.debug("Conexión HTTP de estado interrumpida desde %s", client_address)


class _StatusHandler(BaseHTTPRequestHandler):
    server_version = "feed-status/1.0"

    def setup(self):
        self.request.settimeout(STATUS_REQUEST_TIMEOUT)
        super().setup()

    def log_message(self, fmt, *args):
        logger.debug("HTTP estado: %s", fmt % args)

    def send_json(self, status_code, payload):
        body = json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == CHART_ROUTE:
            try:
                html = render_ohlcv_chart()
            except ChartUnavailable as exc:
                try:
                    self.send_json(503, {"error": str(exc)})
                except OSError:
                    pass
            except Exception:  # noqa: BLE001 - devuelve error HTTP controlado
                logger.exception("Error generando el gráfico OHLCV")
                try:
                    self.send_json(
                        500,
                        {"error": "no se pudo generar el gráfico OHLCV"},
                    )
                except OSError:
                    pass
            else:
                try:
                    self.send_html(html)
                except OSError:
                    pass
            return

        if path not in STATUS_ROUTES:
            try:
                self.send_json(
                    404,
                    {
                        "error": "ruta no encontrada",
                        "routes": [HEALTH_ROUTE, STATUS_ROUTE, CHART_ROUTE],
                        "aliases": list(LEGACY_ROUTES),
                    },
                )
            except OSError:
                pass
            return
        try:
            payload = build_status_payload()
            status_code = 200 if payload["healthy"] else 503
        except Exception:  # noqa: BLE001 - convierte fallos en JSON 500
            logger.exception("Error generando diagnóstico del feed")
            status_code = 500
            payload = {
                "status": "unhealthy",
                "healthy": False,
                "error": "no se pudo generar el diagnóstico",
            }
        try:
            self.send_json(status_code, payload)
        except OSError:
            pass


def start_status_server():
    server = _StatusHTTPServer(
        (STATUS_HOST, STATUS_PORT), _StatusHandler, STATUS_MAX_CLIENTS
    )
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.25},
        name="status-http",
        daemon=True,
    )
    thread.start()
    logger.info(
        "Diagnóstico JSON disponible en http://%s:%s%s",
        STATUS_HOST,
        STATUS_PORT,
        HEALTH_ROUTE,
    )
    logger.info(
        "Gráfico OHLCV disponible en http://%s:%s%s",
        STATUS_HOST,
        STATUS_PORT,
        CHART_ROUTE,
    )
    return (server, thread)
