"""Contrato semántico compartido por el productor y el executor de señales."""

from __future__ import annotations

from typing import Any

STOP_ACTION_CANCEL = "CANCEL"
STOP_ACTION_KEEP = "KEEP"
STOP_ACTION_MOVE_TO_BREAKEVEN = "MOVE_TO_BREAKEVEN"
STOP_ACTIONS = {
    STOP_ACTION_CANCEL,
    STOP_ACTION_KEEP,
    STOP_ACTION_MOVE_TO_BREAKEVEN,
}


def reason_code_for(event: str, reason: str) -> str:
    """Convierte los motivos descriptivos de estrategia en códigos estables."""
    event = str(event).upper()
    reason = str(reason).strip().upper()

    if event == "ENTRY":
        return "ENTRY_CROSS" if reason == "EMA_CROSS" else "ENTRY_OTHER"

    if reason.startswith("PARTIAL_TP_"):
        return "TAKE_PROFIT_1"
    for level in range(2, 21):
        if reason.startswith(f"PARTIAL_TP{level}_"):
            return f"TAKE_PROFIT_{level}"
    if reason.startswith("STOP_LOSS_"):
        return "STOP_LOSS"
    if reason == "BREAKEVEN":
        return "BREAKEVEN"
    if reason == "EMA_CROSS_EXIT":
        return "EMA_CROSS_EXIT"
    if reason.startswith("ADX_"):
        return "ADX_REVERSAL"
    if reason.startswith("RSI_DIVERGENCE_BEAR"):
        return "RSI_DIVERGENCE_BEAR"
    if reason.startswith("RSI_DIVERGENCE_BULL"):
        return "RSI_DIVERGENCE_BULL"
    if reason.startswith("GAUSSIAN_TREND_BEAR"):
        return "GAUSSIAN_TREND_BEAR"
    if reason.startswith("GAUSSIAN_TREND_BULL"):
        return "GAUSSIAN_TREND_BULL"
    if reason.startswith("EMA_SLOW_TOUCH_"):
        return "EMA_SLOW_TOUCH"
    if reason.startswith("MONTHLY_LOSS_LIMIT"):
        return "MONTHLY_LOSS_LIMIT"
    if reason == "EXECUTOR_TEST_PARTIAL":
        return "TEST_PARTIAL"
    if reason.startswith("EXECUTOR_TEST_CLOSE"):
        return "TEST_CLOSE"
    return "EXIT_OTHER"


def stop_action_for(
    event: str,
    action: str,
    reason_code: str,
    payload: dict[str, Any],
) -> str:
    """Describe cómo debe quedar el stop después de procesar una salida."""
    if str(event).upper() != "EXIT":
        raise ValueError("stop_action solo aplica a señales EXIT")
    action = str(action).upper()
    if action == "CLOSE":
        return STOP_ACTION_CANCEL
    if action != "PARTIAL_CLOSE":
        raise ValueError(f"action EXIT no soportada: {action}")

    sizing = payload.get("sizing")
    if (
        reason_code == "TAKE_PROFIT_1"
        and isinstance(sizing, dict)
        and sizing.get("move_stop_to_breakeven") is True
    ):
        return STOP_ACTION_MOVE_TO_BREAKEVEN
    return STOP_ACTION_KEEP


def apply_signal_policy(
    event: str,
    action: str,
    reason: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Añade y valida metadatos de ejecución explícitos en el payload."""
    event = str(event).upper()
    action = str(action).upper()
    result = dict(payload)
    reason_code = reason_code_for(event, reason)

    supplied_reason_code = result.get("reason_code")
    if supplied_reason_code is not None and supplied_reason_code != reason_code:
        raise ValueError(
            "payload.reason_code no coincide con event/reason "
            f"({supplied_reason_code!r} != {reason_code!r})"
        )
    result["reason_code"] = reason_code

    if event == "EXIT":
        stop_action = stop_action_for(event, action, reason_code, result)
        supplied_stop_action = result.get("stop_action")
        if supplied_stop_action is not None and supplied_stop_action != stop_action:
            raise ValueError(
                "payload.stop_action no coincide con la política "
                f"({supplied_stop_action!r} != {stop_action!r})"
            )
        result["stop_action"] = stop_action
    elif "stop_action" in result:
        raise ValueError("payload.stop_action no es válido para ENTRY")

    return result


def signal_reason_code(signal: dict[str, Any]) -> str:
    """Lee el código estructurado o deriva el de una señal legacy v3."""
    payload = signal.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    code = payload.get("reason_code")
    if isinstance(code, str) and code:
        return code
    return reason_code_for(signal.get("event", ""), signal.get("reason", ""))


def signal_stop_action(signal: dict[str, Any]) -> str:
    """Lee la acción explícita o deriva la política de una señal legacy v3."""
    payload = signal.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    action = payload.get("stop_action")
    if action in STOP_ACTIONS:
        return action
    return stop_action_for(
        signal.get("event", ""),
        signal.get("action", ""),
        signal_reason_code(signal),
        payload,
    )
