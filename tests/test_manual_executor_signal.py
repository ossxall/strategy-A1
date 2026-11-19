import json

from live.bot.scripts.test_execution_signal import (
    _test_entry_signal,
    _test_exit_signal,
)
from live.bot.storage import SignalStore


def test_manual_executor_entry_and_exit_are_valid_v4_signals():
    sizing = {
        "risk_pct": 0.01,
        "stop_loss_pct": 0.01,
        "commission_bps": 0.0,
        "slippage_bps": 0.0,
        "move_stop_to_breakeven": True,
    }
    event = _test_entry_signal("BTCUSDT", "LONG", sizing)
    normalized = SignalStore._normalize_event(event)
    payload = json.loads(normalized["payload"])

    assert normalized["schema_version"] == 4
    assert normalized["event"] == "ENTRY"
    assert normalized["action"] == "OPEN"
    assert normalized["signal"] == normalized["side"] == "LONG"
    assert normalized["position_id"] > 0
    assert normalized["signal_time"] >= normalized["bar_time"]
    assert payload["sizing"] == sizing
    assert payload["reason_code"] == "ENTRY_OTHER"

    close_event = _test_exit_signal("BTCUSDT", "LONG", event["position_id"], attempt=1)
    close_normalized = SignalStore._normalize_event(close_event)
    close_payload = json.loads(close_normalized["payload"])
    assert close_normalized["event"] == "EXIT"
    assert close_normalized["action"] == "CLOSE"
    assert close_normalized["position_id"] == event["position_id"]
    assert close_payload["close_pct"] == 100.0
    assert close_payload["reason_code"] == "EXIT_OTHER"
    assert close_payload["stop_action"] == "CANCEL"
