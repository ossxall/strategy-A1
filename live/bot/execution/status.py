"""Endpoint HTTP de salud del executor, basado en una instantánea en memoria."""

from __future__ import annotations

import copy
import json
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from ..config import logger

STATUS_ROUTE = "/live/bot/execution/status"
HEALTH_ROUTE = "/live/bot/execution/health"
STATUS_ROUTES = (STATUS_ROUTE, HEALTH_ROUTE)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExecutionHealth:
    """Mantiene datos de ejecución sin realizar I/O desde las peticiones HTTP."""

    def __init__(self, settings):
        self.settings = settings
        self.started_monotonic = time.monotonic()
        self._lock = threading.Lock()
        self._http_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self._state = {
            "loop_status": "starting",
            "last_heartbeat_monotonic": None,
            "cycle_in_progress": False,
            "cycle_started_at": None,
            "cycle_started_monotonic": None,
            "cycle_count": 0,
            "successful_cycle_count": 0,
            "failed_cycle_count": 0,
            "last_cycle_status": "pending",
            "last_cycle_started_at": None,
            "last_cycle_completed_at": None,
            "last_cycle_duration_seconds": None,
            "last_cycle_error": None,
            "last_error": None,
            "last_error_at": None,
            "signals_claimed": 0,
            "last_signal": None,
            "last_execution": None,
            "last_reconciliation_error": None,
            "last_reconciliation_signal_id": None,
            "reconciliation_blocked": False,
            "worker_initialized": False,
            "account_consistency_error": None,
            "account_snapshot": {
                "available": False,
                "observed_at": None,
                "checked_monotonic": None,
                "wallet_balance": None,
                "available_balance": None,
                "positions": [],
                "open_orders_count": None,
                "open_algo_orders_count": None,
                "last_error": "sin consulta de cuenta",
            },
        }

    def _update(self, **values) -> None:
        with self._lock:
            self._state.update(values)

    def attach_threads(
        self,
        http_thread: threading.Thread,
        worker_thread: threading.Thread,
    ) -> None:
        self._http_thread = http_thread
        self._worker_thread = worker_thread

    def mark_running(self) -> None:
        now = time.monotonic()
        self._update(
            loop_status="running",
            last_heartbeat_monotonic=now,
        )

    def mark_stopped(self) -> None:
        self._update(
            loop_status="stopped",
            last_heartbeat_monotonic=time.monotonic(),
        )

    def set_worker_id(self, worker_id: str) -> None:
        self._update(worker_id=worker_id)

    def begin_cycle(self) -> None:
        now = time.monotonic()
        with self._lock:
            self._state.update(
                cycle_in_progress=True,
                cycle_started_at=_utc_iso(),
                cycle_started_monotonic=now,
                last_cycle_started_at=_utc_iso(),
                last_heartbeat_monotonic=now,
                cycle_count=self._state["cycle_count"] + 1,
            )

    def finish_cycle(self, *, status: str, error: str | None = None) -> None:
        now = time.monotonic()
        with self._lock:
            started = self._state["cycle_started_monotonic"]
            duration = max(0.0, now - started) if started is not None else 0.0
            self._state.update(
                cycle_in_progress=False,
                last_cycle_status=status,
                last_cycle_completed_at=_utc_iso(),
                last_cycle_duration_seconds=round(duration, 3),
                last_cycle_error=error,
                last_heartbeat_monotonic=now,
            )
            if status == "ok":
                self._state["successful_cycle_count"] += 1
            else:
                self._state["failed_cycle_count"] += 1
                self._state["last_error"] = error
                self._state["last_error_at"] = _utc_iso()

    def record_worker_initialized(self, initialized: bool) -> None:
        self._update(worker_initialized=bool(initialized))

    def record_account_snapshot(
        self,
        snapshot: dict | None = None,
        *,
        error: str | None = None,
    ) -> None:
        now = time.monotonic()
        if error is not None:
            value = {
                "available": False,
                "observed_at": _utc_iso(),
                "checked_monotonic": now,
                "wallet_balance": None,
                "available_balance": None,
                "positions": [],
                "open_orders_count": None,
                "open_algo_orders_count": None,
                "last_error": error,
            }
        else:
            snapshot = snapshot or {}
            value = {
                "available": True,
                "observed_at": _utc_iso(),
                "checked_monotonic": now,
                "wallet_balance": snapshot.get("wallet_balance"),
                "available_balance": snapshot.get("available_balance"),
                "positions": copy.deepcopy(snapshot.get("positions", [])),
                "open_orders_count": len(snapshot.get("open_orders", [])),
                "open_algo_orders_count": len(
                    snapshot.get("open_algo_orders", [])
                ),
                "last_error": None,
            }
        self._update(account_snapshot=value)

    def record_account_consistency(self, error: str | None) -> None:
        self._update(account_consistency_error=error)

    def record_signal(self, signal: dict) -> None:
        value = {
            key: signal.get(key)
            for key in ("signal_id", "event", "action", "side", "symbol", "reason")
        }
        with self._lock:
            self._state["signals_claimed"] += 1
            self._state["last_signal"] = value

    def record_execution(
        self,
        signal: dict,
        status: str,
        *,
        order_status: str | None = None,
        error: str | None = None,
    ) -> None:
        value = {
            "signal_id": signal.get("signal_id"),
            "event": signal.get("event"),
            "action": signal.get("action"),
            "status": status,
            "order_status": order_status,
            "updated_at": _utc_iso(),
            "error": error,
        }
        with self._lock:
            previous_reconciliation_signal = self._state[
                "last_reconciliation_signal_id"
            ]
            self._state["last_execution"] = value
            if status == "RECONCILIATION_REQUIRED":
                self._state["last_reconciliation_error"] = error or (
                    "La ejecución requiere reconciliación manual"
                )
                self._state["last_reconciliation_signal_id"] = signal.get(
                    "signal_id"
                )
            elif (
                self._state["last_reconciliation_error"] is not None
                and previous_reconciliation_signal
                == signal.get("signal_id")
            ):
                self._state["last_reconciliation_error"] = None
                self._state["last_reconciliation_signal_id"] = None

    def record_reconciliation_block(self, blocked: bool) -> None:
        with self._lock:
            self._state["reconciliation_blocked"] = bool(blocked)
            if not blocked:
                self._state["last_reconciliation_error"] = None
                self._state["last_reconciliation_signal_id"] = None

    def build_payload(
        self,
        host: str,
        port: int,
        request_timeout: float,
    ) -> dict:
        # No se espera al ciclo del worker ni a I/O; el lock solo protege
        # una copia pequeña y tiene un límite estricto de espera.
        if not self._lock.acquire(timeout=0.02):
            raise TimeoutError("instantánea de salud del executor ocupada")
        try:
            state = copy.deepcopy(self._state)
        finally:
            self._lock.release()

        now_monotonic = time.monotonic()

        def age_seconds(value):
            if value is None:
                return None
            return round(max(0.0, now_monotonic - value), 3)

        heartbeat_age = age_seconds(state.pop("last_heartbeat_monotonic"))
        cycle_started_monotonic = state.pop("cycle_started_monotonic")
        account = state["account_snapshot"]
        account_age = age_seconds(account.pop("checked_monotonic"))
        state.pop("last_reconciliation_signal_id", None)
        cycle_running_age = (
            age_seconds(cycle_started_monotonic)
            if state["cycle_in_progress"]
            else None
        )

        http_alive = bool(self._http_thread and self._http_thread.is_alive())
        worker_alive = bool(self._worker_thread and self._worker_thread.is_alive())
        heartbeat_max_age = max(60.0, 3 * self.settings.poll_seconds)
        account_max_age = max(
            60.0, 3 * self.settings.account_poll_seconds
        )
        cycle_max_age = max(300.0, 10 * self.settings.account_poll_seconds)
        enabled = bool(self.settings.enabled)

        checks = {
            "executor_loop_running": (
                state["loop_status"] == "running" and worker_alive
            ),
            "executor_heartbeat_fresh": (
                heartbeat_age is not None and heartbeat_age <= heartbeat_max_age
            ),
            "http_server_alive": http_alive,
            "cycle_not_stuck": (
                not state["cycle_in_progress"]
                or (
                    cycle_running_age is not None
                    and cycle_running_age <= cycle_max_age
                )
            ),
            "last_cycle_ok": state["last_cycle_status"] == "ok",
            "account_snapshot_available": bool(account["available"]),
            "account_snapshot_fresh": (
                account_age is not None and account_age <= account_max_age
            ),
            "account_consistent": state["account_consistency_error"] is None,
            "no_reconciliation_required": (
                state["last_reconciliation_error"] is None
            ),
            "no_inflight_reconciliation_block": (
                not state["reconciliation_blocked"]
            ),
        }
        critical_failure = (
            not checks["executor_loop_running"]
            or not checks["executor_heartbeat_fresh"]
            or not checks["http_server_alive"]
            or not checks["cycle_not_stuck"]
            or (
                enabled
                and state["last_cycle_status"] == "error"
            )
            or (
                enabled
                and state["cycle_count"] > 0
                and (
                    not checks["account_snapshot_available"]
                    or not checks["account_consistent"]
                    or not checks["no_reconciliation_required"]
                    or not checks["no_inflight_reconciliation_block"]
                )
            )
        )
        degraded = (
            state["last_cycle_status"] == "pending"
            or (
                enabled
                and (
                    not state["worker_initialized"]
                    or not checks["account_snapshot_available"]
                    or not checks["account_snapshot_fresh"]
                    or not checks["account_consistent"]
                    or not checks["no_reconciliation_required"]
                    or not checks["no_inflight_reconciliation_block"]
                )
            )
        )
        health_status = (
            "unhealthy" if critical_failure
            else "degraded" if degraded
            else "healthy"
        )

        account["check_age_seconds"] = account_age
        account["check_max_age_seconds"] = account_max_age
        state["heartbeat_age_seconds"] = heartbeat_age
        state["heartbeat_max_age_seconds"] = heartbeat_max_age
        state["cycle_running_for_seconds"] = cycle_running_age
        state["cycle_max_duration_seconds"] = cycle_max_age
        state["account_snapshot"] = account

        return {
            "status": health_status,
            "healthy": health_status == "healthy",
            "checked_at": _utc_iso(),
            "process": {
                "pid": os.getpid(),
                "uptime_seconds": round(
                    now_monotonic - self.started_monotonic, 3
                ),
                "worker_id": state.get("worker_id"),
            },
            "config": {
                "enabled": enabled,
                "test_mode": self.settings.test_mode,
                "environment": self.settings.environment,
                "allow_live_trading": self.settings.allow_live_trading,
                "account_id": self.settings.account_id,
                "symbol": self.settings.symbol,
                "poll_seconds": self.settings.poll_seconds,
                "account_poll_seconds": self.settings.account_poll_seconds,
                "lease_seconds": self.settings.lease_seconds,
                "max_entry_age_seconds": self.settings.max_entry_age_seconds,
                "initial_backfill_seconds": (
                    self.settings.initial_backfill_seconds
                ),
                "max_leverage": self.settings.max_leverage,
                "set_leverage": self.settings.set_leverage,
                "max_equity_fraction": self.settings.max_equity_fraction,
                "max_risk_pct": self.settings.max_risk_pct,
                "signal_db_path": str(self.settings.signal_db),
                "heartbeat_path": (
                    str(self.settings.heartbeat_path)
                    if self.settings.heartbeat_path is not None
                    else None
                ),
                "status_endpoint": {
                    "host": host,
                    "port": port,
                    "routes": [STATUS_ROUTE],
                    "aliases": [HEALTH_ROUTE],
                },
                "http_request_timeout_seconds": request_timeout,
            },
            "executor": state,
            "account": account,
            "threads": {
                "worker_alive": worker_alive,
                "http_server_alive": http_alive,
            },
            "checks": checks,
        }


