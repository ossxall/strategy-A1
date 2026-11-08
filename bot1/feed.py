
#!/usr/bin/env python3
"""
Binance USDⓈ-M Futures OHLCV Collector
- REST backfill en segundo plano
- WebSocket con watchdog y reconexión automática
- SQLite con WAL y protección de velas cerradas
- Reparación periódica de huecos
- Cierre ordenado con Ctrl+C
"""

import argparse
import logging
import os
import random
import signal
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode

import requests
import websocket


# ============================================================
# CONFIGURACIÓN
# ============================================================

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

REST_BASE_URL = "https://fapi.binance.com"
WS_BASE_URL = "wss://fstream.binance.com/ws"

SYMBOL = os.getenv("SYMBOL", "BTCUSDT").upper()
INTERVAL = os.getenv("INTERVAL", "1m")
DB_PATH = os.getenv("DB_PATH", "data/feed.db")
START_DATE = os.getenv("START_DATE", "2026-08-01")

REST_TIMEOUT_CONNECT = float(os.getenv("REST_TIMEOUT_CONNECT", "5"))
REST_TIMEOUT_READ = float(os.getenv("REST_TIMEOUT_READ", "20"))
REST_PAGE_LIMIT = min(
    1500, max(1, int(os.getenv("REST_PAGE_LIMIT", "1500")))
)
REST_REPAIR_INTERVAL = max(
    5, int(os.getenv("REST_REPAIR_INTERVAL", "60"))
)
GAP_SCAN_MINUTES = max(
    10, int(os.getenv("GAP_SCAN_MINUTES", "1440"))
)

WS_PING_INTERVAL = max(
    5, int(os.getenv("WS_PING_INTERVAL", "20"))
)
WS_PING_TIMEOUT = max(
    1, int(os.getenv("WS_PING_TIMEOUT", "10"))
)
WS_WATCHDOG_SECONDS = max(
    15, int(os.getenv("WS_WATCHDOG_SECONDS", "90"))
)
MAX_BACKOFF_SECONDS = max(
    5, int(os.getenv("MAX_BACKOFF_SECONDS", "60"))
)
# 0 (por defecto) = reintentar indefinidamente.
WS_MAX_RECOVERY_SECONDS = max(
    0, int(os.getenv("WS_MAX_RECOVERY_SECONDS", "0"))
)
WS_STABLE_SECONDS = 60

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

STOP_EVENT = threading.Event()
DB_LOCK = threading.RLock()
WS_LOCK = threading.RLock()

ACTIVE_WS = None
WS_CONNECTED = threading.Event()
BACKFILL_DONE = threading.Event()

LAST_WS_MESSAGE_MONOTONIC = 0.0
LAST_WS_MESSAGE_UTC = None
WS_OPENED_MONOTONIC = 0.0
WS_LAST_ERROR = None


# ============================================================
# LOGGING Y UTILIDADES
# ============================================================

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(threadName)s: %(message)s",
)
logger = logging.getLogger("binance_collector")


def utc_now():
    return datetime.now(timezone.utc)


def utc_iso(dt=None):
    dt = dt or utc_now()
    return dt.astimezone(timezone.utc).isoformat()


def interval_milliseconds(interval):
    """Convierte intervalos Binance habituales a milisegundos."""
    unit = interval[-1]
    amount = int(interval[:-1])

    units = {
        "m": 60_000,
        "h": 3_600_000,
        "d": 86_400_000,
        "w": 604_800_000,
    }

    if unit not in units or amount <= 0:
        raise ValueError(f"Intervalo no soportado: {interval}")

    return amount * units[unit]


INTERVAL_MS = interval_milliseconds(INTERVAL)


def parse_start_date(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


START_TIME_MS = parse_start_date(START_DATE)


def floor_interval_ms(timestamp_ms):
    return timestamp_ms - (timestamp_ms % INTERVAL_MS)


# ============================================================
# SQLITE
# ============================================================

def db_connect():
    connection = sqlite3.connect(
        DB_PATH,
        timeout=30,
        check_same_thread=False,
    )
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def init_db():
    Path(DB_PATH).resolve().parent.mkdir(
        parents=True, exist_ok=True
    )

    with DB_LOCK:
        conn = db_connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS candles (
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    open_time INTEGER NOT NULL,
                    close_time INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    quote_volume REAL NOT NULL DEFAULT 0,
                    trades INTEGER NOT NULL DEFAULT 0,
                    taker_buy_base REAL NOT NULL DEFAULT 0,
                    taker_buy_quote REAL NOT NULL DEFAULT 0,
                    is_closed INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT 'rest',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, interval, open_time)
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_candles_time
                ON candles(symbol, interval, open_time)
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS collector_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            conn.commit()
        finally:
            conn.close()


def set_state(key, value):
    with DB_LOCK:
        conn = db_connect()
        try:
            conn.execute("""
                INSERT INTO collector_state(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
            """, (key, str(value), utc_iso()))
            conn.commit()
        finally:
            conn.close()


def get_latest_open_time():
    with DB_LOCK:
        conn = db_connect()
        try:
            row = conn.execute("""
                SELECT MAX(open_time)
                FROM candles
                WHERE symbol=? AND interval=?
            """, (SYMBOL, INTERVAL)).fetchone()
            return row[0] if row and row[0] is not None else None
        finally:
            conn.close()


def save_candle(candle):
    """
    REST puede corregir una vela cerrada.
    Una vela provisional del WS nunca sobrescribe una cerrada.
    """
    with DB_LOCK:
        conn = db_connect()
        try:
            conn.execute("""
                INSERT INTO candles (
                    symbol, interval, open_time, close_time,
                    open, high, low, close, volume,
                    quote_volume, trades, taker_buy_base,
                    taker_buy_quote, is_closed, source, updated_at
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(symbol, interval, open_time)
                DO UPDATE SET
                    close_time=excluded.close_time,
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume,
                    quote_volume=excluded.quote_volume,
                    trades=excluded.trades,
                    taker_buy_base=excluded.taker_buy_base,
                    taker_buy_quote=excluded.taker_buy_quote,
                    is_closed=MAX(candles.is_closed, excluded.is_closed),
                    source=excluded.source,
                    updated_at=excluded.updated_at
                WHERE
                    candles.is_closed=0
                    OR excluded.is_closed=1
            """, (
                SYMBOL,
                INTERVAL,
                candle["open_time"],
                candle["close_time"],
                candle["open"],
                candle["high"],
                candle["low"],
                candle["close"],
                candle["volume"],
                candle["quote_volume"],
                candle["trades"],
                candle["taker_buy_base"],
                candle["taker_buy_quote"],
                candle["is_closed"],
                candle["source"],
                utc_iso(),
            ))
            conn.commit()
        finally:
            conn.close()


def normalize_candle(
    open_time,
    close_time,
    open_price,
    high,
    low,
    close_price,
    volume,
    quote_volume=0,
    trades=0,
    taker_buy_base=0,
    taker_buy_quote=0,
    is_closed=False,
    source="rest",
):
    values = [
        open_price, high, low, close_price, volume,
        quote_volume, taker_buy_base, taker_buy_quote,
    ]

    values = [float(v) for v in values]

    if not all(
        __import__("math").isfinite(v) for v in values
    ):
        raise ValueError("La vela contiene valores no finitos")

    op, hi, lo, cl, vol = values[:5]

    if min(op, hi, lo, cl) <= 0 or vol < 0:
        raise ValueError("Precio o volumen inválido")

    if hi < max(op, cl, lo) or lo > min(op, cl, hi):
        raise ValueError("Rango high/low inválido")

    if int(close_time) < int(open_time):
        raise ValueError("close_time anterior a open_time")

    return {
        "open_time": int(open_time),
        "close_time": int(close_time),
        "open": op,
        "high": hi,
        "low": lo,
        "close": cl,
        "volume": vol,
        "quote_volume": values[5],
        "trades": int(trades),
        "taker_buy_base": values[6],
        "taker_buy_quote": values[7],
        "is_closed": int(bool(is_closed)),
        "source": source,
    }


# ============================================================
# REST BINANCE
# ============================================================

def create_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "OHLCVCollector/1.0"
    })
    return session


def retry_after_seconds(response):
    value = response.headers.get("Retry-After")
    if not value:
        return None

    try:
        return max(1.0, float(value))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
            return max(
                1.0,
                (dt - datetime.now(dt.tzinfo)).total_seconds(),
            )
        except Exception:
            return None


def rest_get_klines(session, start_time, end_time):
    params = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "startTime": int(start_time),
        "endTime": int(end_time),
        "limit": REST_PAGE_LIMIT,
    }

    attempt = 0

    while not STOP_EVENT.is_set():
        attempt += 1

        try:
            response = session.get(
                f"{REST_BASE_URL}/fapi/v1/klines",
                params=params,
                timeout=(
                    REST_TIMEOUT_CONNECT,
                    REST_TIMEOUT_READ,
                ),
            )

            if response.status_code in (418, 429):
                delay = retry_after_seconds(response)
                if delay is None:
                    delay = min(
                        MAX_BACKOFF_SECONDS,
                        2 ** min(attempt, 6),
                    )
                delay += random.uniform(0, 1)
                logger.warning(
                    "Rate limit Binance HTTP %s; espera %.1fs",
                    response.status_code,
                    delay,
                )
                STOP_EVENT.wait(delay)
                continue

            response.raise_for_status()
            data = response.json()

            if not isinstance(data, list):
                raise ValueError("Respuesta REST inesperada")

            return data

        except (
            requests.RequestException,
            ValueError,
        ) as exc:
            delay = min(
                MAX_BACKOFF_SECONDS,
                2 ** min(attempt, 6),
            )
            delay += random.uniform(0, 1)

            logger.warning(
                "Error REST (%s). Reintento en %.1fs",
                exc,
                delay,
            )
            STOP_EVENT.wait(delay)

    return []


def rest_row_to_candle(row):
    now_ms = int(time.time() * 1000)

    open_time = int(row[0])
    close_time = int(row[6])

    return normalize_candle(
        open_time=open_time,
        close_time=close_time,
        open_price=row[1],
        high=row[2],
        low=row[3],
        close_price=row[4],
        volume=row[5],
        quote_volume=row[7],
        trades=row[8],
        taker_buy_base=row[9],
        taker_buy_quote=row[10],
        is_closed=(close_time < now_ms),
        source="rest",
    )


def fetch_and_save_range(session, start_ms, end_ms):
    """
    Descarga un rango paginado. Devuelve las velas procesadas.
    """
    cursor = max(0, int(start_ms))
    end_ms = int(end_ms)
    total = 0

    while cursor <= end_ms and not STOP_EVENT.is_set():
        rows = rest_get_klines(session, cursor, end_ms)

        if not rows or STOP_EVENT.is_set():
            break

        last_open_time = None

        for row in rows:
            try:
                candle = rest_row_to_candle(row)
                save_candle(candle)
                last_open_time = candle["open_time"]
                total += 1
            except (ValueError, TypeError, IndexError) as exc:
                logger.warning("Vela REST inválida: %s", exc)

        if last_open_time is None:
            break

        next_cursor = last_open_time + INTERVAL_MS
        if next_cursor <= cursor:
            logger.error("Cursor REST no avanza; se cancela página")
            break

        cursor = next_cursor

        if len(rows) < REST_PAGE_LIMIT:
            break

        # Evita hacer solicitudes REST a máxima velocidad.
        STOP_EVENT.wait(0.1)

    return total


def initial_backfill_worker():
    session = create_session()

    try:
        latest = get_latest_open_time()

        if latest is None:
            start_ms = START_TIME_MS
            logger.info(
                "Base de datos vacía; backfill desde %s UTC",
                START_DATE,
            )
        else:
            start_ms = max(
                START_TIME_MS,
                latest - 10 * INTERVAL_MS,
            )
            logger.info(
                "Backfill desde la última vela guardada "
                "(con solapamiento de 10 velas)."
            )

        end_ms = int(time.time() * 1000)

        total = fetch_and_save_range(
            session, start_ms, end_ms
        )

        logger.info("Backfill terminado: %s velas", total)
        set_state("backfill_status", "completed")

    except Exception:
        logger.exception("Fallo inesperado en backfill")
        set_state("backfill_status", "error")
    finally:
        BACKFILL_DONE.set()
        session.close()


def find_recent_gaps():
    """
    Busca huecos entre velas guardadas en una ventana reciente.
    """
    latest = get_latest_open_time()
    if latest is None:
        return []

    cutoff = latest - GAP_SCAN_MINUTES * 60_000

    with DB_LOCK:
        conn = db_connect()
        try:
            rows = conn.execute("""
                SELECT open_time
                FROM candles
                WHERE symbol=? AND interval=?
                  AND open_time >= ?
                ORDER BY open_time
            """, (SYMBOL, INTERVAL, cutoff)).fetchall()
        finally:
            conn.close()

    times = [r[0] for r in rows]
    gaps = []

    for previous, current in zip(times, times[1:]):
        expected = previous + INTERVAL_MS
        if current > expected:
            gaps.append((expected, current - INTERVAL_MS))

    return gaps


