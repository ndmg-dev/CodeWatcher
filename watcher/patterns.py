"""Detecta achados que se repetem em mais de um projeto monitorado --
reaproveita os mesmos itens do backlog (watcher/gui/state.py) em vez de
inventar uma segunda fonte de dados.
"""

from .config import STATE_DIR
from .llm import call_llm

PROMPT_TEMPLATE_PATTERNS = """Você recebe uma lista de achados de revisão de código, ainda \
pendentes de resolução, de VÁRIOS projetos diferentes (não um só). Cada \
linha tem: projeto, arquivo, severidade e um resumo curto do problema.

Sua tarefa: identificar problemas que SE REPETEM em mais de um projeto -- \
o mesmo tipo de falha (ex: senha em texto puro, token salvo sem segurança, \
falta de tratamento de erro, N+1 query, endpoint sem autenticação) \
aparecendo em arquivos/projetos diferentes. Para cada padrão encontrado, \
liste em quais projetos/arquivos ele aparece.

Se nada se repetir de forma clara entre projetos diferentes, diga isso \
objetivamente -- não force um padrão que não existe.

--- ACHADOS PENDENTES ---
{items}
--- FIM ---
"""

# Sem repo de projeto nenhum faz sentido como cwd aqui (a analise cruza
# varios projetos, nao pertence a um so) -- STATE_DIR (%LOCALAPPDATA%\
# CodeWatcher) e sempre uma pasta neutra, sem codigo/CLAUDE.md que
# poderiam vazar contexto irrelevante pro Claude Code CLI.
_NEUTRAL_CWD = STATE_DIR


def detect_patterns(items):
    """items: lista de dicts com project/file/severity/excerpt (mesmo shape
    do backlog exposto pelo painel). Retorna (resposta, custo_usd, erro)."""
    if len(items) < 2:
        return None, 0.0, "Poucos itens pendentes no backlog ainda para detectar padrões (precisa de pelo menos 2)."

    lines = [
        f"- [{it['project']}] {it['file']} (severidade {it['severity']}): {it['excerpt']}"
        for it in items
    ]
    prompt = PROMPT_TEMPLATE_PATTERNS.format(items="\n".join(lines))
    answer, cost_usd = call_llm(_NEUTRAL_CWD, prompt)
    if answer is None:
        return None, 0.0, "O provedor de LLM não respondeu (erro ou timeout). Veja o watcher.log."
    return answer, cost_usd, None
