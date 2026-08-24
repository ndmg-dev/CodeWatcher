"""WatcherState: agrega os eventos do watcher e o estado de pausa.

Fonte da verdade: events.jsonl (historico) + control.json (pausa).
Esta classe so mantem um cache em memoria para a janela consultar.
"""

import json
import os
import threading
import time
from datetime import datetime, timedelta

from ..config import EVENTS_FILE, read_control, load_watched_dirs, load_events_summary
from ..git import project_name, is_git_repo
from ..review import rate_limit_status, rate_limit_buckets

DAILY_TREND_DAYS = 14

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
        self.total_cost_usd = summary["total_cost_usd"]
        self.session_count = 0
        self.review_seconds = 0.0
        self.reviewing = False
        self._seq = 0
        # Resumo diario: {"YYYY-MM-DD": {"total": n, "critical": n, "cost_usd": x}}.
        # Chaveado pela data do proprio evento (nao "hoje" fixo no boot), entao
        # a virada de meia-noite se resolve sozinha em snapshot() sem precisar
        # de um timer de fundo.
        self.daily_counts = {}

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
                cost_usd = float(event.get("cost_usd") or 0)
                self.total_cost_usd += cost_usd
                severity = event.get("severity", "baixa")
                day = (event.get("ts") or "")[:10]
                if day:
                    bucket = self.daily_counts.setdefault(day, {"total": 0, "critical": 0, "cost_usd": 0.0})
                    bucket["total"] += 1
                    bucket["cost_usd"] += cost_usd
                    if severity == "alta":
                        bucket["critical"] += 1
                if not historical:
                    self.session_count += 1
                    self.review_seconds += float(event.get("duration") or 0)
                self._resolve(project, event.get("file"), {
                    "status": "done",
                    "source": event.get("source", "file"),
                    "review": event.get("review", ""),
                    "severity": severity,
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
        hourly_trend = rate_limit_buckets()
        today = datetime.now().strftime("%Y-%m-%d")
        with self.lock:
            today_stats = self.daily_counts.get(today, {"total": 0, "critical": 0, "cost_usd": 0.0})
            # Serie real (nao inventada) para o sparkline de "Total historico":
            # contagem de revisoes por dia, do mais antigo pro mais recente,
            # com zero nos dias sem nenhuma revisao.
            daily_trend = [
                self.daily_counts.get(
                    (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d"),
                    {"total": 0},
                )["total"]
                for i in range(DAILY_TREND_DAYS - 1, -1, -1)
            ]
            return {
                "uptime": time.time() - self.started_at,
                "paused": paused,
                "paused_until": control.get("paused_until"),
                "reviewing": self.reviewing,
                "watcher_alive": watcher_alive,
                "session_count": self.session_count,
                "total_count": self.total_count,
                "total_cost_usd": self.total_cost_usd,
                "review_seconds": self.review_seconds,
                "reviews_this_hour": reviews_this_hour,
                "reviews_hour_limit": reviews_hour_limit,
                "hourly_trend": hourly_trend,
                "daily_trend": daily_trend,
                "today_total": today_stats["total"],
                "today_critical": today_stats["critical"],
                "today_cost_usd": today_stats.get("cost_usd", 0.0),
                "llm_provider": control.get("llm_provider", "claude"),
                "openai_api_key": control.get("openai_api_key", ""),
                "openai_model": control.get("openai_model", "gpt-4o"),
                "max_reviews_per_hour": control.get("max_reviews_per_hour"),
                "notify_severity": control.get("notify_severity", "alta"),
                "projects": [
                    {"name": p["name"], "path": p["path"],
                     "exists": p["exists"], "is_git": p["is_git"],
                     "paused": p["name"] in paused_projects,
                     "count": self.per_project.get(p["name"], 0)}
                    for p in projects
                ],
                "feed": list(self.feed),
            }


def tail_events(state, stop_flag, on_live_event=None):
    """Le o historico e depois acompanha o arquivo de eventos em tempo real.

    on_live_event(event), se passado, e chamado so para eventos NOVOS (nao
    para o replay do historico no boot) — usado para disparar a notificacao
    de achado critico sem reabrir notificacoes de coisas que ja aconteceram
    antes desta execucao da GUI."""
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
                            event = json.loads(line)
                        except ValueError:
                            continue
                        state.apply(event, historical=False)
                        if on_live_event:
                            try:
                                on_live_event(event)
                            except Exception:
                                pass
                offset = fh.tell()
        time.sleep(0.4)
