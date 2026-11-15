#!/usr/bin/env python3
"""Inserta una única señal ENTRY pequeña para probar el executor en testnet."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


TEST_REASON = "MANUAL_EXECUTOR_TEST"
TEST_RISK_PCT = 0.0012  # ≈12 USDT notional por cada 100 USDT de equity.
TEST_STOP_LOSS_PCT = 0.01


def _test_entry_signal(symbol: str, side: str) -> dict:
    now_ms = int(time.time() * 1000)
    position_id = time.time_ns() // 1000
    return {
        "symbol": symbol,
        "interval": "manual",
        "timeframe": "manual-test",
        "bar_time": now_ms,
        "signal_time": now_ms,
        "event": "ENTRY",
        "action": "OPEN",
        "signal": side,
        "side": side,
        "position_id": position_id,
        "scheduled": False,
        "reason": TEST_REASON,
        "price": None,
        "ema_fast": None,
        "ema_slow": None,
        "adx_entry": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "action": "OPEN",
            "position_id": position_id,
            "sizing": {
                "risk_pct": TEST_RISK_PCT,
                "risk_multiplier": 1.0,
                "stop_loss_pct": TEST_STOP_LOSS_PCT,
                "max_leverage": 1.0,
                "commission_bps": 0.0,
                "slippage_bps": 0.0,
                "move_stop_to_breakeven": False,
            },
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Guarda una señal ENTRY v3 en la misma base que consume el executor. "
            "Solo permite Binance testnet."
        )
    )
    parser.add_argument(
        "--side",
        choices=("LONG", "SHORT"),
        default="LONG",
        help="dirección de la única entrada de prueba (por defecto LONG)",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="confirma que se escriba la señal y pueda abrir una posición testnet",
    )
    args = parser.parse_args()

    live_dir = Path(__file__).resolve().parents[1]
    if str(live_dir) not in sys.path:
        sys.path.insert(0, str(live_dir))

    from bot.env import load_direct_profile

    load_direct_profile(live_dir)

    from bot.execution.worker import ExecutorSettings
    from bot.storage import SignalStore

    settings = ExecutorSettings.from_env()
    if settings.environment != "testnet":
        raise RuntimeError(
            "Prueba cancelada: solo se permite BINANCE_ENVIRONMENT=testnet"
        )
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

    print(f"Base configurada para el executor: {settings.signal_db}")
    print(
        f"Perfil: testnet | cuenta: {settings.account_id} | "
        f"símbolo: {settings.symbol} | lado: {args.side}"
    )
    print(
        "Señal: una ENTRY, riesgo objetivo 0.12% del equity, stop 1%, "
        "leverage objetivo 1x."
    )
    print(
        "Si se ejecuta, puede dejar una posición abierta en testnet hasta que "
        "se cierre por stop o por otra señal."
    )
    if not args.confirm:
        print("No se guardó nada. Repite con --confirm para insertar la señal.")
        return 2

    store = SignalStore(settings.signal_db)
    store.connect()
    try:
        active = store.get_active_execution_positions(
            settings.account_id, settings.symbol
        )
        if active:
            raise RuntimeError(
                "Prueba cancelada: el ledger ya registra una posición activa "
                f"para {settings.symbol}."
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
                "o requiere reconciliación."
            )

        event = _test_entry_signal(settings.symbol, args.side)
        normalized = store._normalize_event(event)
        if store.insert([event]) != 1:
            raise RuntimeError("No se insertó la señal manual (posible duplicado).")
        print(f"Señal guardada: {normalized['signal_id']}")
        print(
            "El executor debe estar activo y usar esta misma base; "
            "comprueba su log y el estado de ejecución."
        )
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
