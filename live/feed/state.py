"""Estado compartido entre los trabajadores del colector."""

import threading
import time


class FeedState:
    def __init__(self):
        self.stop_event = threading.Event()
        self.db_lock = threading.RLock()
        self.ws_lock = threading.RLock()
        self.status_lock = threading.Lock()
        self.ws_connected = threading.Event()
        self.backfill_done = threading.Event()
        self.active_ws = None

        self.last_ws_message_monotonic = 0.0
        self.last_ws_message_utc = None
        self.ws_opened_monotonic = 0.0
        self.ws_last_error_utc = None
        self.ws_last_error = None
        self.ws_reconnect_attempts = 0
        self.ws_last_reconnect_utc = None
        self.ws_last_reconnect_delay_seconds = None
        self.process_started_monotonic = time.monotonic()

        self.runtime_state = {
            "collector_status": "starting",
            "websocket_status": "starting",
            "backfill_status": "pending",
        }
        self.backfill_diagnostics = {
            "status": "pending",
            "started_at": None,
            "completed_at": None,
            "candles_saved": 0,
            "candles_expected": None,
            "pages_completed": 0,
            "pages_expected": None,
            "progress_percent": 0.0,
            "candles_per_second": 0.0,
            "eta_seconds": None,
            "last_candle_at": None,
            "last_error": None,
            "_running_since_monotonic": None,
        }
        self.repair_diagnostics = {
            "status": "starting",
            "last_started_at": None,
            "last_completed_at": None,
            "last_error": None,
            "last_gaps_found": 0,
            "_running_since_monotonic": None,
        }
        self.database_diagnostics = {
            "available": False,
            "checked_at": None,
            "check_duration_seconds": None,
            "last_error": "aún no se ha ejecutado el diagnóstico",
            "latest_candle": None,
            "persisted_state": {},
            "_checked_monotonic": None,
        }
        self.threads = {
            "backfill": None,
            "repair": None,
            "database_diagnostics": None,
            "status_server": None,
            "watchdog": None,
        }

    def set_runtime_state(self, key, value):
        with self.status_lock:
            self.runtime_state[key] = str(value)

    def update_diagnostics(self, section, **values):
        sections = {
            "backfill": self.backfill_diagnostics,
            "repair": self.repair_diagnostics,
            "database": self.database_diagnostics,
        }
        with self.status_lock:
            sections[section].update(values)


STATE = FeedState()


def update_diagnostics(section, **values):
    STATE.update_diagnostics(section, **values)
