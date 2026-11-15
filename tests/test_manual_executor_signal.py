import json

from live.bot.storage import SignalStore
from live.bot.scripts.test_execution_signal import _test_entry_signal


def test_manual_executor_signal_is_a_small_valid_v3_entry():
    event = _test_entry_signal("BTCUSDT", "LONG")
    normalized = SignalStore._normalize_event(event)
    payload = json.loads(normalized["payload"])

    assert normalized["schema_version"] == 3
    assert normalized["event"] == "ENTRY"
    assert normalized["action"] == "OPEN"
    assert normalized["signal"] == normalized["side"] == "LONG"
    assert normalized["position_id"] > 0
    assert normalized["signal_time"] >= normalized["bar_time"]
    assert payload["sizing"]["risk_pct"] == 0.0012
    assert payload["sizing"]["stop_loss_pct"] == 0.01
    assert payload["sizing"]["max_leverage"] == 1.0
