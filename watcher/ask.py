"""Pergunta em linguagem natural sobre o historico acumulado de um projeto
(review-log.md) -- complementar a busca por texto do painel (client-side,
so filtra o que ja esta carregado em memoria). Aqui a pergunta vai pro LLM
configurado, com o log inteiro (ou o mais recente, se grande demais) como
contexto.
"""

import os

from .config import REVIEW_LOG_NAME
from .llm import call_llm

# Mesma logica de MAX_DIFF_CHARS (watcher/config.py): um teto generoso, nao
# um limite fino ajustado por modelo -- so para nao mandar megabytes de log
# antigo quando o projeto ja tem meses de historico.
MAX_HISTORY_CHARS = 60000

PROMPT_TEMPLATE_ASK = """Voce e um assistente que responde perguntas sobre o historico de \
revisoes de codigo de um projeto. Abaixo esta o log de revisoes ja feitas \
(arquivo review-log.md, em ordem cronologica -- as mais recentes ficam por \
ultimo).

Responda em portugues, direto ao ponto, citando arquivo(s) e data(s) quando \
fizer sentido. Se a resposta nao estiver no log, diga isso claramente -- \
nao invente nada que nao esteja no texto abaixo.

--- LOG DE REVISOES ---
{log}
--- FIM DO LOG ---

PERGUNTA: {question}
"""


def ask_history(repo_root, question):
    """Retorna (resposta, custo_usd, erro). 'erro' (str) vem preenchido e os
    outros dois None/0.0 quando nao da pra responder (sem log ainda, etc)."""
    log_path = os.path.join(repo_root, REVIEW_LOG_NAME)
    if not os.path.isfile(log_path):
        return None, 0.0, "Este projeto ainda não tem nenhuma revisão registrada."

    with open(log_path, encoding="utf-8", errors="replace") as fh:
        content = fh.read()
    if not content.strip():
        return None, 0.0, "Este projeto ainda não tem nenhuma revisão registrada."

    if len(content) > MAX_HISTORY_CHARS:
        content = ("... (log truncado, mostrando só a parte mais recente) ...\n"
                   + content[-MAX_HISTORY_CHARS:])

    prompt = PROMPT_TEMPLATE_ASK.format(log=content, question=question)
    answer, cost_usd = call_llm(repo_root, prompt)
    if answer is None:
        return None, 0.0, "O provedor de LLM não respondeu (erro ou timeout). Veja o watcher.log."
    return answer, cost_usd, None
