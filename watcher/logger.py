import json
import os
import sys
import threading
from datetime import datetime

from .config import STATE_DIR, WATCHER_LOG_FILE, EVENTS_FILE

_log_lock = threading.Lock()
_emit_lock = threading.Lock()

def log(msg):
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    if sys.stdout is not None:
        try:
            print(line, flush=True)
        except Exception:
            pass
    try:
        with _log_lock:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(WATCHER_LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError:
        pass

def emit_event(event_type, **fields):
    event = {"ts": datetime.now().isoformat(timespec="seconds"),
             "type": event_type}
    event.update(fields)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with _emit_lock, open(EVENTS_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass
