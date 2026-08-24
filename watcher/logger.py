import json
import os
import sys
import threading
from datetime import datetime

from .config import (
    STATE_DIR, WATCHER_LOG_FILE, EVENTS_FILE,
    EVENTS_MAX_BYTES, EVENTS_KEEP_LINES,
    load_events_summary, save_events_summary,
)

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

def _rotate_events_if_needed():
    """Se events.jsonl passou de EVENTS_MAX_BYTES, arquiva a contagem dos
    eventos mais antigos em events_summary.json e mantem so os ultimos
    EVENTS_KEEP_LINES no arquivo. Chamado antes de cada append (barato: so
    faz um os.path.getsize na maioria das vezes). Deve ser chamado com
    _emit_lock ja adquirido pelo chamador."""
    try:
        if os.path.getsize(EVENTS_FILE) <= EVENTS_MAX_BYTES:
            return
    except OSError:
        return

    try:
        with open(EVENTS_FILE, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return
    if len(lines) <= EVENTS_KEEP_LINES:
        return

    archived, kept = lines[:-EVENTS_KEEP_LINES], lines[-EVENTS_KEEP_LINES:]
    summary = load_events_summary()
    for raw in archived:
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if event.get("type") == "review_done":
            summary["total_count"] = summary.get("total_count", 0) + 1
            project = event.get("project", "?")
            summary["per_project"][project] = summary["per_project"].get(project, 0) + 1
    save_events_summary(summary)

    tmp = EVENTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.writelines(kept)
    os.replace(tmp, EVENTS_FILE)
    print(f"[{datetime.now():%H:%M:%S}]   = events.jsonl rotacionado: "
          f"{len(archived)} eventos arquivados em events_summary.json")

def emit_event(event_type, **fields):
    event = {"ts": datetime.now().isoformat(timespec="seconds"),
             "type": event_type}
    event.update(fields)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with _emit_lock:
            _rotate_events_if_needed()
            with open(EVENTS_FILE, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass
