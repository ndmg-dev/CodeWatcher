"""Persiste so o status (resolvido/dispensado) dos itens do backlog.

Os itens em si (projeto, arquivo, severidade, resumo) sao derivados dos
mesmos eventos que alimentam o feed (ver WatcherState em state.py) -- este
arquivo guarda so a decisao do usuario sobre cada `item_id`, para
sobreviver a reinicios da GUI. Mesmo padrao de leitura/escrita atomica do
control.json (ver watcher/config.py).
"""

import json
import os

from ..config import STATE_DIR, BACKLOG_STATUS_FILE


def load_backlog_status():
    """{item_id: {"status": "done"|"dismissed", "at": iso}}. Arquivo
    ausente/corrompido volta como vazio -- nunca trava o painel por isso."""
    try:
        with open(BACKLOG_STATUS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = BACKLOG_STATUS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, BACKLOG_STATUS_FILE)


def set_backlog_status(item_id, status, at):
    """status: 'done' | 'dismissed'. 'at' e um datetime.isoformat() ja
    formatado pelo chamador, para nao acoplar este modulo a `datetime`."""
    data = load_backlog_status()
    data[item_id] = {"status": status, "at": at}
    _write(data)


def reopen_backlog_item(item_id):
    """Remove a marcacao -- o item volta a aparecer como pendente."""
    data = load_backlog_status()
    if item_id in data:
        del data[item_id]
        _write(data)
