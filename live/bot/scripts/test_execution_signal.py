#!/usr/bin/env python3
"""Prueba un ciclo ENTRY→EXIT normal del executor, exclusivamente en testnet."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


TEST_REASON = "MANUAL_EXECUTOR_TEST"
MAX_CLOSE_ATTEMPTS = 3
SUCCESS_STATUSES = {"FILLED", "PARTIALLY_FILLED"}
FAILURE_STATUSES = {"REJECTED", "EXPIRED", "RECONCILIATION_REQUIRED"}


def _event_base(symbol: str, side: str, position_id: int) -> dict:
    now_ms = int(time.time() * 1000)
    return {
        "symbol": symbol,
        "interval": "manual",
        "timeframe": "manual-test",
        "bar_time": now_ms,
        "signal_time": now_ms,
        "signal": side,
        "side": side,
        "position_id": position_id,
        "scheduled": False,
        "price": None,
        "ema_fast": None,
        "ema_slow": None,
        "adx_entry": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _test_entry_signal(symbol: str, side: str, sizing: dict) -> dict:
    position_id = time.time_ns() // 1000
    return {
        **_event_base(symbol, side, position_id),
        "event": "ENTRY",
        "action": "OPEN",
        "reason": TEST_REASON,
        "payload": {
            "action": "OPEN",
            "position_id": position_id,
            "sizing": dict(sizing),
        },
    }


def _test_exit_signal(
    symbol: str,
    side: str,
    position_id: int,
    attempt: int,
) -> dict:
    return {
        **_event_base(symbol, side, position_id),
        "event": "EXIT",
        "action": "CLOSE",
        "reason": f"{TEST_REASON}_CLOSE_{attempt}",
        "payload": {
            "action": "CLOSE",
            "position_id": position_id,
            "close_pct": 100.0,
        },
    }


def _insert_signal(store, event: dict) -> str:
    normalized = store._normalize_event(event)
    if store.insert([event]) != 1:
        raise RuntimeError(
            f"No se insertó la señal {event['event']} (posible duplicado)."
        )
    return normalized["signal_id"]


def _wait_for_execution(
    store,
    account_id: str,
    signal_id: str,
    timeout_seconds: float,
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = store.get_execution_state(account_id, signal_id)
        if state and state["status"] in SUCCESS_STATUSES | FAILURE_STATUSES:
            return state
        time.sleep(0.5)

    state = store.get_execution_state(account_id, signal_id)
    status = state["status"] if state else "sin reclamar"
    detail = f"; error={state['error']}" if state and state.get("error") else ""
    raise TimeoutError(
        f"El executor no finalizó signal_id={signal_id} "
        f"(estado={status}{detail}). No cierres la posición manualmente."
    )


def _account_snapshot(store, account_id: str) -> dict | None:
    row = store.conn.execute(
        """
        SELECT observed_at_ms, wallet_balance, available_balance,
               positions_json, open_orders_json, last_error
        FROM binance_account_snapshot
        WHERE account_id = ?
        """,
        (account_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "observed_at_ms": int(row[0]),
        "wallet_balance": row[1],
        "available_balance": row[2],
        "positions": json.loads(row[3]),
        "open_orders": json.loads(row[4]),
        "last_error": row[5],
    }


def _snapshot_is_flat(snapshot: dict, symbol: str) -> bool:
    position_open = any(
        str(position.get("symbol", "")).upper() == symbol
        and abs(float(position.get("position_amt") or 0.0)) > 0
        for position in snapshot["positions"]
    )
    order_open = any(
        str(order.get("symbol", "")).upper() == symbol
        for order in snapshot["open_orders"]
    )
    return not position_open and not order_open


def _require_fresh_flat_snapshot(store, settings) -> dict:
    snapshot = _account_snapshot(store, settings.account_id)
    if snapshot is None:
        raise RuntimeError(
            "No hay snapshot de cuenta; inicia el executor y espera su "
            "primera consulta a Binance."
        )
    max_age_ms = int(
        max(60.0, 3 * settings.account_poll_seconds) * 1000
    )
    age_ms = int(time.time() * 1000) - snapshot["observed_at_ms"]
    if age_ms < 0 or age_ms > max_age_ms:
        raise RuntimeError(
            "El snapshot de Binance no es reciente; espera a que el executor "
            "actualice la cuenta."
        )
    if snapshot["last_error"]:
        raise RuntimeError(
            "El último snapshot tiene un error: "
            f"{snapshot['last_error']}"
        )
    if not _snapshot_is_flat(snapshot, settings.symbol):
        raise RuntimeError(
            f"La cuenta Binance no está plana o tiene órdenes abiertas "
            f"para {settings.symbol}; no se insertó ninguna señal."
        )
    return snapshot


def _wait_for_flat_snapshot(
    store,
    settings,
    *,
    after_ms: int,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        snapshot = _account_snapshot(store, settings.account_id)
        if (
            snapshot is not None
            and snapshot["observed_at_ms"] >= after_ms
            and not snapshot["last_error"]
            and _snapshot_is_flat(snapshot, settings.symbol)
        ):
            return
        time.sleep(0.5)
    raise TimeoutError(
        f"No se confirmó en Binance que {settings.symbol} esté plana y sin "
        "órdenes. No cierres la posición por fuera del executor; revisa su "
        "estado y reconcilia antes de volver a probar."
    )


def _verify_executor(settings) -> None:
    if settings.http_port <= 0:
        raise RuntimeError(
            "La prueba requiere el endpoint HTTP del executor habilitado."
        )
    host = settings.http_host
    if host in {"0.0.0.0", "::", ""}:
        host = "127.0.0.1"
    url = (
        f"http://{host}:{settings.http_port}"
        "/live/bot/execution/status"
    )
    try:
        with urlopen(url, timeout=2.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        detail = (
            payload.get("executor", {}).get("account_consistency_error")
            or payload.get("status")
            or f"HTTP {exc.code}"
        )
        raise RuntimeError(
            f"El executor no está sano; no se insertó ninguna señal: {detail}"
        ) from exc
    except (OSError, URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "No se pudo verificar que el executor local esté activo y sano; "
            "no se insertó ninguna señal."
        ) from exc

    config = payload.get("config", {})
    if (
        payload.get("status") != "healthy"
        or config.get("environment") != "testnet"
        or config.get("test_mode") is not False
        or config.get("enabled") is not True
        or config.get("account_id") != settings.account_id
        or config.get("signal_db_path") != str(settings.signal_db)
        or config.get("leverage") != settings.leverage
    ):
        raise RuntimeError(
            "La configuración del executor no coincide con este perfil "
            "testnet o su estado no es healthy; no se insertó ninguna señal."
        )


def _wait_while_active(
    store,
    settings,
    position_id: int,
    hold_seconds: float,
) -> bool:
    deadline = time.monotonic() + hold_seconds
    while time.monotonic() < deadline:
        active = store.get_active_execution_positions(
            settings.account_id, settings.symbol
        )
        if not any(item["position_id"] == position_id for item in active):
            return False
        time.sleep(0.5)
    return True


def _close_test_position(
    store,
    settings,
    *,
    position_id: int,
    side: str,
    timeout_seconds: float,
    entry_signal_time_ms: int,
) -> None:
    for attempt in range(1, MAX_CLOSE_ATTEMPTS + 1):
        active = store.get_active_execution_positions(
            settings.account_id, settings.symbol
        )
        matching = [
            item for item in active if item["position_id"] == position_id
        ]
        if not matching:
            _wait_for_flat_snapshot(
                store,
                settings,
                after_ms=entry_signal_time_ms,
                timeout_seconds=timeout_seconds,
            )
            print(
                "La posición ya está plana y reconciliada "
                "(por cierre o stop protector)."
            )
            return
        if len(matching) != 1 or len(active) != 1:
            raise RuntimeError(
                "Hay posiciones activas que no pertenecen a esta prueba; "
                "no se enviará el cierre automático."
            )

        event = _test_exit_signal(
            settings.symbol, side, position_id, attempt
        )
        signal_id = _insert_signal(store, event)
        print(
            f"Salida {attempt}/{MAX_CLOSE_ATTEMPTS} guardada: {signal_id}; "
            f"open_qty={matching[0]['open_qty']:.8g}"
        )
        state = _wait_for_execution(
            store,
            settings.account_id,
            signal_id,
            timeout_seconds,
        )
        if state["status"] in FAILURE_STATUSES:
            remaining = store.get_active_execution_positions(
                settings.account_id, settings.symbol
            )
            if (
                state["status"] in {"REJECTED", "EXPIRED"}
                and not remaining
            ):
                _wait_for_flat_snapshot(
                    store,
                    settings,
                    after_ms=event["signal_time"],
                    timeout_seconds=timeout_seconds,
                )
                print(
                    "El stop protector cerró la posición antes de la salida; "
                    "Binance y ledger están planos."
                )
                return
            raise RuntimeError(
                f"La salida terminó en {state['status']}: "
                f"{state.get('error') or 'sin detalle'}. "
                "No cierres la posición manualmente."
            )

        remaining = store.get_active_execution_positions(
            settings.account_id, settings.symbol
        )
        if not any(item["position_id"] == position_id for item in remaining):
            _wait_for_flat_snapshot(
                store,
                settings,
                after_ms=event["signal_time"],
                timeout_seconds=timeout_seconds,
            )
            print("ENTRY y EXIT confirmadas; cuenta plana y ledger reconciliado.")
            return
        if state["status"] not in SUCCESS_STATUSES:
            raise RuntimeError(
                f"Estado inesperado de la salida: {state['status']}."
            )

    remaining = store.get_active_execution_positions(
        settings.account_id, settings.symbol
    )
    position = next(
        (item for item in remaining if item["position_id"] == position_id),
        None,
    )
    if position:
        raise RuntimeError(
            "Quedó cantidad abierta después de varios intentos de cierre: "
            f"{position['open_qty']:.8g}. Requiere revisión; no la cierres "
            "manualmente sin reconciliar el ledger."
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Prueba el flujo normal del executor con una ENTRY y su EXIT "
            "automático en Binance testnet."
        )
    )
    parser.add_argument(
        "--side",
        choices=("LONG", "SHORT"),
        default="LONG",
        help="dirección de la entrada (por defecto LONG)",
    )
    parser.add_argument(
        "--hold-seconds",
        type=float,
        default=3.0,
        help="segundos entre la entrada confirmada y la señal de cierre",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=90.0,
        help="espera máxima por cada señal del executor",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="confirma el ciclo completo ENTRY→EXIT en testnet",
    )
    args = parser.parse_args()
    if args.hold_seconds < 0 or args.timeout_seconds <= 0:
        parser.error("hold-seconds debe ser >= 0 y timeout-seconds > 0")

    live_dir = Path(__file__).resolve().parents[2]
    if str(live_dir) not in sys.path:
        sys.path.insert(0, str(live_dir))

    from bot.env import load_direct_profile

    load_direct_profile(live_dir, service="executor")

    from bot.config import build_strategy_config
    from bot.execution.worker import ExecutorSettings
    from bot.storage import SignalStore

    settings = ExecutorSettings.from_env()
    if settings.environment != "testnet":
        raise RuntimeError("Prueba cancelada: solo se permite Binance testnet.")
    if settings.test_mode:
        raise RuntimeError(
            "Prueba cancelada: usa BOT_EXECUTOR_TEST_MODE=false para probar "
            "el flujo normal de señales."
        )
    if not settings.enabled:
        raise RuntimeError(
            "Prueba cancelada: BOT_BINANCE_TRADING_ENABLED debe ser true."
        )
    if not settings.api_key or not settings.api_secret:
        raise RuntimeError(
            "Prueba cancelada: configura las claves API de Binance testnet."
        )
    if args.timeout_seconds > settings.max_entry_age_seconds:
        parser.error(
            "--timeout-seconds no puede superar "
            "BOT_EXECUTOR_MAX_ENTRY_AGE_SECONDS."
        )

    strategy = build_strategy_config()
    if strategy.risk_pct <= 0 or strategy.stop_loss_pct <= 0:
        raise RuntimeError(
            "Prueba cancelada: la estrategia requiere risk_pct y stop_loss_pct "
            "positivos para dimensionar y proteger la entrada."
        )
    sizing = {
        "risk_pct": strategy.risk_pct,
        "stop_loss_pct": strategy.stop_loss_pct,
        "commission_bps": strategy.commission_bps,
        "slippage_bps": strategy.slippage_bps,
        "move_stop_to_breakeven": strategy.move_stop_to_breakeven,
    }

    print(f"Base configurada: {settings.signal_db}")
    print(
        f"Testnet | cuenta={settings.account_id} | símbolo={settings.symbol} | "
        f"lado={args.side}"
    )
    print(
        "Ciclo: ENTRY MARKET + stop protector; espera "
        f"{args.hold_seconds:g}s; EXIT MARKET reduce-only."
    )
    print(
        f"Sizing de estrategia: riesgo={sizing['risk_pct']:.4%}, "
        f"stop={sizing['stop_loss_pct']:.4%}."
    )
    if not args.confirm:
        print(
            "No se guardó nada. Repite con --confirm para ejecutar el ciclo "
            "completo en testnet."
        )
        return 2
    _verify_executor(settings)
    store = SignalStore(settings.signal_db)
    store.connect()
    try:
        _require_fresh_flat_snapshot(store, settings)
        active = store.get_active_execution_positions(
            settings.account_id, settings.symbol
        )
        if active:
            raise RuntimeError(
                "Prueba cancelada: el ledger ya registra una posición activa "
                f"para {settings.symbol}; primero requiere reconciliación."
            )

        previous = store.conn.execute(
            """
            SELECT x.status
            FROM signals AS s
            LEFT JOIN signal_execution AS x
              ON x.signal_id = s.signal_id AND x.account_id = ?
            WHERE s.reason = ? AND s.event = 'ENTRY'
            ORDER BY s.id DESC
            LIMIT 1
            """,
            (settings.account_id, TEST_REASON),
        ).fetchone()
        if previous and previous[0] in {
            None,
            "READY",
            "CLAIMED",
            "SUBMITTED",
            "PARTIALLY_FILLED",
            "RECONCILIATION_REQUIRED",
        }:
            raise RuntimeError(
                "Prueba cancelada: ya existe una señal manual pendiente "
                "o que requiere reconciliación."
            )

        entry_event = _test_entry_signal(settings.symbol, args.side, sizing)
        entry_signal_id = _insert_signal(store, entry_event)
        print(f"ENTRY guardada: {entry_signal_id}")
        entry_state = _wait_for_execution(
            store,
            settings.account_id,
            entry_signal_id,
            args.timeout_seconds,
        )
        if entry_state["status"] not in SUCCESS_STATUSES:
            raise RuntimeError(
                f"La ENTRY terminó en {entry_state['status']}: "
                f"{entry_state.get('error') or 'sin detalle'}."
            )
        print(
            f"ENTRY confirmada: status={entry_state['status']}, "
            f"filled_qty={entry_state['filled_qty']:.8g}, "
            f"position_id={entry_event['position_id']}"
        )

        remains_open = _wait_while_active(
            store,
            settings,
            entry_event["position_id"],
            args.hold_seconds,
        )
        if not remains_open:
            _wait_for_flat_snapshot(
                store,
                settings,
                after_ms=entry_event["signal_time"],
                timeout_seconds=args.timeout_seconds,
            )
            print(
                "El stop protector cerró la posición; Binance y ledger "
                "están planos."
            )
            return 0

        _close_test_position(
            store,
            settings,
            position_id=entry_event["position_id"],
            side=args.side,
            timeout_seconds=args.timeout_seconds,
            entry_signal_time_ms=entry_event["signal_time"],
        )
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
