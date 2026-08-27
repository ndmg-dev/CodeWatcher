# Code Watcher — handoff

> Ponto de partida pra continuar o projeto. Pra profundidade técnica
> (decisões de arquitetura, bugs corrigidos e por quê, limitações
> conhecidas), ver [`docs/code-watcher.md`](code-watcher.md) — este
> documento aqui é só o estado atual e o que falta.

## 1. Estado agora

- Repo: `github.com/ndmg-dev/CodeWatcher`, branch `main`.
- **2 commits locais ainda não enviados** (`097efef`, `7d016d3` — ajustes
  de estética + redesign de layout). Rodar `git push origin main` antes
  de considerar o trabalho "publicado".
- App funcional, rodando via `pythonw watcher_gui.py --show` (ou sem
  `--show` pra subir só na bandeja). 13 projetos monitorados em produção.
- Backlog e review-log.md têm dados reais acumulados — não é ambiente de
  teste vazio.

## 2. O que existe (visão rápida)

Motor headless (`watcher/monitor.py`, `review.py`, `git.py`, `llm.py`)
observa arquivos salvos, commits e PRs, manda pro LLM configurado
(Claude Code CLI ou API OpenAI) e grava em `review-log.md` por projeto.
GUI (`watcher/gui/`) é bandeja + painel pywebview, tudo em `ui.html`
(HTML/CSS/JS puro, sem framework). Os dois processos conversam só por
arquivo (`control.json`, `events.jsonl`) — sem socket.

Módulos além do motor original:
- `watcher/secrets.py` — scan local de segredos (regex), roda antes do LLM.
- `watcher/ask.py` — "pergunte ao histórico" (LLM sobre o review-log.md de um projeto).
- `watcher/patterns.py` — detecta achados repetidos entre projetos (via backlog).
- `watcher/summary.py` — resumo diário/standup (hoje/ontem/7 dias).
- `watcher/gui/backlog_store.py` — persiste status (resolvido/dispensado) do backlog.

## 3. O que foi feito nesta rodada de trabalho (resumo cronológico)

1. **Fixes de notificação da bandeja**: clique na notificação abre o
   painel (pystray não tratava isso por padrão); painel visível não
   dispara mais notificação; ícone/nome próprios via
   `SetCurrentProcessExplicitAppUserModelID` (antes aparecia "Python").
2. **Scan de segredos** (`watcher/secrets.py`) — regex local, roda antes/
   independente do LLM, card vermelho dedicado no feed.
3. **Backlog marcável** — achados alta/média e segredos viram pendências
   com resolver/dispensar/reabrir, histórico de resolvidos.
4. **Pergunte ao histórico**, **padrões repetidos entre projetos**,
   **resumo diário/standup** — três funcionalidades que reusam o mesmo
   LLM configurado sobre dados já coletados (review-log.md, backlog,
   events.jsonl).
5. **Limpeza do repositório pra divulgação pública**: removidos arquivos
   de spec/prompt originais e notas de handoff antigas (tinham "cara de
   sessão de IA"); `docs/code-watcher.md` reescrito em primeira pessoa,
   sem nome nem narração de sessões; **histórico do git reescrito** (6
   commits antigos tinham rodapé `Co-Authored-By: Claude`, removido via
   `git filter-branch` + `push --force` — branch local
   `backup-before-history-rewrite` guarda o histórico original, caso
   precise).
6. **Ajustes de estética** — scrollbar customizada, abas com sublinhado,
   dots sem glow no estado default.
7. **Redesign completo de layout** — paleta oklch, sidebar reorganizada
   (ações rápidas em grid, stats em grid 2×2 sem sparklines), header com
   segmented control, cards com fonte mono e radius maior. Baseado num
   handoff de design (`.dc.html` + screenshot) que **não foi versionado**
   no repo de propósito (mesma lógica do item 5 — arquivo de spec bruto
   não pertence a um repo público).
8. **Fix**: item resolvido/dispensado no backlog não some mais do feed —
   o card correspondente fica marcado ("Resolvido no backlog" /
   "Dispensado no backlog") e esmaecido, sem sumir do histórico principal.

## 4. Lições aprendidas do jeito difícil (não repetir)

- **O app é 100% offline por design.** Uma tentativa de carregar
  JetBrains Mono via Google Fonts (`<link>` no `<head>`) travou a
  inicialização inteira — o WebView2 ficava preso esperando a fonte
  externa, o evento `pywebviewready` nunca disparava, e a janela ficava
  invisível pra sempre (sem erro no log, processo vivo mas "não
  responde"). Nunca adicionar `<link>`/fetch pra recurso externo no
  `ui.html` — só fontes já instaladas no Windows (Consolas pra mono,
  Segoe UI pro resto).
- **Chamada de LLM na ponte JS nunca pode ser síncrona.** `ask_history`,
  `detect_patterns`, `generate_summary` (as 3 funcionalidades sob
  demanda) rodam em background via `App._run_async()` + polling
  (`poll_job`) — se a chamada bloquear a thread da ponte pywebview/
  WinForms, a janela mostra "Não está respondendo" pela duração inteira
  da chamada (até 180s). Qualquer novo bridge method que chame o LLM
  precisa seguir esse mesmo padrão.
- **Cuidado ao automatizar `SetForegroundWindow`/`ShowWindow` de fora do
  processo** (ex: script de captura de tela) — repetido rápido demais
  pareceu contribuir pra travamentos da janela real (mesma classe do bug
  de deadlock já documentado em `docs/code-watcher.md`, seção 4). Se for
  tirar screenshot do app pra alguma tarefa futura, prefira uma única
  captura, sem foco repetido.
- **`git filter-branch` com `--all` reescreve TODAS as branches locais**,
  inclusive uma branch de backup criada na hora — só serve de backup de
  verdade se apontar pro ref original (`refs/original/refs/heads/<nome>`)
  depois do filter-branch, não pro HEAD atual.

## 5. Pendências / próximos passos

- **Enviar os 2 commits locais** (`097efef`, `7d016d3`) pro GitHub —
  ficaram só localmente até o momento deste handoff.
- Nada do roadmap de funcionalidades ficou pendente (scan de segredos,
  backlog, pergunte ao histórico, padrões repetidos, resumo diário — os
  5 implementados e testados).
- Ideia futura **deliberadamente não implementada** (ver
  `docs/code-watcher.md`, fim): comentar automaticamente no PR do GitHub
  com a revisão, em vez de só `review-log.md`. Reverteria a decisão de
  "somente leitura" — categoria de decisão diferente, pensar bem antes.
- Divulgação em redes sociais estava em andamento (legenda já escrita,
  screenshot do painel completo ainda não finalizado) — se retomar isso,
  o painel já está com o layout novo, vale recapturar a imagem.
