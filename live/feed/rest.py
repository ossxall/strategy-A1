"""Cliente REST, backfill inicial y reparación periódica de huecos."""

import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from itertools import pairwise

import requests

from .candles import normalize_candle
from .config import (
    GAP_SCAN_MINUTES,
    INTERVAL,
    INTERVAL_MS,
    MAX_BACKOFF_SECONDS,
    REST_BASE_URL,
    REST_PAGE_LIMIT,
    REST_REPAIR_INTERVAL,
    REST_TIMEOUT_CONNECT,
    REST_TIMEOUT_READ,
    START_DATE,
    START_TIME_MS,
    SYMBOL,
    logger,
    utc_iso,
)
from .database import db_connect, get_latest_open_time, save_candle, set_state
from .state import STATE, update_diagnostics


def create_session():
    session = requests.Session()
    session.headers.update({"User-Agent": "OHLCVCollector/1.0"})
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
            return max(1.0, (dt - datetime.now(dt.tzinfo)).total_seconds())
        except (TypeError, ValueError, OverflowError):
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
    while not STATE.stop_event.is_set():
        attempt += 1
        try:
            response = session.get(
                f"{REST_BASE_URL}/fapi/v1/klines",
                params=params,
                timeout=(REST_TIMEOUT_CONNECT, REST_TIMEOUT_READ),
            )
            if response.status_code in (418, 429):
                delay = retry_after_seconds(response)
                if delay is None:
                    delay = min(MAX_BACKOFF_SECONDS, 2 ** min(attempt, 6))
                delay += random.uniform(0, 1)
                logger.warning(
                    "Rate limit Binance HTTP %s; espera %.1fs",
                    response.status_code,
                    delay,
                )
                STATE.stop_event.wait(delay)
                continue
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, list):
                raise TypeError("Respuesta REST inesperada")
            return data
        except (requests.RequestException, TypeError, ValueError) as exc:
            delay = min(MAX_BACKOFF_SECONDS, 2 ** min(attempt, 6))
            delay += random.uniform(0, 1)
            logger.warning("Error REST (%s). Reintento en %.1fs", exc, delay)
            STATE.stop_event.wait(delay)
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
        is_closed=close_time < now_ms,
        source="rest",
    )