class _ExecutionStatusHTTPServer(ThreadingHTTPServer):
    """Concurrencia limitada para no atascar el executor por clientes HTTP."""

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
        logger.debug(
            "Conexión HTTP de salud del executor interrumpida desde %s",
            client_address,
        )


def start_execution_status_server(
    health: ExecutionHealth,
    host: str,
    port: int,
    *,
    max_clients: int = 8,
    request_timeout: float = 1.0,
    worker_thread: threading.Thread | None = None,
):
    class _ExecutionStatusHandler(BaseHTTPRequestHandler):
        server_version = "executor-status/1.0"

        def setup(self):
            self.request.settimeout(request_timeout)
            super().setup()

        def log_message(self, fmt, *args):
            logger.debug("HTTP salud executor: %s", fmt % args)

        def send_json(self, status_code: int, payload: dict):
            body = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
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
            if path not in STATUS_ROUTES:
                try:
                    self.send_json(
                        404,
                        {
                            "error": "ruta no encontrada",
                            "routes": [STATUS_ROUTE],
                            "aliases": [HEALTH_ROUTE],
                        },
                    )
                except OSError:
                    pass
                return

            actual_host, actual_port = self.server.server_address[:2]
            try:
                payload = health.build_payload(
                    str(actual_host or host),
                    int(actual_port or port),
                    request_timeout,
                )
                status_code = 200 if payload["healthy"] else 503
            except TimeoutError:
                status_code = 503
                payload = {
                    "status": "degraded",
                    "healthy": False,
                    "error": "instantánea de salud del executor ocupada",
                }
            except Exception:  # noqa: BLE001 - error de diagnóstico controlado
                logger.exception("Error generando diagnóstico del executor")
                status_code = 500
                payload = {
                    "status": "unhealthy",
                    "healthy": False,
                    "error": "no se pudo generar el diagnóstico del executor",
                }
            try:
                self.send_json(status_code, payload)
            except OSError:
                pass

    server = _ExecutionStatusHTTPServer(
        (host, port),
        _ExecutionStatusHandler,
        max_clients=max(1, max_clients),
    )
    http_thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.25},
        name="executor-status-http",
        daemon=True,
    )
    health.attach_threads(http_thread, worker_thread or threading.current_thread())
    http_thread.start()
    logger.info(
        "Estado del executor disponible en http://%s:%s%s (alias: %s)",
        host,
        server.server_address[1],
        STATUS_ROUTE,
        HEALTH_ROUTE,
    )
    return server, http_thread
