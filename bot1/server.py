"""Endpoint HTTP de debug: sirve el último cálculo de series por timeframe.

No conoce la estrategia: el bot le entrega DataFrames ya calculados con
`SeriesSnapshot.publish()` y este módulo solo los serializa a JSON columnar
(listo para graficar desde otro worker).

    GET /health                 estado y marca del último cálculo
    GET /series                 todos los timeframes
    GET /series/<timeframe>     un timeframe (p. ej. /series/5m)
    ?limit=N                    solo las últimas N velas (por defecto 500;
                                `limit=0` devuelve todo el histórico)

La respuesta incluye `trades`: las ENTRY/EXIT del backtest de la estrategia
(`time` en epoch ms), para superponerlas en cualquier timeframe.
"""
from __future__ import annotations

import json
import logging
import math
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import numpy as np
import pandas as pd

logger = logging.getLogger("bot1.server")

DEFAULT_LIMIT = 500


# ---------------------------------------------------------------------------
# Serialización
# ---------------------------------------------------------------------------

def _number(value) -> float | None:
    value = float(value)
    return value if math.isfinite(value) else None


def _to_list(values) -> list:
    """Columna -> lista JSON-safe (NaN/inf -> null)."""
    arr = np.asarray(values)
    if arr.dtype.kind == "b":
        return arr.tolist()
    if arr.dtype.kind in "fiu":
        return [_number(v) for v in arr.tolist()]
    return [None if v is None else str(v) for v in arr.tolist()]


def frame_to_columnar(frame: pd.DataFrame, limit: int) -> dict:
    """DataFrame indexado por tiempo -> {"t": [...], "series": {col: [...]}}."""
    if limit > 0:
        frame = frame.tail(limit)
    times = frame.index.as_unit("ms").asi8.tolist()  # epoch ms, sea cual sea la unidad
    return {
        "bars": len(frame),
        "first_bar_time": times[0] if times else None,
        "last_bar_time": times[-1] if times else None,
        "t": times,
        "series": {str(col): _to_list(frame[col]) for col in frame.columns},
    }


# ---------------------------------------------------------------------------
# Estado compartido entre el bot y el servidor
# ---------------------------------------------------------------------------

class SeriesSnapshot:
    """Último cálculo de series. Los DataFrames publicados no se mutan; la
    serialización se hace al atender cada petición."""

    def __init__(self, symbol: str, interval: str):
        self._base = {"symbol": symbol, "interval": interval}
        self._lock = threading.Lock()
        self._state: dict | None = None

    def publish(
        self,
        frames: dict[str, tuple[list[str], pd.DataFrame]],
        strategy_timeframe: str,
        trades: list[dict] | None = None,
    ) -> None:
        """`frames` = {timeframe: (roles, DataFrame)}; `trades` = lista de
        dicts JSON-safe con `time` en epoch ms."""
        state = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "strategy_timeframe": strategy_timeframe,
            "frames": frames,
            "trades": trades or [],
        }
        with self._lock:
            self._state = state

    def health(self) -> dict:
        with self._lock:
            state = self._state
        return {
            "status": "ok",
            **self._base,
            "ready": state is not None,
            "updated_at": state["updated_at"] if state else None,
            "timeframes": list(state["frames"]) if state else [],
        }

    def render(self, timeframe: str | None, limit: int) -> dict | None:
        """JSON de uno o todos los timeframes; None si el pedido no existe."""
        with self._lock:
            state = self._state
        if state is None:
            return {**self._base, "ready": False, "updated_at": None, "timeframes": []}

        frames = state["frames"]
        if timeframe is not None and timeframe not in frames:
            return None
        names = [timeframe] if timeframe is not None else list(frames)
        items = [
            {
                "timeframe": name,
                "roles": frames[name][0],
                **frame_to_columnar(frames[name][1], limit),
            }
            for name in names
        ]
        # Con `limit`, solo los trades desde el inicio de la ventana más antigua.
        starts = [i["first_bar_time"] for i in items if i["first_bar_time"] is not None]
        since = min(starts) if limit > 0 and starts else None
        return {
            **self._base,
            "ready": True,
            "updated_at": state["updated_at"],
            "strategy_timeframe": state["strategy_timeframe"],
            "limit": limit,
            "timeframes": items,
            "trades": [t for t in state["trades"] if since is None or t["time"] >= since],
        }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _route(snapshot: SeriesSnapshot, path: str, query: dict) -> tuple[int, dict]:
    try:
        limit = max(int(query.get("limit", [DEFAULT_LIMIT])[0]), 0)
    except ValueError:
        return 400, {"error": "limit debe ser un entero"}

    parts = [unquote(p) for p in path.split("/") if p]
    if parts == ["health"]:
        return 200, snapshot.health()
    if parts == ["series"]:
        return 200, snapshot.render(None, limit)
    if len(parts) == 2 and parts[0] == "series":
        data = snapshot.render(parts[1], limit)
        if data is None:
            return 404, {
                "error": f"timeframe '{parts[1]}' no disponible",
                "available": snapshot.health()["timeframes"],
            }
        return 200, data
    return 404, {
        "error": "ruta no encontrada",
        "routes": ["/health", "/series", "/series/<timeframe>"],
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = "bot1-debug/1.0"

    def log_message(self, fmt, *args):  # sin log por petición
        logger.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, payload: dict | None = None) -> None:
        body = b"" if payload is None else json.dumps(payload, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin", "*")
        if payload is None:  # preflight CORS
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        else:
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204)

    def do_GET(self):
        url = urlparse(self.path)
        try:
            status, payload = _route(self.server.snapshot, url.path, parse_qs(url.query))
        except Exception:
            logger.exception("Error atendiendo %s", self.path)
            status, payload = 500, {"error": "error interno"}
        self._send(status, payload)


def start_server(snapshot: SeriesSnapshot, host: str, port: int) -> ThreadingHTTPServer:
    """Arranca el servidor en un hilo daemon. Parar con `.shutdown()` + `.server_close()`."""
    server = ThreadingHTTPServer((host, port), _Handler)
    server.snapshot = snapshot
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="http-series", daemon=True).start()
    logger.info("Endpoint de series en http://%s:%d/series", host, port)
    return server