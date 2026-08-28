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
from .backlog_store import load_backlog_status

DAILY_TREND_DAYS = 14

FEED_LIMIT = 60
# Itens do backlog nao devem sumir so porque saiu do teto do feed (ver
# FEED_LIMIT) -- um item pendente precisa continuar visivel ate o usuario
# resolver/dispensar. Teto bem mais alto, mesma logica de nao guardar o
# markdown inteiro (so um resumo curto) para nao pesar a ponte JS↔Python.
BACKLOG_LIMIT = 300
BACKLOG_EXCERPT_CHARS = 220
# O painel faz polling de get_state() a cada poucos segundos (ver ui.html);
# mandar o markdown inteiro de ate 60 revisoes em cada chamada pela ponte
# COM do pywebview (WinForms) e caro e, empiricamente, parece contribuir
# para vazamento de memoria real no processo (visto crescer a varios GB em
# poucos minutos). O texto completo sempre continua disponivel no
# review-log.md do projeto — o painel so precisa mostrar um preview.
MAX_FEED_REVIEW_CHARS = 2000


class WatcherState:

    def __init__(self):
        self.lock = threading.Lock()
        self.started_at = time.time()
        self.feed = []
        # Fonte separada do feed -- ver BACKLOG_LIMIT. Mais recente primeiro,
        # sem o markdown completo (so um resumo curto por item).
        self.backlog_items = []
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
        # Agregados ricos pra aba Analise (ver analytics_snapshot). Preenchidos
        # em apply() a partir de todo o events.jsonl no boot -> cobrem o
        # historico inteiro que ainda esta no arquivo (nao so o teto do feed).
        self.stats = {
            "by_day": {},      # "YYYY-MM-DD" -> {alta,media,baixa,nenhuma,cost,dur_sum,dur_n}
            "by_project": {},  # nome -> {reviews,alta,media,baixa,cost,dur_sum,dur_n}
            "by_file": {},     # "proj\x00file" -> {project,file,count,alta,media,last_ts}
            "by_source": {"pr": 0, "commit": 0, "file": 0},
        }

    def _stat_bump(self, project, file, ts, severity, cost_usd, duration, source):
        st = self.stats
        sev = severity if severity in ("alta", "media", "baixa") else "nenhuma"
        day = (ts or "")[:10]
        if day:
            d = st["by_day"].setdefault(
                day, {"alta": 0, "media": 0, "baixa": 0, "nenhuma": 0,
                      "cost": 0.0, "dur_sum": 0.0, "dur_n": 0})
            d[sev] += 1
            d["cost"] += cost_usd
            if duration:
                d["dur_sum"] += duration
                d["dur_n"] += 1
        p = st["by_project"].setdefault(
            project, {"reviews": 0, "alta": 0, "media": 0, "baixa": 0,
                      "cost": 0.0, "dur_sum": 0.0, "dur_n": 0})
        p["reviews"] += 1
        if sev != "nenhuma":
            p[sev] += 1
        p["cost"] += cost_usd
        if duration:
            p["dur_sum"] += duration
            p["dur_n"] += 1
        if source in st["by_source"]:
            st["by_source"][source] += 1
        fk = f"{project}\x00{file}"
        f = st["by_file"].setdefault(
            fk, {"project": project, "file": file, "count": 0,
                 "alta": 0, "media": 0, "last_ts": ""})
        f["count"] += 1
        if sev in ("alta", "media"):
            f[sev] += 1
        if ts:
            f["last_ts"] = max(f["last_ts"], ts)

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
                self._stat_bump(
                    project, event.get("file", "?"), event.get("ts", ""),
                    severity, cost_usd, float(event.get("duration") or 0),
                    event.get("source", "file"),
                )
                self._resolve(project, event.get("file"), {
                    "status": "done",
                    "source": event.get("source", "file"),
                    "review": event.get("review", ""),
                    "severity": severity,
                    "duration": event.get("duration"),
                    "ts": event.get("ts", ""),
                })
                if severity in ("alta", "media"):
                    self._push_backlog(
                        project, event.get("file", "?"), event.get("ts", ""),
                        source=event.get("source", "file"), severity=severity,
                        excerpt=event.get("review", ""), kind=None,
                    )

            elif etype == "secret_found":
                # Alerta instantaneo do scan local (watcher/secrets.py) — nao
                # tem "running" antes (nao passa pela fila do LLM), entao so
                # empilha um card novo direto, sem resolver nada pendente.
                project = event.get("project", "?")
                day = (event.get("ts") or "")[:10]
                if day:
                    bucket = self.daily_counts.setdefault(day, {"total": 0, "critical": 0, "cost_usd": 0.0})
                    bucket["critical"] += 1
                self._push({
                    "id": self._next_id(), "status": "secret",
                    "project": project, "file": event.get("file", "?"),
                    "source": event.get("source", "file"),
                    "ts": event.get("ts", ""), "duration": None,
                    "severity": "alta", "findings": event.get("findings", []),
                    "review": "",
                })
                self._stat_bump(
                    project, event.get("file", "?"), event.get("ts", ""),
                    "alta", 0.0, 0.0, event.get("source", "file"),
                )
                kinds = ", ".join(sorted({f.get("kind", "?") for f in event.get("findings", [])}))
                self._push_backlog(
                    project, event.get("file", "?"), event.get("ts", ""),
                    source=event.get("source", "file"), severity="alta",
                    excerpt=f"🔑 Possível segredo exposto: {kinds}", kind="secret",
                )

            elif etype == "history_query":
                # "Pergunte ao histórico" (watcher/ask.py) usa o mesmo LLM
                # configurado -- o custo real (OpenAI) conta pro mesmo total,
                # senao o painel subestimaria o gasto. Nao mexe em
                # total_count/per_project (isso e contagem de revisoes, nao
                # de perguntas) nem entra no feed/backlog.
                cost_usd = float(event.get("cost_usd") or 0)
                self.total_cost_usd += cost_usd
                day = (event.get("ts") or "")[:10]
                if day and cost_usd:
                    bucket = self.daily_counts.setdefault(day, {"total": 0, "critical": 0, "cost_usd": 0.0})
                    bucket["cost_usd"] += cost_usd

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

    def _push_backlog(self, project, file, ts, source, severity, excerpt, kind):
        """id determinístico (nao o contador de self._seq, que so faz
        sentido dentro de uma execucao) -- assim o mesmo item recebe o mesmo
        id no replay do historico e ao vivo, e o status gravado em
        backlog_status.json (chaveado por esse id) casa certo depois de um
        restart da GUI."""
        item_id = f"{project}|{file}|{ts}"
        excerpt = (excerpt or "").strip()
        if len(excerpt) > BACKLOG_EXCERPT_CHARS:
            excerpt = excerpt[:BACKLOG_EXCERPT_CHARS] + "…"
        self.backlog_items.insert(0, {
            "id": item_id, "project": project, "file": file, "ts": ts,
            "source": source, "severity": severity, "kind": kind,
            "excerpt": excerpt,
        })
        del self.backlog_items[BACKLOG_LIMIT:]

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
        backlog_status = load_backlog_status()
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
                # Card do feed carrega a mesma resolucao do item de backlog
                # correspondente (mesmo id "projeto|arquivo|ts") -- nao some
                # do feed, so marca visualmente como resolvido/dispensado,
                # pra manter os dois lugares consistentes sem esconder
                # historico do feed principal.
                "feed": [
                    dict(self._feed_card_for_output(c),
                         resolution=backlog_status.get(
                             f"{c.get('project')}|{c.get('file')}|{c.get('ts')}", {}
                         ).get("status", "open"))
                    for c in self.feed
                ],
                "backlog": [
                    dict(item, status=backlog_status.get(item["id"], {}).get("status", "open"),
                         resolved_at=backlog_status.get(item["id"], {}).get("at"))
                    for item in self.backlog_items
                ],
            }

    def analytics_snapshot(self, days=30):
        with self.lock:
            st = self.stats
            series = []
            for i in range(days - 1, -1, -1):
                day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                b = st["by_day"].get(day, {})
                series.append({
                    "day": day,
                    "alta": b.get("alta", 0), "media": b.get("media", 0),
                    "baixa": b.get("baixa", 0), "nenhuma": b.get("nenhuma", 0),
                    "cost": round(b.get("cost", 0.0), 4),
                    "avg_dur": round(b["dur_sum"] / b["dur_n"], 1) if b.get("dur_n") else 0,
                })

            def _proj(k, v):
                return {
                    "project": k, "reviews": v["reviews"],
                    "alta": v["alta"], "media": v["media"], "baixa": v["baixa"],
                    "cost": round(v["cost"], 4),
                    "avg_dur": round(v["dur_sum"] / v["dur_n"], 1) if v["dur_n"] else 0,
                }
            by_project = sorted(
                (_proj(k, v) for k, v in st["by_project"].items()),
                key=lambda x: -x["reviews"],
            )
            hotspots = sorted(
                st["by_file"].values(),
                key=lambda x: (-x["count"], -x["alta"], -x["media"]),
            )[:12]
            dur_n = sum(v["dur_n"] for v in st["by_project"].values())
            dur_s = sum(v["dur_sum"] for v in st["by_project"].values())
            return {
                "series": series,
                "by_project": by_project,
                "hotspots": [dict(h) for h in hotspots],
                "by_source": dict(st["by_source"]),
                "totals": {
                    "reviews": self.total_count,
                    "cost_usd": round(self.total_cost_usd, 4),
                    "avg_dur": round(dur_s / dur_n, 1) if dur_n else 0,
                    "flagged": sum(v["alta"] + v["media"] for v in st["by_project"].values()),
                    "window_days": days,
                },
            }

    @staticmethod
    def _feed_card_for_output(card):
        """Copia rasa do card com o texto da revisao truncado — ver
        MAX_FEED_REVIEW_CHARS. self.feed em si fica intacto."""
        review = card.get("review") or ""
        if len(review) <= MAX_FEED_REVIEW_CHARS:
            return card
        out = dict(card)
        out["review"] = review[:MAX_FEED_REVIEW_CHARS] + "\n\n... (veja o review-log.md do projeto para o texto completo)"
        return out


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
