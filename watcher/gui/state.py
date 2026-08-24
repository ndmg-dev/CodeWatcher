"""WatcherState: agrega os eventos do watcher e o estado de pausa.

Fonte da verdade: events.jsonl (historico) + control.json (pausa).
Esta classe so mantem um cache em memoria para a janela consultar.
"""

import json
import os
import threading
import time

from ..config import EVENTS_FILE, read_control, load_watched_dirs, load_events_summary
from ..git import project_name, is_git_repo
from ..review import rate_limit_status

FEED_LIMIT = 60


class WatcherState:

    def __init__(self):
        self.lock = threading.Lock()
        self.started_at = time.time()
        self.feed = []
        summary = load_events_summary()
        # Baseline vinda de events.jsonl ja rotacionado (eventos antigos
        # arquivados como contagem em events_summary.json) — sem isso o
        # "Total historico" do painel voltaria a zero a cada rotacao.
        self.total_count = summary["total_count"]
        self.per_project = dict(summary["per_project"])
        self.session_count = 0
        self.review_seconds = 0.0
        self.reviewing = False
        self._seq = 0

    @staticmethod
    def current_projects():
        return [{"name": project_name(p), "path": p,
                 "exists": os.path.isdir(p), "is_git": is_git_repo(p)}
                for p in load_watched_dirs()]

    # -- consumo de eventos --------------------------------------------------

    def apply(self, event, historical):
        etype = event.get("type")
        with self.lock:
            if etype == "review_start":
                self.reviewing = True
                if not historical:
                    self._push({
                        "id": self._next_id(), "status": "running",
                        "project": event.get("project", "?"),
                        "file": event.get("file", "?"),
                        "source": event.get("source", "file"),
                        "ts": event.get("ts", ""), "duration": None,
                        "review": "",
                    })

            elif etype == "review_done":
                self.reviewing = False
                self.total_count += 1
                project = event.get("project", "?")
                self.per_project[project] = self.per_project.get(project, 0) + 1
                if not historical:
                    self.session_count += 1
                    self.review_seconds += float(event.get("duration") or 0)
                self._resolve(project, event.get("file"), {
                    "status": "done",
                    "source": event.get("source", "file"),
                    "review": event.get("review", ""),
                    "duration": event.get("duration"),
                    "ts": event.get("ts", ""),
                })

            elif etype == "review_failed":
                self.reviewing = False
                self._resolve(event.get("project", "?"), event.get("file"), {
                    "status": "failed",
                    "source": event.get("source", "file"),
                    "duration": event.get("duration"),
                    "ts": event.get("ts", ""),
                })

            elif etype in ("started", "stopped"):
                self.reviewing = False

    def _next_id(self):
        self._seq += 1
        return self._seq

    def _push(self, card):
        self.feed.insert(0, card)
        del self.feed[FEED_LIMIT:]

    def _resolve(self, project, file, updates):
        for card in self.feed:
            if (card["status"] == "running"
                    and card["project"] == project and card["file"] == file):
                card.update(updates)
                return
        card = {"id": self._next_id(), "project": project, "file": file,
                "source": "file", "review": "", "duration": None, "ts": ""}
        card.update(updates)
        self._push(card)

    # -- leitura pela janela -------------------------------------------------

    def snapshot(self, watcher_alive):
        control = read_control()
        paused = control["paused"]
        paused_projects = set(control["paused_projects"])
        projects = self.current_projects()
        reviews_this_hour, reviews_hour_limit = rate_limit_status()
        with self.lock:
            return {
                "uptime": time.time() - self.started_at,
                "paused": paused,
                "reviewing": self.reviewing,
                "watcher_alive": watcher_alive,
                "session_count": self.session_count,
                "total_count": self.total_count,
                "review_seconds": self.review_seconds,
                "reviews_this_hour": reviews_this_hour,
                "reviews_hour_limit": reviews_hour_limit,
                "llm_provider": control.get("llm_provider", "claude"),
                "openai_api_key": control.get("openai_api_key", ""),
                "openai_model": control.get("openai_model", "gpt-4o"),
                "projects": [
                    {"name": p["name"], "path": p["path"],
                     "exists": p["exists"], "is_git": p["is_git"],
                     "paused": p["name"] in paused_projects,
                     "count": self.per_project.get(p["name"], 0)}
                    for p in projects
                ],
                "feed": list(self.feed),
            }


def tail_events(state, stop_flag):
    """Le o historico e depois acompanha o arquivo de eventos em tempo real."""
    offset = 0
    if os.path.exists(EVENTS_FILE):
        with open(EVENTS_FILE, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        state.apply(json.loads(line), historical=True)
                    except ValueError:
                        pass
            offset = fh.tell()

    while not stop_flag.is_set():
        try:
            size = os.path.getsize(EVENTS_FILE)
        except OSError:
            time.sleep(0.5)
            continue
        if size < offset:
            offset = 0
        if size > offset:
            with open(EVENTS_FILE, encoding="utf-8", errors="replace") as fh:
                fh.seek(offset)
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            state.apply(json.loads(line), historical=False)
                        except ValueError:
                            pass
                offset = fh.tell()
        time.sleep(0.4)