def repair_recent(session):
    now_ms = int(time.time() * 1000)
    latest = get_latest_open_time()

    if latest is None:
        start_ms = START_TIME_MS
    else:
        # Solapamiento para corregir velas recientes.
        start_ms = max(
            START_TIME_MS,
            latest - 10 * INTERVAL_MS,
        )

    fetch_and_save_range(session, start_ms, now_ms)

    for gap_start, gap_end in find_recent_gaps():
        if STOP_EVENT.is_set():
            break
        logger.warning(
            "Hueco detectado: %s - %s",
            gap_start,
            gap_end,
        )
        fetch_and_save_range(
            session, gap_start, gap_end
        )


def rest_repair_worker():
    session = create_session()

    try:
        # Espera a que el backfill inicial termine para no
        # descargar el mismo rango en paralelo.
        while not STOP_EVENT.is_set():
            if BACKFILL_DONE.wait(1):
                break
        else:
            return

        while not STOP_EVENT.is_set():
            try:
                repair_recent(session)
            except Exception:
                logger.exception("Error en reparación REST")

            STOP_EVENT.wait(REST_REPAIR_INTERVAL)
    finally:
        session.close()


# ============================================================
# WEBSOCKET
# ============================================================

def ws_url():
    stream = f"{SYMBOL.lower()}@kline_{INTERVAL}"
    return f"{WS_BASE_URL}/{stream}"


def on_message(ws, message):
    global LAST_WS_MESSAGE_MONOTONIC, LAST_WS_MESSAGE_UTC

    # Registrar actividad incluso si el mensaje no es una vela válida.
    LAST_WS_MESSAGE_MONOTONIC = time.monotonic()
    LAST_WS_MESSAGE_UTC = utc_iso()

    try:
        import json

        payload = json.loads(message)

        # Compatible con mensajes directos y streams combinados.
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
                    candle["open_time"] / 1000,
                    timezone.utc,
                ).isoformat(),
                candle["close"],
            )

    except Exception:
        logger.exception("No se pudo procesar mensaje WS")


def on_open(ws):
    global LAST_WS_MESSAGE_MONOTONIC
    global LAST_WS_MESSAGE_UTC, WS_OPENED_MONOTONIC

    WS_OPENED_MONOTONIC = time.monotonic()
    LAST_WS_MESSAGE_MONOTONIC = WS_OPENED_MONOTONIC
    LAST_WS_MESSAGE_UTC = utc_iso()

    WS_CONNECTED.set()
    set_state("websocket_status", "connected")
    logger.info("WebSocket conectado: %s", SYMBOL)


def on_error(ws, error):
    global WS_LAST_ERROR
    WS_LAST_ERROR = str(error)

    logger.warning("Error WebSocket: %s", error)
    set_state("websocket_status", "error")


def on_close(ws, status_code, message):
    WS_CONNECTED.clear()

    if not STOP_EVENT.is_set():
        set_state("websocket_status", "disconnected")

    logger.warning(
        "WebSocket cerrado: código=%s motivo=%s",
        status_code,
        message,
    )


def watchdog_worker():
    """
    Cierra el WebSocket si deja de recibir mensajes.
    El bucle principal detectará el cierre y reconectará.
    """
    while not STOP_EVENT.wait(5):
        with WS_LOCK:
            ws = ACTIVE_WS

        if ws is None or not WS_CONNECTED.is_set():
            continue

        last_message = LAST_WS_MESSAGE_MONOTONIC
        if not last_message:
            continue

        idle_seconds = time.monotonic() - last_message

        if idle_seconds >= WS_WATCHDOG_SECONDS:
            logger.error(
                "Watchdog: %.0fs sin mensajes; "
                "cerrando WebSocket para reconectar.",
                idle_seconds,
            )

            set_state("websocket_status", "stale")
            WS_CONNECTED.clear()

            try:
                ws.close()
            except Exception:
                logger.exception(
                    "No se pudo cerrar WS inactivo"
                )


