"""Orquestación del ciclo operativo de prueba del executor."""

from __future__ import annotations

import json
import logging
import time
from decimal import Decimal

from ..config import STOP_EVENT, utc_iso
from .binance_usdm import BinanceOrderNotFound
from .test_mode_policy import (
    TEST_MODE_MAX_LEVERAGE,
    TEST_MODE_MAX_MARGIN_USDT,
    TEST_MODE_PAUSE_SECONDS,
    TEST_MODE_STOP_LOSS_PCT,
    calculate_test_entry_quantity,
)
from .worker import (
    ExecutionWorker,
    _decimal,
    _decimal_text,
    _float,
    _get,
    _items,
)

LOG = logging.getLogger("bot1.executor")


class ExecutionTestMode:
    """Conduce ciclos de prueba a través del worker de ejecución compartido.

    El controlador prepara señales sintéticas y las procesa con ``run_once``;
    la validación, sizing y envío siguen pasando por la lógica habitual del worker.
    """

    def __init__(self, worker: ExecutionWorker):
        self.worker = worker
        self._test_position_sequence = time.time_ns() // 1000
        self._test_next_side = "LONG"

    @property
    def next_side(self) -> str:
        return self._test_next_side

    def _test_signal(
        self,
        *,
        event: str,
        action: str,
        side: str,
        position_id: int,
        reason: str,
        close_pct: float | None = None,
    ) -> dict:
        now_ms = self.worker.clock_ms()
        sizing = {
            "risk_pct": 0.01,
            "risk_multiplier": 1.0,
            "stop_loss_pct": TEST_MODE_STOP_LOSS_PCT,
            "commission_bps": 0.0,
            "slippage_bps": 0.0,
            "move_stop_to_breakeven": True,
        }
        payload = {
            "action": action,
            "position_id": position_id,
            "sizing": sizing,
        }
        if event == "ENTRY":
            payload["entry_pct"] = 100.0
        else:
            payload["close_pct"] = close_pct
        raw = {
            "schema_version": 3,
            "symbol": "BTCUSDT",
            "interval": "test",
            "timeframe": "test",
            "bar_time": now_ms,
            "signal_time": now_ms,
            "event": event,
            "action": action,
            "signal": side,
            "side": side,
            "position_id": position_id,
            "scheduled": False,
            "reason": reason,
            "price": None,
            "ema_fast": None,
            "ema_slow": None,
            "adx_entry": None,
            "executed": 0,
            "created_at": utc_iso(),
            "payload": payload,
        }
        normalized = self.worker.store._normalize_event(raw)
        if self.worker.store.insert([raw]) != 1:
            raise RuntimeError(
                f"EXECUTOR_TEST_MODE: no se pudo insertar señal {event}/{action}"
            )
        return normalized

    def _run_test_signal(self, **kwargs) -> dict:
        test_signal = self._test_signal(**kwargs)
        self.worker.run_once()
        state = self.worker.store.get_execution_state(
            self.worker.settings.account_id, test_signal["signal_id"]
        )
        if state is None or state["status"] != "FILLED":
            status = state["status"] if state else "sin estado"
            detail = f": {state['error']}" if state and state.get("error") else ""
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: secuencia detenida; "
                f"{test_signal['event']}/{test_signal['action']} terminó en "
                f"{status}{detail}"
            )
        return state

    def _stop_closed_test_position(
        self, position_id: int, side: str
    ) -> bool:
        """Accepts a confirmed Binance stop fill and halts before another cycle."""
        try:
            snapshot = self.worker._reconcile_protective_stops(
                self.worker._account_snapshot(force=True)
            )
        except Exception:
            LOG.exception(
                "No se pudo confirmar si el stop cerró la posición de prueba"
            )
            return False
        if snapshot.get("reconciliation_error"):
            return False
        if (
            self.worker._position_for_symbol(snapshot) is not None
            or snapshot.get("open_orders")
            or snapshot.get("open_algo_orders")
            or self.worker.store.get_active_execution_positions(
                self.worker.settings.account_id, self.worker.settings.symbol
            )
        ):
            return False

        stop_fill = self.worker.store.conn.execute(
            """
            SELECT 1
            FROM execution_adjustments
            WHERE account_id = ?
              AND symbol = ?
              AND position_id = ?
              AND reason = 'BINANCE_PROTECTIVE_STOP'
            LIMIT 1
            """,
            (
                self.worker.settings.account_id,
                self.worker.settings.symbol,
                int(position_id),
            ),
        ).fetchone()
        if stop_fill is None:
            return False

        self._test_next_side = "SHORT" if side == "LONG" else "LONG"
        STOP_EVENT.set()
        LOG.warning(
            "Stop protector de Binance cerró la posición de prueba %s %s; "
            "cuenta flat y reconciliada. Test mode se detiene sin abrir "
            "otra posición.",
            side,
            self.worker.settings.symbol,
        )
        return True

    def _test_entry_signal(self, position_id: int) -> dict:
        cursor = self.worker.store.conn.execute(
            """
            SELECT s.*
            FROM signals AS s
            JOIN signal_execution AS x ON x.signal_id = s.signal_id
            WHERE x.account_id = ?
              AND s.symbol = ?
              AND s.position_id = ?
              AND s.event = 'ENTRY'
              AND s.reason = 'EXECUTOR_TEST_ENTRY'
            ORDER BY s.id DESC
            LIMIT 1
            """,
            (
                self.worker.settings.account_id,
                self.worker.settings.symbol,
                int(position_id),
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: posición activa sin ENTRY test correlacionable."
            )
        names = [column[0] for column in cursor.description]
        signal = dict(zip(names, row))
        signal["payload"] = (
            json.loads(signal["payload"])
            if isinstance(signal["payload"], str)
            else signal["payload"]
        )
        return signal

    def recover_active_position(
        self, active: dict, snapshot: dict
    ) -> dict:
        position_id = int(active["position_id"])
        exits = self.worker.store.conn.execute(
            """
            SELECT s.signal_id, s.action, s.reason, x.status,
                   x.order_status, x.filled_qty
            FROM signals AS s
            JOIN signal_execution AS x ON x.signal_id = s.signal_id
            WHERE x.account_id = ?
              AND s.symbol = ?
              AND s.position_id = ?
              AND s.event = 'EXIT'
            ORDER BY s.id
            """,
            (self.worker.settings.account_id, self.worker.settings.symbol, position_id),
        ).fetchall()
        partial_execution = None
        if exits:
            if (
                len(exits) == 1
                and exits[0][1] == "PARTIAL_CLOSE"
                and exits[0][2] in {
                    "PARTIAL_TP_1R",
                    "EXECUTOR_TEST_PARTIAL",
                }
                and exits[0][3] in {"FILLED", "RECONCILIATION_REQUIRED"}
                and exits[0][4] == "FILLED"
                and float(exits[0][5] or 0) > 0
            ):
                partial_execution = {
                    "signal_id": exits[0][0],
                    "reason": exits[0][2],
                    "filled_qty": float(exits[0][5]),
                    "status": exits[0][3],
                }
            else:
                raise RuntimeError(
                    "EXECUTOR_TEST_MODE: posición con salidas previas pendiente "
                    "de reconciliación manual."
                )

        signal = self._test_entry_signal(position_id)
        position = self.worker._position_for_symbol(snapshot)
        if snapshot.get("open_orders"):
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: hay órdenes MARKET pendientes; "
                "no se recuperará la posición automáticamente."
            )
        if position is None:
            if snapshot.get("open_algo_orders"):
                raise RuntimeError(
                    "EXECUTOR_TEST_MODE: cuenta flat con stops abiertos; "
                    "requiere revisión manual."
                )
            open_qty = _decimal(active["open_qty"], "open_qty")
            self.worker.store.save_execution_adjustment(
                self.worker.settings.account_id,
                self.worker.settings.symbol,
                position_id,
                f"test-flat-recovery:{position_id}",
                float(open_qty),
                None,
                "EXECUTOR_TEST_MODE: ENTRY test previa ya está flat",
                self.worker.clock_ms(),
            )
            LOG.warning(
                "EXECUTOR_TEST_MODE: posición test %s ya está flat; "
                "ledger reconciliado sin enviar órdenes.",
                position_id,
            )
            return {
                "position_id": position_id,
                "side": active["side"],
                "already_flat": True,
                "partial_completed": partial_execution is not None,
            }

        actual_side = "LONG" if position["position_amt"] > 0 else "SHORT"
        actual_qty = abs(_decimal(position["position_amt"], "positionAmt"))
        expected_qty = _decimal(active["open_qty"], "open_qty")
        tolerance = self.worker._rules_cache[self.worker.settings.symbol]["step_size"] / 2
        if (
            actual_side != active["side"]
            or abs(actual_qty - expected_qty) > tolerance
        ):
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: posición activa no coincide con el ledger; "
                "requiere reconciliación manual."
            )

        stop_variant = "sl"
        if (
            partial_execution is not None
            and partial_execution["reason"].startswith("PARTIAL_TP_")
            and (signal["payload"].get("sizing") or {}).get(
                "move_stop_to_breakeven"
            )
        ):
            mark_data = _items(
                self.worker.gateway.mark_price(self.worker.settings.symbol), "data"
            )
            mark_value = _float(
                _get(mark_data[0] if mark_data else {}, "markPrice", "mark_price")
            )
            if mark_value is None or mark_value <= 0:
                raise ValueError(
                    "EXECUTOR_TEST_MODE: markPrice inválido al recuperar parcial"
                )
            entry_price = _decimal(position["entry_price"], "entry_price")
            mark_price = _decimal(mark_value, "mark_price")
            breakeven_is_valid = (
                mark_price > entry_price
                if active["side"] == "LONG"
                else mark_price < entry_price
            )
            if breakeven_is_valid:
                stop_variant = "be"

        stop_ids = {
            self.worker._algo_order_id(position_id, variant)
            for variant in ("sl", "be")
        }
        open_stop_ids = {
            str(_get(order, "clientAlgoId", "client_algo_id", default=""))
            for order in snapshot.get("open_algo_orders", [])
        }
        if open_stop_ids - stop_ids:
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: hay stops ajenos a la posición test; "
                "requiere revisión manual."
            )
        stop_id = self.worker._ensure_protective_stop(
            signal, snapshot, breakeven=stop_variant == "be"
        )
        self.worker._verify_protective_stop(stop_id)
        self.worker._cancel_position_stop(
            position_id, "sl" if stop_variant == "be" else "be"
        )
        LOG.warning(
            "EXECUTOR_TEST_MODE: posición test %s recuperada y stop confirmado.",
            position_id,
        )
        return {
            "position_id": position_id,
            "side": active["side"],
            "partial_completed": partial_execution is not None,
            "stop_variant": stop_variant,
        }

    def _recover_test_partial_inflight(self, execution: dict) -> dict:
        if (
            execution["action"] != "PARTIAL_CLOSE"
            or execution["reason"] not in {
                "PARTIAL_TP_1R",
                "EXECUTOR_TEST_PARTIAL",
            }
            or execution["order_status"] != "FILLED"
            or not execution["client_order_id"]
            or float(execution["filled_qty"] or 0) <= 0
        ):
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: parcial previo no verificable; "
                "requiere reconciliación manual."
            )
        order = self.worker.gateway.query_order(
            self.worker.settings.symbol, execution["client_order_id"]
        )
        order_status = str(_get(order, "status", "orderStatus", default="")).upper()
        order_qty = _decimal(
            _get(order, "executedQty", "executed_qty", default="0"),
            "executedQty",
        )
        expected_qty = _decimal(execution["filled_qty"], "filled_qty")
        tolerance = self.worker._rules_cache[self.worker.settings.symbol]["step_size"] / 2
        if order_status != "FILLED" or abs(order_qty - expected_qty) > tolerance:
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: el parcial confirmado por Binance no "
                "coincide con el ledger."
            )

        active_positions = self.worker.store.get_active_execution_positions(
            self.worker.settings.account_id, self.worker.settings.symbol
        )
        if (
            len(active_positions) != 1
            or active_positions[0]["position_id"] != int(execution["position_id"])
        ):
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: no se pudo correlacionar el remanente "
                "del parcial con Binance."
            )
        snapshot = self.worker._account_snapshot(force=True)
        recovered = self.recover_active_position(
            active_positions[0], snapshot
        )
        result = {
            "order": order,
            "recovered_partial": True,
        }
        if recovered.get("already_flat"):
            result["recovered_flat"] = True
        else:
            result["protective_stop_client_algo_id"] = self.worker._algo_order_id(
                int(execution["position_id"]),
                recovered.get("stop_variant", "sl"),
            )
        average_price = _float(
            _get(order, "avgPrice", "averagePrice", "avg_price")
        )
        self.worker.store.update_execution(
            self.worker.settings.account_id,
            execution["signal_id"],
            "FILLED",
            order_status="FILLED",
            client_order_id=execution["client_order_id"],
            exchange_order_id=str(_get(order, "orderId", "order_id", default="")),
            filled_qty=float(order_qty),
            average_fill_price=average_price,
            result=result,
            error=None,
            reconciled_at_ms=self.worker.clock_ms(),
        )
        LOG.warning(
            "EXECUTOR_TEST_MODE: parcial previo %s confirmado; se evita "
            "reenviar el cierre.",
            execution["signal_id"],
        )
        return {
            **recovered,
            "partial_completed": True,
        }

    def _recover_test_close_inflight(self, execution: dict) -> dict:
        if (
            execution["action"] != "CLOSE"
            or execution["reason"] != "EXECUTOR_TEST_CLOSE"
            or execution["order_status"] != "FILLED"
            or not execution["client_order_id"]
            or float(execution["filled_qty"] or 0) <= 0
        ):
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: cierre previo no verificable; "
                "requiere reconciliación manual."
            )
        order = self.worker.gateway.query_order(
            self.worker.settings.symbol, execution["client_order_id"]
        )
        order_status = str(_get(order, "status", "orderStatus", default="")).upper()
        order_qty = _decimal(
            _get(order, "executedQty", "executed_qty", default="0"),
            "executedQty",
        )
        expected_qty = _decimal(execution["filled_qty"], "filled_qty")
        tolerance = self.worker._rules_cache[self.worker.settings.symbol]["step_size"] / 2
        if order_status != "FILLED" or abs(order_qty - expected_qty) > tolerance:
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: el cierre confirmado por Binance no "
                "coincide con el ledger."
            )

        snapshot = self.worker._account_snapshot(force=True)
        if (
            self.worker._position_for_symbol(snapshot) is not None
            or snapshot.get("open_orders")
            or snapshot.get("open_algo_orders")
            or self.worker.store.get_active_execution_positions(
                self.worker.settings.account_id, self.worker.settings.symbol
            )
        ):
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: el cierre previo no dejó la cuenta "
                "flat y sin órdenes; requiere reconciliación manual."
            )
        average_price = _float(
            _get(order, "avgPrice", "averagePrice", "avg_price")
        )
        self.worker.store.update_execution(
            self.worker.settings.account_id,
            execution["signal_id"],
            "FILLED",
            order_status="FILLED",
            client_order_id=execution["client_order_id"],
            exchange_order_id=str(_get(order, "orderId", "order_id", default="")),
            filled_qty=float(order_qty),
            average_fill_price=average_price,
            result={"order": order, "recovered_close": True},
            error=None,
            reconciled_at_ms=self.worker.clock_ms(),
        )
        LOG.warning(
            "EXECUTOR_TEST_MODE: cierre previo %s confirmado; cuenta flat.",
            execution["signal_id"],
        )
        return {
            "position_id": int(execution["position_id"]),
            "side": execution["side"],
            "already_flat": True,
            "partial_completed": True,
        }

    def _resolve_finished_test_breakeven(
        self, position_id: int, side: str
    ) -> bool:
        """Reconciles a BE stop that finished between placement and verification."""
        snapshot = self.worker._reconcile_protective_stops(
            self.worker._account_snapshot(force=True)
        )
        if snapshot.get("reconciliation_error"):
            raise RuntimeError(str(snapshot["reconciliation_error"]))
        active = self.worker.store.get_active_execution_positions(
            self.worker.settings.account_id, self.worker.settings.symbol
        )
        position = self.worker._position_for_symbol(snapshot)
        if position is None:
            if len(active) == 1 and active[0]["position_id"] == position_id:
                recovery = self.recover_active_position(
                    active[0], snapshot
                )
                if not recovery.get("already_flat"):
                    raise RuntimeError(
                        "El stop a breakeven terminó, pero el ledger no quedó flat."
                    )
                active = self.worker.store.get_active_execution_positions(
                    self.worker.settings.account_id, self.worker.settings.symbol
                )
            if not active:
                LOG.warning(
                    "EXECUTOR_TEST_MODE: stop a breakeven cerró la posición; "
                    "ciclo completado."
                )
                return True
            raise RuntimeError(
                "El stop terminó pero queda una posición lógica sin fill "
                "reconciliado."
            )
        if len(active) != 1 or active[0]["position_id"] != position_id:
            raise RuntimeError(
                "Stop a breakeven terminó con una posición no correlacionada."
            )
        actual_side = "LONG" if position["position_amt"] > 0 else "SHORT"
        expected_qty = _decimal(active[0]["open_qty"], "open_qty")
        actual_qty = abs(_decimal(position["position_amt"], "positionAmt"))
        tolerance = self.worker._rules_cache[self.worker.settings.symbol]["step_size"] / 2
        if actual_side != side or abs(actual_qty - expected_qty) > tolerance:
            raise RuntimeError(
                "Posición Binance difiere del ledger tras finalizar breakeven."
            )
        entry_signal = self._test_entry_signal(position_id)
        sl_id = self.worker._ensure_protective_stop(entry_signal, snapshot)
        self.worker._verify_protective_stop(sl_id)
        LOG.warning(
            "EXECUTOR_TEST_MODE: BE terminó con remanente; SL original "
            "confirmado para cerrar el ciclo."
        )
        return False

    def recover_inflight(self) -> dict | None:
        """Recupera un ENTRY test solo tras verificarlo con Binance."""
        inflight = self.worker.store.list_inflight_executions(
            self.worker.settings.account_id
        )
        if not inflight:
            return None
        if len(inflight) != 1:
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: hay varias ejecuciones pendientes; "
                "requieren reconciliación manual."
            )
        execution = inflight[0]
        if execution["execution_status"] != "RECONCILIATION_REQUIRED":
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: hay una ejecución pendiente; "
                "requiere reconciliación manual."
            )
        if execution["event"] == "EXIT":
            if execution["action"] == "PARTIAL_CLOSE":
                return self._recover_test_partial_inflight(execution)
            if execution["action"] == "CLOSE":
                return self._recover_test_close_inflight(execution)
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: acción EXIT previa no recuperable."
            )
        if (
            execution["event"] != "ENTRY"
            or execution["reason"] != "EXECUTOR_TEST_ENTRY"
            or not execution["client_order_id"]
        ):
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: la ejecución previa no puede "
                "recuperarse automáticamente; requiere reconciliación manual."
            )

        if execution["order_status"] == "PREPARED":
            try:
                self.worker.gateway.query_order(
                    self.worker.settings.symbol, execution["client_order_id"]
                )
            except BinanceOrderNotFound:
                self.worker.store.update_execution(
                    self.worker.settings.account_id,
                    execution["signal_id"],
                    "REJECTED",
                    order_status="NOT_FOUND",
                    client_order_id=execution["client_order_id"],
                    error=(
                        "Test mode: se verificó que el probe PREPARED no llegó "
                        "a Binance; se libera para una nueva secuencia."
                    ),
                    reconciled_at_ms=self.worker.clock_ms(),
                )
                return None
            except Exception as exc:
                raise RuntimeError(
                    "EXECUTOR_TEST_MODE: no se pudo verificar la orden previa; "
                    "no se enviarán nuevas órdenes."
                ) from exc
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: Binance encontró la orden PREPARED; "
                "no se reenviará ni se abrirá otra posición."
            )

        if (
            execution["order_status"] != "FILLED"
            or float(execution["filled_qty"] or 0) <= 0
        ):
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: la ENTRY previa no tiene un fill "
                "confirmado; requiere reconciliación manual."
            )

        expected_qty = _decimal(execution["filled_qty"], "filled_qty")
        order = self.worker.gateway.query_order(
            self.worker.settings.symbol, execution["client_order_id"]
        )
        order_status = str(_get(order, "status", "orderStatus", default="")).upper()
        order_qty = _decimal(
            _get(order, "executedQty", "executed_qty", default="0"),
            "executedQty",
        )
        tolerance = self.worker._rules_cache[self.worker.settings.symbol]["step_size"] / 2
        if order_status != "FILLED" or abs(order_qty - expected_qty) > tolerance:
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: la orden confirmada por Binance no "
                "coincide con el fill persistido."
            )

        snapshot = self.worker._account_snapshot(force=True)
        position = self.worker._position_for_symbol(snapshot)
        if snapshot.get("open_orders"):
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: no se puede verificar la posición previa "
                "de Binance; no se enviarán nuevas órdenes."
            )
        if position is None:
            if snapshot.get("open_algo_orders"):
                raise RuntimeError(
                    "EXECUTOR_TEST_MODE: la cuenta está flat pero mantiene "
                    "órdenes condicionales; requiere revisión manual."
                )
            average_price = _float(
                _get(order, "avgPrice", "averagePrice", "avg_price")
            )
            self.worker.store.save_execution_adjustment(
                self.worker.settings.account_id,
                self.worker.settings.symbol,
                int(execution["position_id"]),
                f"test-flat-recovery:{execution['signal_id']}",
                float(order_qty),
                average_price,
                "EXECUTOR_TEST_MODE: fill previo confirmado y cuenta flat al recuperar",
                self.worker.clock_ms(),
            )
            self.worker.store.update_execution(
                self.worker.settings.account_id,
                execution["signal_id"],
                "FILLED",
                order_status="FILLED",
                client_order_id=execution["client_order_id"],
                exchange_order_id=str(
                    _get(order, "orderId", "order_id", default="")
                ),
                filled_qty=float(order_qty),
                average_fill_price=average_price,
                result={"order": order, "recovered_flat": True},
                error=None,
                reconciled_at_ms=self.worker.clock_ms(),
            )
            LOG.warning(
                "EXECUTOR_TEST_MODE: posición previa %s ya está flat; "
                "fill reconciliado sin enviar órdenes.",
                execution["position_id"],
            )
            return {
                "position_id": int(execution["position_id"]),
                "side": execution["side"],
                "already_flat": True,
            }

        actual_side = "LONG" if position["position_amt"] > 0 else "SHORT"
        actual_qty = abs(_decimal(position["position_amt"], "positionAmt"))
        if (
            actual_side != execution["side"]
            or abs(actual_qty - expected_qty) > tolerance
        ):
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: el fill previo no coincide con la "
                "posición real; requiere reconciliación manual."
            )

        recovered = self.worker._record_order_result(
            execution,
            execution["client_order_id"],
            order,
            expected_qty,
            is_entry=True,
        )
        state = self.worker.store.get_execution_state(
            self.worker.settings.account_id, execution["signal_id"]
        )
        if not recovered or state is None or state["status"] != "FILLED":
            detail = f": {state['error']}" if state and state.get("error") else ""
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: se verificó la ENTRY previa, pero no se "
                f"pudo protegerla{detail}"
            )
        return {
            "position_id": int(execution["position_id"]),
            "side": execution["side"],
        }

    def run_sequence(self) -> None:
        """Ejecuta un ciclo real acotado: entrada, parcial, breakeven y cierre."""
        if not self.worker.settings.test_mode or not self.worker.settings.enabled:
            raise RuntimeError("La secuencia requiere EXECUTOR_TEST_MODE y trading activo")
        self.worker._initialize()
        recovered = self.recover_inflight()
        snapshot = self.worker._account_snapshot(force=True)
        if recovered is None:
            active_positions = self.worker.store.get_active_execution_positions(
                self.worker.settings.account_id, "BTCUSDT"
            )
            if len(active_positions) > 1:
                raise RuntimeError(
                    "EXECUTOR_TEST_MODE: ledger tiene varias posiciones activas; "
                    "requiere reconciliación manual."
                )
            if active_positions:
                recovered = self.recover_active_position(
                    active_positions[0], snapshot
                )
        if recovered and recovered.get("already_flat"):
            self._test_next_side = (
                "SHORT" if recovered["side"] == "LONG" else "LONG"
            )
            STOP_EVENT.set()
            LOG.warning(
                "EXECUTOR_TEST_MODE: reconciliación flat completada; "
                "modo detenido para no reabrir otra posición automáticamente."
            )
            return
        if recovered is None:
            if (
                self.worker._position_for_symbol(snapshot) is not None
                or self.worker.store.get_active_execution_positions(
                    self.worker.settings.account_id, "BTCUSDT"
                )
                or snapshot.get("open_orders")
                or snapshot.get("open_algo_orders")
            ):
                raise RuntimeError(
                    "EXECUTOR_TEST_MODE: requiere BTCUSDT flat y sin órdenes "
                    "abiertas; no se colocó ninguna orden."
                )

            mark_data = _items(self.worker.gateway.mark_price("BTCUSDT"), "data")
            mark_price = _float(
                _get(mark_data[0] if mark_data else {}, "markPrice", "mark_price")
            )
            if mark_price is None or mark_price <= 0:
                raise ValueError("EXECUTOR_TEST_MODE: markPrice inválido")
            planned_quantity = calculate_test_entry_quantity(
                self.worker._rules_cache[self.worker.settings.symbol],
                _decimal(mark_price, "mark_price"),
                _decimal(snapshot["available_balance"], "available_balance"),
                Decimal(TEST_MODE_MAX_LEVERAGE),
            )
            LOG.info(
                "EXECUTOR_TEST_MODE preflight: qty=%s notional≈%.2f USDT "
                "margen≈%.2f USDT",
                _decimal_text(planned_quantity),
                float(planned_quantity * _decimal(mark_price, "mark_price")),
                float(
                    planned_quantity
                    * _decimal(mark_price, "mark_price")
                    / Decimal(TEST_MODE_MAX_LEVERAGE)
                ),
            )

            self._test_position_sequence += 1
            position_id = self._test_position_sequence
            side = self._test_next_side
            entry_state = self._run_test_signal(
                event="ENTRY",
                action="OPEN",
                side=side,
                position_id=position_id,
                reason="EXECUTOR_TEST_ENTRY",
            )
            if entry_state["filled_qty"] <= 0:
                raise RuntimeError(
                    "EXECUTOR_TEST_MODE: ENTRY no confirmó cantidad"
                )
        else:
            position_id = recovered["position_id"]
            side = recovered["side"]

        stop_variant = (
            recovered.get("stop_variant", "sl") if recovered else "sl"
        )
        stop_id = self.worker._algo_order_id(position_id, stop_variant)
        try:
            self.worker._verify_protective_stop(stop_id)
        except RuntimeError:
            if self._stop_closed_test_position(position_id, side):
                return
            if stop_variant != "be":
                raise
            if self._resolve_finished_test_breakeven(position_id, side):
                self._test_next_side = (
                    "SHORT" if side == "LONG" else "LONG"
                )
                return
            stop_id = self.worker._algo_order_id(position_id, "sl")
            self.worker._verify_protective_stop(stop_id)

        partial_completed = bool(
            recovered and recovered.get("partial_completed")
        )
        if not partial_completed:
            partial_reason = (
                "PARTIAL_TP_1R"
                if side == "LONG"
                else "EXECUTOR_TEST_PARTIAL"
            )
            try:
                partial_state = self._run_test_signal(
                    event="EXIT",
                    action="PARTIAL_CLOSE",
                    side=side,
                    position_id=position_id,
                    reason=partial_reason,
                    close_pct=50.0,
                )
            except RuntimeError:
                if self._stop_closed_test_position(position_id, side):
                    return
                raise
            partial_result = partial_state.get("result") or {}
            stop_id = partial_result.get(
                "protective_stop_client_algo_id"
            ) or self.worker._algo_order_id(position_id, "sl")
            stop_variant = (
                "be"
                if stop_id == self.worker._algo_order_id(position_id, "be")
                else "sl"
            )
        else:
            stop_variant = recovered.get("stop_variant", "sl")
            stop_id = self.worker._algo_order_id(position_id, stop_variant)
        try:
            self.worker._verify_protective_stop(stop_id)
        except RuntimeError:
            if self._stop_closed_test_position(position_id, side):
                return
            if stop_variant != "be":
                raise
            if self._resolve_finished_test_breakeven(position_id, side):
                self._test_next_side = (
                    "SHORT" if side == "LONG" else "LONG"
                )
                return
            stop_id = self.worker._algo_order_id(position_id, "sl")
            self.worker._verify_protective_stop(stop_id)

        try:
            self._run_test_signal(
                event="EXIT",
                action="CLOSE",
                side=side,
                position_id=position_id,
                reason="EXECUTOR_TEST_CLOSE",
                close_pct=100.0,
            )
        except RuntimeError:
            if self._stop_closed_test_position(position_id, side):
                return
            raise
        final_snapshot = self.worker._account_snapshot(force=True)
        if (
            self.worker._position_for_symbol(final_snapshot) is not None
            or final_snapshot.get("open_algo_orders")
        ):
            raise RuntimeError(
                "EXECUTOR_TEST_MODE: cierre final o cancelación de stops no confirmados"
            )
        self._test_next_side = "SHORT" if side == "LONG" else "LONG"
        LOG.warning(
            "EXECUTOR_TEST_MODE completado para %s %s; siguiente ciclo en %ss",
            side,
            self.worker.settings.symbol,
            TEST_MODE_PAUSE_SECONDS,
        )


