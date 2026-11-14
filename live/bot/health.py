"""Endpoint HTTP de salud del bot, atendido desde instantáneas en memoria."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .config import logger

HEALTH_ROUTE = "/live/bot/health"
STATUS_ROUTE = "/live/bot/status"
LEGACY_ROUTES = ("/health", "/status")
HEALTH_ROUTES = (HEALTH_ROUTE, STATUS_ROUTE, *LEGACY_ROUTES)
_INTERVAL_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86_400, "w": 604_800}


def _utc_iso(moment: datetime | None = None) -> str:
    return (moment or datetime.now(timezone.utc)).isoformat()


def _interval_seconds(interval: str) -> int:
    value = (interval or "").strip().lower()
    try:
        unit = value[-1]
        amount = int(value[:-1])
    except (ValueError, IndexError):
        return 60
    factor = _INTERVAL_SECONDS.get(unit)
    return amount * factor if factor and amount > 0 else 60


def probe_signal_database(path: Path, timeout_seconds: float) -> dict:
    """Sondea signal.db con timeout acotado; nunca se invoca desde HTTP."""
    started = time.monotonic()
    connection = None
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(
            uri, uri=True, timeout=max(0.01, timeout_seconds)
        )
        connection.execute(
            f"PRAGMA busy_timeout={max(1, int(timeout_seconds * 1000))}"
        )
        deadline = started + max(0.01, timeout_seconds)
        connection.set_progress_handler(
            lambda: int(time.monotonic() >= deadline), 1000
        )
        row = connection.execute(
            "SELECT signal_time FROM signals ORDER BY signal_time DESC LIMIT 1"
        ).fetchone()
        return {
            "available": True,
            "checked_at": _utc_iso(),
            "check_duration_seconds": round(time.monotonic() - started, 4),
            "last_signal_time_ms": int(row[0]) if row and row[0] is not None else None,
            "last_error": None,
        }
    except (OSError, sqlite3.Error, ValueError, OverflowError) as exc:
        return {
            "available": False,
            "checked_at": _utc_iso(),
            "check_duration_seconds": round(time.monotonic() - started, 4),
            "last_signal_time_ms": None,
            "last_error": str(exc),
        }
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass


class BotHealth:
    """Mantiene el diagnóstico actualizado por el ciclo del bot, no por HTTP."""

    def __init__(
        self,
        *,
        symbol: str,
        interval: str,
        timeframe: str,
        feed_path: Path,
        signal_path: Path,
        poll_seconds: float,
        min_bars: int,
        live_only: bool,
        live_since_ms: int | None,
        probe_interval_seconds: float,
    ):
        self.symbol = symbol
        self.interval = interval
        self.timeframe = timeframe
        self.feed_path = str(feed_path)
        self.signal_path = str(signal_path)
        self.poll_seconds = poll_seconds
        self.min_bars = min_bars
        self.live_only = live_only
        self.live_since_ms = live_since_ms
        self.probe_interval_seconds = probe_interval_seconds
        self.started_monotonic = time.monotonic()
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._http_server_thread: threading.Thread | None = None
        self._database_probe_thread: threading.Thread | None = None
        self._state = {
            "loop_status": "starting",
            "last_heartbeat_monotonic": None,
            "last_cycle_status": "pending",
            "cycle_in_progress": False,
            "last_cycle_started_at": None,
            "last_cycle_completed_at": None,
            "last_cycle_started_monotonic": None,
            "last_cycle_duration_seconds": None,
            "last_cycle_error": None,
            "last_cycle_error_at": None,
            "last_events_inserted": 0,
            "feed_rows": 0,
            "feed_latest_candle_ms": None,
            "strategy_ready": False,
            "signal_database": {
                "available": False,
                "checked_at": None,
                "checked_monotonic": None,
                "check_duration_seconds": None,
                "last_signal_time_ms": None,
                "last_error": "sondeo pendiente",
            },
        }

    def _update(self, **values) -> None:
        with self._lock:
            self._state.update(values)

    def attach_threads(
        self,
        http_server_thread: threading.Thread,
        database_probe_thread: threading.Thread,
    ) -> None:
        self._http_server_thread = http_server_thread
        self._database_probe_thread = database_probe_thread

    def mark_running(self) -> None:
        self._update(
            loop_status="running",
            last_heartbeat_monotonic=time.monotonic(),
        )

    def begin_cycle(self) -> None:
        now = time.monotonic()
        self._update(
            cycle_in_progress=True,
            last_cycle_started_at=_utc_iso(),
            last_cycle_started_monotonic=now,
            last_heartbeat_monotonic=now,
        )

    def record_feed(self, raw) -> None:
        latest_candle_ms = None
        if not raw.empty:
            try:
                latest_candle_ms = int(raw.index[-1].timestamp() * 1000)
            except (AttributeError, TypeError, ValueError, OverflowError):
                pass
        self._update(
            feed_rows=len(raw),
            feed_latest_candle_ms=latest_candle_ms,
        )

    def record_strategy_ready(self, ready: bool) -> None:
        self._update(strategy_ready=bool(ready))

    def finish_cycle(
        self,
        *,
        status: str,
        duration_seconds: float,
        events_inserted: int = 0,
        error: str | None = None,
    ) -> None:
        now = time.monotonic()
        self._update(
            last_cycle_status=status,
            cycle_in_progress=False,
            last_cycle_completed_at=_utc_iso(),
            last_cycle_duration_seconds=round(duration_seconds, 3),
            last_cycle_error=error,
            last_cycle_error_at=_utc_iso() if error else None,
            last_events_inserted=events_inserted,
            last_heartbeat_monotonic=now,
        )

    def record_database_probe(self, result: dict) -> None:
        self._update(
            signal_database={
                **result,
                "checked_monotonic": time.monotonic(),
            }
        )

    def mark_stopped(self) -> None:
        self._update(
            loop_status="stopped",
            last_heartbeat_monotonic=time.monotonic(),
        )

    def stop(self) -> None:
        self.stop_event.set()

    def build_payload(self, host: str, port: int, request_timeout: float) -> dict:
        # No se espera a un escritor que, por error, retenga el lock demasiado.
        if not self._lock.acquire(timeout=0.02):
            raise TimeoutError("instantánea de salud ocupada")
        try:
            state = self._state.copy()
            state["signal_database"] = self._state["signal_database"].copy()
        finally:
            self._lock.release()

        now_monotonic = time.monotonic()
        now_ms = int(time.time() * 1000)

        def age_seconds(monotonic_time):
            if monotonic_time is None:
                return None
            return round(max(0.0, now_monotonic - monotonic_time), 3)

        heartbeat_age = age_seconds(state.pop("last_heartbeat_monotonic"))
        cycle_started_monotonic = state.pop("last_cycle_started_monotonic")
        database = state.pop("signal_database")
        database_age = age_seconds(database.pop("checked_monotonic", None))
        latest_candle_ms = state.pop("feed_latest_candle_ms")
        latest_candle_age = (
            round(max(0, now_ms - latest_candle_ms) / 1000, 3)
            if latest_candle_ms is not None
            else None
        )
        cycle_running_age = (
            age_seconds(cycle_started_monotonic)
            if state["cycle_in_progress"]
            else None
        )

        candle_max_age = max(300, 5 * _interval_seconds(self.interval))
        heartbeat_max_age = max(180.0, 3 * self.poll_seconds)
        database_max_age = max(30.0, 3 * self.probe_interval_seconds)
        http_server_alive = bool(
            self._http_server_thread and self._http_server_thread.is_alive()
        )
        database_probe_alive = bool(
            self._database_probe_thread and self._database_probe_thread.is_alive()
        )
        checks = {
            "bot_loop_running": state["loop_status"] == "running",
            "bot_heartbeat_fresh": (
                heartbeat_age is not None and heartbeat_age <= heartbeat_max_age
            ),
            "last_cycle_ok": state["last_cycle_status"] == "ok",
            "feed_data_available": state["feed_rows"] > 0,
            "feed_candle_fresh": (
                latest_candle_age is not None
                and latest_candle_age <= candle_max_age
            ),
            "strategy_ready": state["strategy_ready"],
            "signal_database_available": bool(database["available"]),
            "signal_database_probe_fresh": (
                database_age is not None and database_age <= database_max_age
            ),
            "database_probe_thread_alive": database_probe_alive,
            "http_server_alive": http_server_alive,
        }
        if (
            not checks["bot_loop_running"]
            or not checks["bot_heartbeat_fresh"]
            or not checks["signal_database_available"]
            or not checks["signal_database_probe_fresh"]
            or not checks["database_probe_thread_alive"]
            or not checks["http_server_alive"]
        ):
            health_status = "unhealthy"
        elif not all(
            checks[key]
            for key in (
                "last_cycle_ok",
                "feed_data_available",
                "feed_candle_fresh",
                "strategy_ready",
            )
        ):
            health_status = "degraded"
        else:
            health_status = "healthy"

        database["check_age_seconds"] = database_age
        database["check_max_age_seconds"] = database_max_age
        database["last_signal_time"] = (
            datetime.fromtimestamp(
                database["last_signal_time_ms"] / 1000, timezone.utc
            ).isoformat()
            if database["last_signal_time_ms"] is not None
            else None
        )
        state["heartbeat_age_seconds"] = heartbeat_age
        state["heartbeat_max_age_seconds"] = heartbeat_max_age
        state["cycle_running_for_seconds"] = cycle_running_age
        state["feed_latest_candle"] = (
            datetime.fromtimestamp(
                latest_candle_ms / 1000, timezone.utc
            ).isoformat()
            if latest_candle_ms is not None
            else None
        )
        state["feed_latest_candle_age_seconds"] = latest_candle_age
        state["feed_latest_candle_max_age_seconds"] = candle_max_age

        return {
            "status": health_status,
            "healthy": health_status == "healthy",
            "checked_at": _utc_iso(),
            "process": {
                "pid": os.getpid(),
                "uptime_seconds": round(now_monotonic - self.started_monotonic, 3),
            },
            "config": {
                "symbol": self.symbol,
                "interval": self.interval,
                "strategy_timeframe": self.timeframe,
                "feed_db_path": self.feed_path,
                "signal_db_path": self.signal_path,
                "poll_seconds": self.poll_seconds,
                "min_bars": self.min_bars,
                "live_only": self.live_only,
                "live_since_ms": self.live_since_ms,
                "status_endpoint": {
                    "host": host,
                    "port": port,
                    "routes": [HEALTH_ROUTE, STATUS_ROUTE],
                    "aliases": list(LEGACY_ROUTES),
                },
                "http_request_timeout_seconds": request_timeout,
            },
            "bot": state,
            "feed": {
                "rows": state["feed_rows"],
                "latest_candle": state["feed_latest_candle"],
                "latest_candle_age_seconds": state[
                    "feed_latest_candle_age_seconds"
                ],
                "latest_candle_max_age_seconds": state[
                    "feed_latest_candle_max_age_seconds"
                ],
            },
            "signal_database": database,
            "threads": {
                "http_server_alive": http_server_alive,
                "database_probe_alive": database_probe_alive,
            },
            "checks": checks,
        }


class _BotHealthHTTPServer(ThreadingHTTPServer):
    """Concurrencia acotada; clientes lentos no bloquean el ciclo del bot."""

    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False
    request_queue_size = 32

    def __init__(self, address, handler, max_clients: int):
        self._client_slots = threading.BoundedSemaphore(max_clients)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self._client_slots.acquire(blocking=False):
            body = b'{"status":"busy","healthy":false,"error":"demasiadas peticiones"}'
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
        logger.debug("Conexión HTTP de salud interrumpida desde %s", client_address)


def _make_handler(
    health: BotHealth,
    host: str,
    configured_port: int,
    request_timeout: float,
):
    class _BotHealthHandler(BaseHTTPRequestHandler):
        server_version = "bot-health/1.0"

        def setup(self):
            self.request.settimeout(request_timeout)
            super().setup()

        def log_message(self, fmt, *args):
            logger.debug("HTTP salud bot: %s", fmt % args)

        def send_json(self, status_code: int, payload: dict):
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

        def do_GET(self):
            path = urlparse(self.path).path
            if path not in HEALTH_ROUTES:
                try:
                    self.send_json(
                        404,
                        {
                            "error": "ruta no encontrada",
                            "routes": [HEALTH_ROUTE, STATUS_ROUTE],
                        },
                    )
                except OSError:
                    pass
                return

            actual_host, actual_port = self.server.server_address[:2]
            try:
                payload = health.build_payload(
                    str(actual_host or host),
                    int(actual_port or configured_port),
                    request_timeout,
                )
                status_code = 200 if payload["healthy"] else 503
            except TimeoutError:
                status_code = 503
                payload = {
                    "status": "degraded",
                    "healthy": False,
                    "error": "instantánea de salud ocupada",
                }
            except Exception:  # noqa: BLE001 - fallo controlado en JSON
                logger.exception("Error generando diagnóstico del bot")
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

    return _BotHealthHandler


def start_health_server(
    health: BotHealth,
    host: str,
    port: int,
    signal_path: Path,
    *,
    max_clients: int = 8,
    request_timeout: float = 1.0,
    probe_interval: float = 5.0,
    probe_timeout: float = 0.2,
):
    handler = _make_handler(health, host, port, request_timeout)
    server = _BotHealthHTTPServer(
        (host, port), handler, max_clients=max(1, max_clients)
    )
    http_thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.25},
        name="bot-health-http",
        daemon=True,
    )

    def probe_worker():
        while not health.stop_event.is_set():
            health.record_database_probe(
                probe_signal_database(signal_path, probe_timeout)
            )
            health.stop_event.wait(max(0.1, probe_interval))

    probe_thread = threading.Thread(
        target=probe_worker,
        name="bot-health-db-probe",
        daemon=True,
    )
    health.attach_threads(http_thread, probe_thread)
    http_thread.start()
    probe_thread.start()
    logger.info(
        "Salud JSON del bot disponible en http://%s:%s%s (alias %s)",
        host,
        server.server_address[1],
        HEALTH_ROUTE,
        STATUS_ROUTE,
    )
    return server, http_thread, probe_thread