def run_websocket_forever():
    """
    Reconecta con backoff exponencial.
    Si no consigue mantener una conexión funcional durante
    el periodo de recuperación, devuelve False.
    """
    global ACTIVE_WS

    backoff = 1
    recovery_started = time.monotonic()

    watchdog = threading.Thread(
        target=watchdog_worker,
        name="watchdog",
        daemon=True,
    )
    watchdog.start()

    try:
        while not STOP_EVENT.is_set():
            ws = websocket.WebSocketApp(
                ws_url(),
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )

            with WS_LOCK:
                ACTIVE_WS = ws

            connection_started = time.monotonic()

            try:
                ws.run_forever(
                    ping_interval=WS_PING_INTERVAL,
                    ping_timeout=WS_PING_TIMEOUT,
                )
            except Exception:
                logger.exception("Excepción en WebSocket")
            finally:
                WS_CONNECTED.clear()

                with WS_LOCK:
                    if ACTIVE_WS is ws:
                        ACTIVE_WS = None

                try:
                    ws.close()
                except Exception:
                    pass

            if STOP_EVENT.is_set():
                break

            connection_duration = (
                time.monotonic() - connection_started
            )

            # Una conexión mantenida durante 60 s se considera
            # recuperación; los fallos posteriores inician
            # una nueva ventana de recuperación.
            if connection_duration >= WS_STABLE_SECONDS:
                recovery_started = time.monotonic()
                backoff = 1

            failed_for = time.monotonic() - recovery_started

            if (
                WS_MAX_RECOVERY_SECONDS > 0
                and failed_for >= WS_MAX_RECOVERY_SECONDS
            ):
                logger.critical(
                    "Sin recuperación estable durante %s s.",
                    int(failed_for),
                )
                set_state("websocket_status", "failed")
                return False

            delay = min(backoff, MAX_BACKOFF_SECONDS)
            delay *= random.uniform(0.8, 1.2)

            logger.warning(
                "Reconexión en %.1f s; tiempo de recuperación %.0f s",
                delay,
                failed_for,
            )

            set_state("websocket_status", "reconnecting")

            if STOP_EVENT.wait(delay):
                break

            backoff = min(
                backoff * 2,
                MAX_BACKOFF_SECONDS,
            )

    finally:
        STOP_EVENT.set()

        with WS_LOCK:
            ws = ACTIVE_WS

        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

        watchdog.join(timeout=5)

    return True


# ============================================================
# CIERRE Y MAIN
# ============================================================

def request_stop(signum=None, frame=None):
    logger.info("Señal de parada recibida.")
    STOP_EVENT.set()

    with WS_LOCK:
        ws = ACTIVE_WS

    if ws is not None:
        try:
            ws.close()
        except Exception:
            pass


def parse_args():
    parser = argparse.ArgumentParser(
        description="Binance Futures OHLCV Collector"
    )
    parser.add_argument(
        "--symbol", default=SYMBOL
    )
    parser.add_argument(
        "--interval", default=INTERVAL
    )
    parser.add_argument(
        "--db", default=DB_PATH
    )
    parser.add_argument(
        "--start-date", default=START_DATE
    )
    return parser.parse_args()


def main():
    global SYMBOL, INTERVAL, DB_PATH
    global INTERVAL_MS, START_TIME_MS

    args = parse_args()

    SYMBOL = args.symbol.upper()
    INTERVAL = args.interval
    DB_PATH = args.db
    START_DATE = args.start_date

    INTERVAL_MS = interval_milliseconds(INTERVAL)
    START_TIME_MS = parse_start_date(START_DATE)

    init_db()

    logger.info(
        "Iniciando colector: symbol=%s interval=%s db=%s",
        SYMBOL,
        INTERVAL,
        DB_PATH,
    )

    set_state("collector_status", "running")
    set_state("websocket_status", "starting")

    # Cada trabajador tiene su propia sesión HTTP.
    backfill_thread = threading.Thread(
        target=initial_backfill_worker,
        name="initial-backfill",
        daemon=True,
    )
    repair_thread = threading.Thread(
        target=rest_repair_worker,
        name="rest-repair",
        daemon=True,
    )

    backfill_thread.start()
    repair_thread.start()

    exit_code = 0

    try:
        recovered = run_websocket_forever()

        # run_websocket_forever() siempre deja STOP_EVENT activo;
        # recovered=False indica fallo crítico del WebSocket.
        if not recovered:
            exit_code = 1

    except KeyboardInterrupt:
        logger.info("Ctrl+C recibido. Cerrando colector...")
        STOP_EVENT.set()

    finally:
        STOP_EVENT.set()

        with WS_LOCK:
            ws = ACTIVE_WS

        if ws is not None:
            try:
                ws.close()
            except Exception:
                logger.exception("Error cerrando WebSocket")

        backfill_thread.join(timeout=10)
        repair_thread.join(timeout=10)

        set_state("collector_status", "stopped")
        set_state("websocket_status", "stopped")

        logger.info(
            "Colector detenido. "
            "Los trabajadores restantes pueden terminar "
            "cuando finalicen sus operaciones bloqueantes."
        )

    return exit_code


if __name__ == "__main__":
    signal.signal(signal.SIGINT, request_stop)

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        STOP_EVENT.set()
        logger.info("Interrupción recibida; salida.")
        raise SystemExit(130)
