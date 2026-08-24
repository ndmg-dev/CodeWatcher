# Code Watcher

App de bandeja para Windows que observa seus repositórios git em background
e manda automaticamente para o **Claude Code CLI** revisar o que mudou —
foco em bugs e melhorias — sem precisar pedir manualmente. A revisão vai
para um `review-log.md` na raiz de cada projeto e aparece em tempo real
num painel visual.

## Três fontes de revisão

1. **Arquivo salvo com mudança não commitada** — espera ~3s de silêncio
   (debounce), roda `git diff HEAD` e manda para o Claude revisar.
2. **Commit novo** — git escreve o hash em `.git/refs/heads/<branch>` a
   cada commit; o watcher já observa esse arquivo, sem precisar de nada
   além do git local.
3. **PR aberto/atualizado no GitHub** — uma thread separada consulta
   `gh pr list` a cada 5 minutos por repositório com remote do GitHub.
   **Somente leitura**: a revisão nunca é postada de volta no GitHub, só
   fica no `review-log.md` e no painel.

## Requisitos

- Windows (usa APIs do Windows para bandeja/janela sem console).
- [Claude Code CLI](https://docs.claude.com/claude-code) instalado e no
  PATH (`claude`).
- `git` no PATH.
- Opcional, só para a fonte de PR: [GitHub CLI](https://cli.github.com/)
  (`gh`) instalado e autenticado (`gh auth login`). Sem isso, as outras
  duas fontes continuam funcionando normalmente.

## Uso

**Direto com Python** (dev):

```powershell
pip install -r requirements.txt   # watchdog, pystray, pywebview, Pillow
pythonw watcher_gui.py            # sobe minimizado na bandeja
pythonw watcher_gui.py --show     # sobe e já abre o painel
```

**Como executável** (recomendado para o dia a dia):

```powershell
.\build_exe.ps1
```

Gera `dist\CodeWatcher.exe` — standalone, não precisa de Python instalado
na máquina que for rodar. Rode o script de novo depois de qualquer mudança
em `code_watcher.py`, `watcher_gui.py` ou `ui.html` para atualizar o exe.

Pelo painel (ou editando `%LOCALAPPDATA%\CodeWatcher\projects.json`
direto) você adiciona as pastas que quer monitorar — o botão "Buscar
repositórios" varre uma pasta raiz e lista todos os repositórios git
encontrados dentro dela de uma vez.

## Arquitetura, em uma imagem

```
watcher_gui.py (bandeja + janela pywebview)
  ├─ thread: code_watcher.main()   → watchdog + git + gh + claude CLI
  ├─ thread: tail de events.jsonl  → estado em memória → painel
  └─ thread: ícone da bandeja (pystray)
```

Os dois lados conversam por arquivo, sem sockets: `control.json` (pausa,
GUI → watcher) e `events.jsonl` (histórico de revisões, watcher → GUI,
append-only). Tudo em `%LOCALAPPDATA%\CodeWatcher\`.

## Arquivos

| Arquivo | Papel |
|---|---|
| `code_watcher.py` | Motor: monitoramento, diff, chamada ao Claude CLI, `review-log.md`, eventos. |
| `watcher_gui.py` | Bandeja + janela do painel; roda o motor numa thread interna. |
| `ui.html` | Todo o HTML/CSS/JS do painel. |
| `make_icon.py` | Gera `icon.ico` (ícone do executável). |
| `build_exe.ps1` | Empacota tudo em `dist\CodeWatcher.exe` via PyInstaller. |
| `docs/code-watcher.md` | Documentação técnica completa — decisões, bugs corrigidos, limitações conhecidas. |

## Documentação completa

Para detalhes técnicos mais fundos (decisões de arquitetura, bugs
corrigidos e por quê, limitações conhecidas), veja
[`docs/code-watcher.md`](docs/code-watcher.md).