def run_test_mode(worker: ExecutionWorker) -> int:
    """Ejecuta validación offline y después los ciclos acotados contra Binance."""
    try:
        from .selftest import run_executor_self_tests

        selftest_count = run_executor_self_tests(
            data_dir=worker.settings.signal_db.parent
        )
    except Exception:
        LOG.exception(
            "Autopruebas offline del executor fallaron; "
            "no se iniciará la secuencia Binance."
        )
        return 1
    LOG.warning(
        "Autopruebas offline del executor superadas (%s grupos); "
        "no enviaron órdenes.",
        selftest_count,
    )
    if worker.settings.production_audit_enabled:
        try:
            from .production_audit import run_production_read_only_audit

            audit = run_production_read_only_audit(worker.settings)
        except Exception:
            LOG.exception(
                "Auditoría production de solo lectura falló; "
                "no se iniciarán órdenes de prueba."
            )
            return 1
        LOG.warning(
            "Auditoría production de solo lectura superada para %s: "
            "One-way, flat, sin órdenes; margen estimado %.2f USDT, "
            "órdenes production enviadas=0. Esto no valida permisos de trading "
            "ni fills production.",
            audit["symbol"],
            audit["estimated_margin_usdt"],
        )
    LOG.warning(
        "EXECUTOR_TEST_MODE activo: BTCUSDT, tope de margen %s USDT "
        "por orden a un máximo de %sx, stop-loss %s%%. "
        "El flujo alterna LONG/SHORT y se detiene antes de repetir "
        "el patrón.",
        TEST_MODE_MAX_MARGIN_USDT,
        TEST_MODE_MAX_LEVERAGE,
        TEST_MODE_STOP_LOSS_PCT * 100,
    )

    controller = ExecutionTestMode(worker)
    completed_sides: set[str] = set()
    while not STOP_EVENT.is_set():
        side = controller.next_side
        if side in completed_sides:
            LOG.warning(
                "EXECUTOR_TEST_MODE: el siguiente ciclo repetiría %s; "
                "modo detenido.",
                side,
            )
            break
        try:
            controller.run_sequence()
        except Exception:
            LOG.exception(
                "EXECUTOR_TEST_MODE detenido; no se inicia otra secuencia"
            )
            return 1
        if STOP_EVENT.is_set():
            break
        completed_sides.add(side)
        if controller.next_side in completed_sides:
            LOG.warning(
                "EXECUTOR_TEST_MODE: ciclo LONG/SHORT completado; "
                "detenido antes de repetir %s.",
                controller.next_side,
            )
            break
        STOP_EVENT.wait(TEST_MODE_PAUSE_SECONDS)
    return 0
