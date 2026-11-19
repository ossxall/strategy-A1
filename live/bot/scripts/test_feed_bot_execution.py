#!/usr/bin/env python3
"""Prueba opt-in feed→bot→executor con órdenes reales en Binance testnet."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

SYMBOL = "BTCUSDT"
INTERVAL = "1m"
ENTRY_CLOSES = 46
STOP_LOSS_PCT = 0.05
RISK_PCT = 0.01
TERMINAL_STATUSES = {
    "FILLED",
    "PARTIALLY_FILLED",
    "REJECTED",
    "EXPIRED",
    "RECONCILIATION_REQUIRED",
}


def _strategy_config():
    """Estrategia corta y determinista; solo cambia parámetros, no el pipeline."""
    from strategy.config import Config

    return replace(
        Config(),
        timeframe="1min",
        macd_timeframe="1min",
        ema_fast=3,
        ema_slow=8,
        macd_fast=3,
        macd_slow=6,
        macd_signal=3,
        confirm_bars=1,
        risk_pct=RISK_PCT,
        stop_loss_pct=STOP_LOSS_PCT,
        allow_short=False,
        entry_adx_enabled=False,
        entry_atr_enabled=False,
        entry_price_ema_filter_enabled=False,
        entry_gaussian_trend_enabled=False,
        entry_rsi_divergence_enabled=False,
        gaussian_trend_reversal_exit_enabled=False,
        adx_exit_enabled=False,
        ema_weakening_exit_enabled=False,
        rsi_divergence_exit_enabled=False,
        partial_tp_enabled=False,
        partial_tp2_r=0,
        partial_tp2_pct=0,
        partial_tp3_r=0,
        partial_tp3_pct=0,
        partial_tp4_r=0,
        partial_tp4_pct=0,
        partial_tp5_r=0,
        partial_tp5_pct=0,
        partial_tp6_r=0,
        partial_tp6_pct=0,
        partial_tp7_r=0,
        partial_tp7_pct=0,
        streak_trend_only=False,
        force_close_at_end=False,
    )


def _seed_candles(
    feed_database,
    *,
    base_open_ms: int,
    reference_price: float,
    include_stop: bool,
) -> None:
    import numpy as np
    from feed.candles import normalize_candle

    closes = np.concatenate(
        (
            np.linspace(reference_price * 1.10, reference_price, 40),
            np.linspace(reference_price * 1.002, reference_price * 1.012, 6),
        )
    )
    price_tick = reference_price * 0.0001
    previous_close = float(closes[0])
    for index, close_value in enumerate(closes):
        close = float(close_value)
        open_price = previous_close if index else close
        high = max(open_price, close) + price_tick
        low = min(open_price, close) - price_tick
        candle = normalize_candle(
            open_time=base_open_ms + index * 60_000,
            close_time=base_open_ms + (index + 1) * 60_000 - 1,
            open_price=open_price,
            high=high,
            low=low,
            close_price=close,
            volume=1.0,
            is_closed=True,
            source="feed-bot-execution-test",
        )
        feed_database.save_candle(candle)
        previous_close = close

    if include_stop:
        index = len(closes)
        candle = normalize_candle(
            open_time=base_open_ms + index * 60_000,
            close_time=base_open_ms + (index + 1) * 60_000 - 1,
            open_price=previous_close,
            high=previous_close + price_tick,
            low=previous_close * (1.0 - STOP_LOSS_PCT - 0.01),
            close_price=previous_close,
            volume=1.0,
            is_closed=True,
            source="feed-bot-execution-test",
        )
        feed_database.save_candle(candle)


def _fetch_mark_price(gateway) -> float:
    response = gateway.mark_price(SYMBOL)
    if not isinstance(response, (dict, list)):
        dump = getattr(response, "model_dump", None)
        if callable(dump):
            response = dump(by_alias=True, mode="json")
        else:
            dump = getattr(response, "dict", None)
            if callable(dump):
                response = dump(by_alias=True)
    if isinstance(response, dict) and "data" in response:
        response = response["data"]
    if isinstance(response, list):
        response = response[0] if response else None
    value = None
    if isinstance(response, dict):
        value = response.get("markPrice", response.get("mark_price"))
    try:
        mark_price = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("No se pudo leer markPrice de Binance testnet.") from exc
    if not (mark_price > 0 and mark_price < float("inf")):
        raise RuntimeError("Binance testnet devolvió un markPrice inválido.")
    return mark_price


def _signal_row(store, event: str, *, position_id: int | None = None) -> dict | None:
    query = """
        SELECT signal_id, event, action, side, position_id, reason, signal_time
        FROM signals WHERE event = ?
    """
    params: tuple = (event,)
    if position_id is not None:
        query += " AND position_id = ?"
        params += (position_id,)
    query += " ORDER BY signal_time DESC, id DESC LIMIT 1"
    row = store.conn.execute(query, params).fetchone()
    if row is None:
        return None
    return dict(
        zip(
            (
                "signal_id",
                "event",
                "action",
                "side",
                "position_id",
                "reason",
                "signal_time",
            ),
            row,
        )
    )


def _wait_for_execution(worker, store, settings, signal_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        worker.run_once()
        state = store.get_execution_state(settings.account_id, signal_id)
        if state and state["status"] in TERMINAL_STATUSES:
            return state
        time.sleep(min(0.5, settings.poll_seconds))
    state = store.get_execution_state(settings.account_id, signal_id)
    status = state["status"] if state else "no reclamada"
    detail = f"; error={state.get('error')}" if state and state.get("error") else ""
    raise TimeoutError(
        f"Timeout esperando signal_id={signal_id} (estado={status}{detail}). "
        "La base de estado se conserva para reconciliación."
    )


def _require_flat_snapshot(store, settings) -> None:
    row = store.conn.execute(
        """
        SELECT observed_at_ms, positions_json, open_orders_json, last_error
        FROM binance_account_snapshot WHERE account_id = ?
        """,
        (settings.account_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("Binance no ha publicado un snapshot inicial.")
    observed_at_ms, positions_json, orders_json, last_error = row
    if last_error:
        raise RuntimeError(f"Falló el snapshot de Binance: {last_error}")
    max_age_ms = max(60_000, 3 * settings.account_poll_seconds * 1000)
    age_ms = int(time.time() * 1000) - int(observed_at_ms)
    if age_ms < 0 or age_ms > max_age_ms:
        raise RuntimeError("El snapshot de Binance está obsoleto.")

    positions = json.loads(positions_json)
    orders = json.loads(orders_json)
    position_open = any(
        str(item.get("symbol", "")).upper() == settings.symbol
        and abs(float(item.get("position_amt") or 0)) > 0
        for item in positions
    )
    order_open = any(
        str(item.get("symbol", "")).upper() == settings.symbol
        for item in orders
    )
    if position_open or order_open:
        raise RuntimeError(
            f"{settings.symbol} no está flat o mantiene órdenes abiertas; "
            "no se enviarán órdenes de prueba."
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Genera ENTRY/EXIT desde velas guardadas por el feed, procesa "
            "ambas con el bot y el executor real de Binance testnet."
        )
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="autoriza una entrada MARKET y su cierre en Binance testnet",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=120.0,
        help="espera máxima por cada orden (máximo 300)",
    )
    args = parser.parse_args()
    if not 0 < args.timeout_seconds <= 300:
        parser.error("--timeout-seconds debe estar entre 0 y 300")

    live_dir = Path(__file__).resolve().parents[2]
    if os.getenv("APP_ENV", "dev").strip().lower() != "dev":
        raise RuntimeError("Prueba cancelada: APP_ENV debe ser dev.")
    if str(live_dir) not in sys.path:
        sys.path.insert(0, str(live_dir))

    from env_profiles import load_direct_profile

    load_direct_profile(live_dir, service="executor")

    from feed import database as feed_database

    from bot.app import SignalBot
    from bot.config import STOP_EVENT
    from bot.execution.worker import ExecutionWorker, ExecutorSettings
    from bot.storage import FeedReader, SignalStore
    STOP_EVENT.clear()
    configured = ExecutorSettings.from_env()
    if configured.environment != "testnet":
        raise RuntimeError("Prueba cancelada: solo se permite Binance testnet.")
    if configured.test_mode:
        raise RuntimeError(
            "Prueba cancelada: BOT_EXECUTOR_TEST_MODE debe ser false; "
            "esta prueba valida el worker normal."
        )
    if not configured.enabled:
        raise RuntimeError(
            "Prueba cancelada: BOT_BINANCE_TRADING_ENABLED debe ser true."
        )
    if configured.symbol != SYMBOL:
        raise RuntimeError(
            f"Prueba cancelada: el fixture solo admite {SYMBOL}, "
            f"no {configured.symbol}."
        )
    if not configured.api_key or not configured.api_secret:
        raise RuntimeError(
            "Prueba cancelada: configura claves API de Binance testnet."
        )
    if configured.allow_live_trading:
        raise RuntimeError(
            "Prueba cancelada: BOT_BINANCE_ALLOW_LIVE_TRADING debe ser false."
        )

    run_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:8]
    )
    run_dir = live_dir / "data" / "feed_bot_execution" / run_id
    feed_path = run_dir / "feed.db"
    signal_path = run_dir / "signal.db"

    # Identidad separada: nunca se mezcla el ledger de este test con el del bot.
    settings = replace(
        configured,
        account_id=f"{configured.account_id}-feed-e2e-{run_id[-8:]}",
        signal_db=signal_path,
        leverage=1,
        max_entry_age_seconds=300,
        initial_backfill_seconds=600,
        account_poll_seconds=1.0,
        heartbeat_path=None,
        http_port=0,
    )

    print(f"Base feed: {feed_path}")
    print(f"Ledger: {signal_path}")
    print(
        "Binance testnet | símbolo=BTCUSDT | leverage objetivo=1x | "
        f"riesgo teórico configurado={RISK_PCT:.2%} del balance disponible"
    )
    if not args.confirm:
        print(
            "Modo de previsualización: no se conectó a Binance ni se creó "
            "ninguna orden. Añade --confirm para ejecutar."
        )
        return 2

    run_dir.mkdir(parents=True, exist_ok=False)
    feed_database.DB_PATH = feed_path
    feed_database.SYMBOL = SYMBOL
    feed_database.INTERVAL = INTERVAL
    feed_database.init_db()

    store = SignalStore(signal_path)
    feed = FeedReader(feed_path, SYMBOL, INTERVAL)
    store.connect()
    feed.connect()
    worker = None
    try:
        worker = ExecutionWorker(settings, store)
        # Primer ciclo: handshake real, validación One-way, filtros y snapshot.
        worker.run_once()
        _require_flat_snapshot(store, settings)
        reference_price = _fetch_mark_price(worker.gateway)

        now_floor_ms = int(time.time() * 1000) // 60_000 * 60_000
        # La vela de salida termina antes de "ahora"; ambos eventos son recientes.
        base_open_ms = now_floor_ms - (ENTRY_CLOSES + 1) * 60_000
        _seed_candles(
            feed_database,
            base_open_ms=base_open_ms,
            reference_price=reference_price,
            include_stop=False,
        )

        cfg = _strategy_config()
        bot = SignalBot(
            cfg,
            feed,
            store,
            SYMBOL,
            INTERVAL,
            min_bars=cfg.ema_slow + 2,
            live_only=True,
            live_since_ms=base_open_ms,
            mode="live",
        )
        inserted = bot.process_once()
        entry = _signal_row(store, "ENTRY")
        if inserted != 1 or entry is None or entry["side"] != "LONG":
            raise AssertionError(
                "El feed determinista no produjo exactamente una ENTRY LONG "
                f"(inserted={inserted}, entry={entry})."
            )
        print(
            f"Bot publicó ENTRY LONG ({entry['reason']}): "
            f"{entry['signal_id']}"
        )

        entry_state = _wait_for_execution(
            worker, store, settings, entry["signal_id"], args.timeout_seconds
        )
        if entry_state["status"] not in {"FILLED", "PARTIALLY_FILLED"}:
            raise RuntimeError(
                f"ENTRY terminó en {entry_state['status']}: "
                f"{entry_state.get('error') or 'sin detalle'}."
            )
        print(
            f"Binance confirmó ENTRY: {entry_state['status']}, "
            f"filled_qty={entry_state['filled_qty']:.8g}"
        )

        # La vela cerrada atraviesa el stop simulado; el bot calcula el EXIT.
        _seed_candles(
            feed_database,
            base_open_ms=base_open_ms,
            reference_price=reference_price,
            include_stop=True,
        )
        exit_inserted = bot.process_once()
        exit_event = _signal_row(
            store, "EXIT", position_id=int(entry["position_id"])
        )
        if exit_inserted != 1 or exit_event is None \
                or not exit_event["reason"].startswith("STOP_LOSS"):
            raise AssertionError(
                "El feed no generó el EXIT de stop-loss esperado "
                f"(inserted={exit_inserted}, exit={exit_event})."
            )
        print(
            f"Bot publicó EXIT ({exit_event['reason']}): "
            f"{exit_event['signal_id']}"
        )

        exit_state = _wait_for_execution(
            worker, store, settings, exit_event["signal_id"], args.timeout_seconds
        )
        if exit_state["status"] != "FILLED":
            raise RuntimeError(
                f"EXIT terminó en {exit_state['status']}: "
                f"{exit_state.get('error') or 'sin detalle'}."
            )

        # Refresca posiciones y órdenes en Binance después del fill de cierre.
        worker.run_once()
        _require_flat_snapshot(store, settings)
        active = store.get_active_execution_positions(settings.account_id, SYMBOL)
        if active:
            raise RuntimeError(
                f"Binance aparece flat pero el ledger conserva posiciones: {active}"
            )
        print(
            "PASS: feed → SignalBot → señales SQLite → ExecutionWorker → "
            "Binance testnet; ENTRY y EXIT confirmadas, cuenta flat."
        )
        return 0
    except Exception:
        print(
            "La prueba falló. Se conserva el directorio con feed.db y signal.db "
            f"para inspección/reconciliación: {run_dir}",
            file=sys.stderr,
        )
        raise
    finally:
        feed.close()
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