def fetch_and_save_range(session, start_ms, end_ms, progress_callback=None):
    """
    Descarga un rango paginado. Devuelve las velas procesadas.
    """
    cursor = max(0, int(start_ms))
    end_ms = int(end_ms)
    total = 0
    expected = max(1, (end_ms - cursor) // INTERVAL_MS + 1)
    page = 0
    while cursor <= end_ms and (not STATE.stop_event.is_set()):
        rows = rest_get_klines(session, cursor, end_ms)
        if not rows or STATE.stop_event.is_set():
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
        page += 1
        if progress_callback is not None:
            progress_callback(total, expected, last_open_time, page)
        if len(rows) < REST_PAGE_LIMIT:
            break
        STATE.stop_event.wait(0.1)
    return total


def initial_backfill_worker():
    session = create_session()
    started_at = utc_iso()
    update_diagnostics(
        "backfill",
        status="running",
        started_at=started_at,
        completed_at=None,
        last_error=None,
        _running_since_monotonic=time.monotonic(),
    )
    try:
        set_state("backfill_status", "running")
        latest = get_latest_open_time()
        if latest is None:
            start_ms = START_TIME_MS
            logger.info("Base de datos vacía; backfill desde %s UTC", START_DATE)
        else:
            start_ms = max(START_TIME_MS, latest - 10 * INTERVAL_MS)
            logger.info(
                "Backfill desde la última vela guardada (con solapamiento de 10 velas)."
            )
        end_ms = int(time.time() * 1000)

        expected_candles = max(1, (end_ms - start_ms) // INTERVAL_MS + 1)
        expected_pages = (expected_candles + REST_PAGE_LIMIT - 1) // REST_PAGE_LIMIT
        progress_started = time.monotonic()
        update_diagnostics(
            "backfill",
            candles_saved=0,
            candles_expected=expected_candles,
            pages_completed=0,
            pages_expected=expected_pages,
            progress_percent=0.0,
            candles_per_second=0.0,
            eta_seconds=None,
            last_candle_at=None,
        )
        logger.info(
            "Backfill planificado: %s velas (~%s páginas REST)",
            expected_candles,
            expected_pages,
        )

        def log_backfill_progress(saved, expected, last_open_time, page):
            elapsed = max(time.monotonic() - progress_started, 0.001)
            rate = saved / elapsed
            percent = min(100.0, saved * 100.0 / expected)
            remaining = max(0, expected - saved)
            eta_seconds = remaining / rate if rate > 0 else None
            last_candle_at = datetime.fromtimestamp(
                last_open_time / 1000,
                timezone.utc,
            ).isoformat()

            update_diagnostics(
                "backfill",
                candles_saved=saved,
                pages_completed=page,
                progress_percent=round(percent, 2),
                candles_per_second=round(rate, 2),
                eta_seconds=(
                    round(eta_seconds, 1) if eta_seconds is not None else None
                ),
                last_candle_at=last_candle_at,
            )
            eta_text = (
                "n/a"
                if eta_seconds is None
                else f"{eta_seconds / 60:.1f} min"
            )
            logger.info(
                "Backfill progreso: %s/%s velas (%.1f%%), "
                "página %s/%s, última=%s, velocidad=%.1f velas/s, ETA=%s",
                saved,
                expected,
                percent,
                page,
                expected_pages,
                last_candle_at,
                rate,
                eta_text,
            )

        total = fetch_and_save_range(
            session,
            start_ms,
            end_ms,
            progress_callback=log_backfill_progress,
        )
        logger.info("Backfill terminado: %s velas", total)
        update_diagnostics(
            "backfill",
            status="completed",
            completed_at=utc_iso(),
            candles_saved=total,
            progress_percent=100.0,
            eta_seconds=0.0,
            last_error=None,
            _running_since_monotonic=None,
        )
        set_state("backfill_status", "completed")
    except Exception as exc:  # noqa: BLE001 - límite del worker REST
        logger.exception("Fallo inesperado en backfill")
        update_diagnostics(
            "backfill",
            status="error",
            completed_at=utc_iso(),
            last_error=str(exc),
            _running_since_monotonic=None,
        )
        set_state("backfill_status", "error")
    finally:
        STATE.backfill_done.set()
        session.close()


def find_recent_gaps():
    """
    Busca huecos entre velas guardadas en una ventana reciente.
    """
    latest = get_latest_open_time()
    if latest is None:
        return []
    cutoff = latest - GAP_SCAN_MINUTES * 60000
    with STATE.db_lock:
        conn = db_connect()
        try:
            rows = conn.execute(
                """
                SELECT open_time
                FROM candles
                WHERE symbol=? AND interval=?
                  AND open_time >= ?
                ORDER BY open_time
                """,
                (SYMBOL, INTERVAL, cutoff),
            ).fetchall()
        finally:
            conn.close()
    times = [r[0] for r in rows]
    gaps = []
    for previous, current in pairwise(times):
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
        start_ms = max(START_TIME_MS, latest - 10 * INTERVAL_MS)
    fetch_and_save_range(session, start_ms, now_ms)
    gaps = find_recent_gaps()
    for gap_start, gap_end in gaps:
        if STATE.stop_event.is_set():
            break
        logger.warning("Hueco detectado: %s - %s", gap_start, gap_end)
        fetch_and_save_range(session, gap_start, gap_end)
    return len(gaps)


def rest_repair_worker():
    session = create_session()
    update_diagnostics("repair", status="waiting_backfill")
    try:
        while not STATE.stop_event.is_set():
            if STATE.backfill_done.wait(1):
                break
        else:
            return
        while not STATE.stop_event.is_set():
            repair_started_at = utc_iso()
            update_diagnostics(
                "repair",
                status="running",
                last_started_at=repair_started_at,
                last_error=None,
                _running_since_monotonic=time.monotonic(),
            )
            try:
                gaps_found = repair_recent(session)
            except Exception as exc:  # noqa: BLE001 - permite reintentar
                logger.exception("Error en reparación REST")
                update_diagnostics(
                    "repair",
                    status="error",
                    last_completed_at=utc_iso(),
                    last_error=str(exc),
                    _running_since_monotonic=None,
                )
            else:
                update_diagnostics(
                    "repair",
                    status="completed",
                    last_completed_at=utc_iso(),
                    last_error=None,
                    last_gaps_found=gaps_found,
                    _running_since_monotonic=None,
                )
            STATE.stop_event.wait(REST_REPAIR_INTERVAL)
    finally:
        update_diagnostics("repair", status="stopped", _running_since_monotonic=None)
        session.close()
