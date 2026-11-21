import json

import pytest

from live.bot.signal_contract import (
    STOP_ACTION_CANCEL,
    STOP_ACTION_KEEP,
    STOP_ACTION_MOVE_TO_BREAKEVEN,
    signal_stop_action,
)
from live.bot.storage import SignalStore


def _exit_event(reason, *, action="PARTIAL_CLOSE", sizing=None, payload=None):
    body = {
        "action": action,
        "position_id": 7,
        "close_pct": 50.0 if action == "PARTIAL_CLOSE" else 100.0,
    }
    if sizing is not None:
        body["sizing"] = sizing
    if payload:
        body.update(payload)
    return {
        "schema_version": 4,
        "symbol": "BTCUSDT",
        "interval": "1m",
        "timeframe": "30min",
        "bar_time": 1_000_000,
        "signal_time": 1_000_000,
        "event": "EXIT",
        "action": action,
        "signal": "LONG",
        "side": "LONG",
        "position_id": 7,
        "scheduled": False,
        "reason": reason,
        "created_at": "2026-01-01T00:00:00+00:00",
        "payload": body,
    }


@pytest.mark.parametrize(
    ("reason", "action", "expected_code", "expected_stop"),
    [
        ("PARTIAL_TP_1R", "PARTIAL_CLOSE", "TAKE_PROFIT_1", "MOVE_TO_BREAKEVEN"),
        ("PARTIAL_TP2_1.5R", "PARTIAL_CLOSE", "TAKE_PROFIT_2", "KEEP"),
        ("PARTIAL_TP3_2R", "PARTIAL_CLOSE", "TAKE_PROFIT_3", "KEEP"),
        ("PARTIAL_TP4_2.5R", "PARTIAL_CLOSE", "TAKE_PROFIT_4", "KEEP"),
        ("PARTIAL_TP5_3R", "PARTIAL_CLOSE", "TAKE_PROFIT_5", "KEEP"),
        ("PARTIAL_TP6_3.5R", "PARTIAL_CLOSE", "TAKE_PROFIT_6", "KEEP"),
        ("PARTIAL_TP7_4R", "PARTIAL_CLOSE", "TAKE_PROFIT_7", "KEEP"),
        ("PARTIAL_TP8_6R", "PARTIAL_CLOSE", "TAKE_PROFIT_8", "KEEP"),
        ("PARTIAL_TP9_7R", "PARTIAL_CLOSE", "TAKE_PROFIT_9", "KEEP"),
        ("PARTIAL_TP10_8R", "PARTIAL_CLOSE", "TAKE_PROFIT_10", "KEEP"),
        ("PARTIAL_TP11_9R", "PARTIAL_CLOSE", "TAKE_PROFIT_11", "KEEP"),
        ("PARTIAL_TP20_18R", "PARTIAL_CLOSE", "TAKE_PROFIT_20", "KEEP"),
        ("ADX_1D_REVERSAL_50%", "PARTIAL_CLOSE", "ADX_REVERSAL", "KEEP"),
        ("RSI_DIVERGENCE_BEAR_50%", "PARTIAL_CLOSE", "RSI_DIVERGENCE_BEAR", "KEEP"),
        ("RSI_DIVERGENCE_BULL", "CLOSE", "RSI_DIVERGENCE_BULL", "CANCEL"),
        ("GAUSSIAN_TREND_BEAR_25%", "PARTIAL_CLOSE", "GAUSSIAN_TREND_BEAR", "KEEP"),
        ("GAUSSIAN_TREND_BULL", "CLOSE", "GAUSSIAN_TREND_BULL", "CANCEL"),
        ("STOP_LOSS_1.00%", "CLOSE", "STOP_LOSS", "CANCEL"),
        ("BREAKEVEN", "CLOSE", "BREAKEVEN", "CANCEL"),
        ("EMA_CROSS_EXIT", "CLOSE", "EMA_CROSS_EXIT", "CANCEL"),
        (
            "EMA_SLOW_TOUCH_SHORT_AFTER_TP3",
            "CLOSE",
            "EMA_SLOW_TOUCH",
            "CANCEL",
        ),
        ("MONTHLY_LOSS_LIMIT_5%", "CLOSE", "MONTHLY_LOSS_LIMIT", "CANCEL"),
        ("EXECUTOR_TEST_PARTIAL", "PARTIAL_CLOSE", "TEST_PARTIAL", "KEEP"),
    ],
)
def test_exit_signal_contract_assigns_stable_reason_and_stop_policy(
    reason, action, expected_code, expected_stop
):
    normalized = SignalStore._normalize_event(
        _exit_event(
            reason,
            action=action,
            sizing={"move_stop_to_breakeven": True},
        )
    )
    payload = json.loads(normalized["payload"])

    assert normalized["schema_version"] == 4
    assert payload["reason_code"] == expected_code
    assert payload["stop_action"] == expected_stop


@pytest.mark.parametrize("level", range(1, 21))
def test_every_partial_tp_level_maps_to_its_stable_reason_code(level):
    reason = "PARTIAL_TP_0.5R" if level == 1 else f"PARTIAL_TP{level}_1R"
    normalized = SignalStore._normalize_event(
        _exit_event(
            reason,
            sizing={"move_stop_to_breakeven": True},
        )
    )
    payload = json.loads(normalized["payload"])

    assert payload["reason_code"] == f"TAKE_PROFIT_{level}"
    assert payload["stop_action"] == (
        STOP_ACTION_MOVE_TO_BREAKEVEN if level == 1 else STOP_ACTION_KEEP
    )


def test_tp1_full_close_cancels_stop_instead_of_requesting_breakeven():
    normalized = SignalStore._normalize_event(
        _exit_event(
            "PARTIAL_TP_1R",
            action="CLOSE",
            sizing={"move_stop_to_breakeven": True},
        )
    )

    assert json.loads(normalized["payload"])["stop_action"] == STOP_ACTION_CANCEL


def test_non_tp1_partial_keeps_stop_even_when_breakeven_is_enabled():
    normalized = SignalStore._normalize_event(
        _exit_event(
            "PARTIAL_TP2_1.5R",
            sizing={"move_stop_to_breakeven": True},
        )
    )

    assert json.loads(normalized["payload"])["stop_action"] == STOP_ACTION_KEEP


def test_v3_partial_tp1_policy_is_derived_for_backwards_compatibility():
    legacy = {
        "schema_version": 3,
        "event": "EXIT",
        "action": "PARTIAL_CLOSE",
        "reason": "PARTIAL_TP_1.25R",
        "payload": {
            "sizing": {"move_stop_to_breakeven": True},
        },
    }

    assert signal_stop_action(legacy) == STOP_ACTION_MOVE_TO_BREAKEVEN


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        ({"reason_code": "TAKE_PROFIT_2"}, "reason_code"),
        ({"stop_action": "KEEP"}, "stop_action"),
    ],
)
def test_signal_contract_rejects_conflicting_execution_metadata(metadata, message):
    with pytest.raises(ValueError, match=message):
        SignalStore._normalize_event(
            _exit_event(
                "PARTIAL_TP_1R",
                sizing={"move_stop_to_breakeven": True},
                payload=metadata,
            )
        )
