"""Resumo de um periodo (hoje/ontem/ultimos 7 dias) em todos os projetos --
util tanto como resumo diario quanto como material pronto pra um standup
("o que eu fiz ontem"). Fonte: events.jsonl direto, nao o backlog (que so
guarda alta/media/segredo) -- aqui interessa TUDO que foi revisado no
periodo, inclusive baixa, pra dar o quadro completo do que mudou.
"""

import json
import os

from .config import EVENTS_FILE, STATE_DIR
from .llm import call_llm

MAX_EXCERPT_CHARS = 200

PROMPT_TEMPLATE_SUMMARY = """Você recebe uma lista de revisões de código feitas em {periodo}, em \
vários projetos diferentes. Gere um resumo em português, curto e direto, \
adequado tanto pra registro do dia quanto pra colar num standup ("o que eu \
fiz {periodo}"). Cubra:

1. O que mudou, agrupado por projeto.
2. O que foi crítico ou precisa de atenção (se houver).

Seja objetivo -- bullet points, sem enrolação, sem repetir o texto completo \
de cada revisão.

--- REVISÕES DO PERÍODO ---
{items}
--- FIM ---
"""

# Mesmo raciocinio do patterns.py: um resumo cruzando varios projetos nao
# pertence a nenhum deles como cwd do Claude Code CLI.
_NEUTRAL_CWD = STATE_DIR


def _read_events_for_range(start_date, end_date):
    """Le events.jsonl (nao o self.feed em memoria, que e capado em 60 --
    um dia cheio facilmente passa disso). start/end: 'YYYY-MM-DD', inclusive."""
    results = []
    if not os.path.isfile(EVENTS_FILE):
        return results
    with open(EVENTS_FILE, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") != "review_done":
                continue
            day = (event.get("ts") or "")[:10]
            if not (start_date <= day <= end_date):
                continue
            results.append(event)
    return results


def generate_summary(period_label, start_date, end_date):
    """Retorna (resposta, custo_usd, erro)."""
    events = _read_events_for_range(start_date, end_date)
    if not events:
        return None, 0.0, f"Nenhuma revisão registrada em {period_label}."

    lines = []
    for e in events:
        review = (e.get("review") or "").strip().replace("\n", " ")
        if len(review) > MAX_EXCERPT_CHARS:
            review = review[:MAX_EXCERPT_CHARS] + "…"
        project = e.get("project", "?")
        file_ = e.get("file", "?")
        severity = e.get("severity", "baixa")
        lines.append(f"- [{project}] {file_} (severidade {severity}): {review or 'sem observações'}")

    prompt = PROMPT_TEMPLATE_SUMMARY.format(periodo=period_label, items="\n".join(lines))
    answer, cost_usd = call_llm(_NEUTRAL_CWD, prompt)
    if answer is None:
        return None, 0.0, "O provedor de LLM não respondeu (erro ou timeout). Veja o watcher.log."
    return answer, cost_usd, None
