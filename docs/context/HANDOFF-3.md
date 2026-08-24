# Code Watcher — continuação do handoff (sessão de 2026-08-24, parte 3)

> Gerado em 2026-08-24, sessão seguinte às de `HANDOFF.md` e `HANDOFF-2.md` —
> leia aqueles primeiro se ainda não leu. Este documento cobre uma sessão
> bem mais longa: descoberta e correção de um refactor incompleto que
> quebrava o app, um deadlock intermitente na janela, um vazamento de
> memória real, e uma leva grande de funcionalidades novas pedidas pelo
> Arthur ao longo da conversa. **Ainda tem uma pendência em aberto na
> seção 6** — leia antes de considerar o vazamento de memória resolvido.

---

## 1. Resumo desta sessão em uma frase

Um refactor feito por outra sessão de LLM (que dividiu `code_watcher.py` e
`watcher_gui.py` monolíticos num pacote `watcher/`) tinha ficado
**incompleto e quebrava o app ao abrir**; depois de corrigido, a sessão
seguiu com uma limpeza de repositório, dois bugs reais de Windows
(deadlock na janela, vazamento de memória), e ~12 funcionalidades novas
pedidas pelo Arthur — a maioria já testada e no ar, uma ainda em
verificação final.

---

## 2. O que aconteceu, em ordem

### 2.1 Refactor incompleto quebrava o app (corrigido)

Antes desta sessão, outra LLM tinha começado um refactor: extrair a lógica
de `code_watcher.py`/`watcher_gui.py` para um pacote `watcher/`
(`config.py`, `git.py`, `llm.py`, `logger.py`, `monitor.py`, `review.py`,
`gui/app.py`, `gui/state.py`, `gui/tray.py`). O `code_watcher.py` da raiz
tinha virado corretamente um shim fino (`sys.exit(watcher.monitor.main())`),
**mas `watcher_gui.py` da raiz não foi tocado** — continuava sendo o
arquivo monolítico antigo inteiro, ainda com `import code_watcher as cw` e
chamando `cw.read_control()`, que não existia mais no shim novo.

Um subagente de revisão de código, pedido para conferir esse refactor,
**não pegou esse bug** (só leu os arquivos, não rodou o app de verdade).
Só foi descoberto testando o app ao vivo (`python watcher_gui.py --show`),
que quebrou com `AttributeError: module 'code_watcher' has no attribute
'read_control'`.

**Correção:** `watcher_gui.py` virou um shim fino igual ao `code_watcher.py`
(`sys.exit(watcher.gui.app.run_gui())`). Commit `42f1070`.

**Lição:** revisão de código lendo arquivo por arquivo não substitui rodar
o programa. Da próxima vez que alguém disser "revisei e está tudo certo"
depois de um refactor, exigir que o app tenha sido **executado**, não só
lido.

### 2.2 Limpeza de repositório

Pedido do Arthur: auditar e apagar arquivos não usados. Removido (~67MB,
tudo já no `.gitignore`, não afetava o repositório):
- Logs de debug soltos de sessões anteriores (`console_err.log`,
  `console_out.log`, `err_icon_test.log`).
- Artefatos de build regeneráveis (`build/`, `dist/`, `CodeWatcher.spec`,
  `__pycache__/`).

Mantidos de propósito: `watcher-spec.md`, `watcher-gui-spec.md`,
`watcher-gui-prompt.md` — são specs históricas citadas explicitamente em
`docs/code-watcher.md` e no `HANDOFF.md`, não lixo.

### 2.3 Deadlock intermitente ao abrir a janela (corrigido)

Depois da correção do item 2.1, o app abria mas travava
("Não está respondendo") de forma **intermitente** — funcionava numas
vezes, travava em outras, sem padrão óbvio.

**Causa raiz:** `show_window()` em `watcher/gui/app.py` chamava
`self.window.show()` / `self.window.restore()` (API do **pywebview**) a
partir de uma thread de fundo (`show_soon`, dispara 2s após a criação da
janela). Essas chamadas fazem `Invoke` **síncrono** na thread da UI do
WinForms. Se essa thread ainda estiver ocupada inicializando o WebView2
quando a chamada chega — o que pode levar bem mais que 2s às vezes — trava
de verdade. Mesma classe de bug já documentada no `HANDOFF-2.md` para o
ícone (resolvida trocando `SendMessageW` por `PostMessageW`).

**Correção:** `_force_show_at()` já mostrava/restaurava/posicionava a
janela via **Win32 puro** (`ShowWindow` + `SetWindowPos` com
`SWP_SHOWWINDOW`), sem depender do loop de mensagens do WinForms —
`show_window()` parou de chamar `self.window.show()`/`restore()` e usa só
o caminho Win32. Commit `1334aaf`. Testado com `smoke_test.ps1` (script
novo, criado nesta sessão — abre a GUI N vezes seguidas e checa se trava)
8/8 sem travar, várias vezes ao longo da sessão.

**`smoke_test.ps1` teve seu próprio bug:** usava `pythonw` "pelado" em vez
do caminho completo. Nesta máquina isso às vezes resolve para o shim do
Windows Apps, que reexecuta o interpretador real como processo **filho
separado** — `Start-Process` devolve o PID do shim, e matar só esse PID
deixa o processo real órfão. Corrigido resolvendo o caminho completo de
antemão e identificando/matando processos pela linha de comando, não pelo
PID devolvido. Commit `ca2610d`.

### 2.4 Rate limit, dedup de diffs, rotação de eventos, docs (commit `5ee3094`)

Quatro melhorias pedidas juntas:
- **Rate limit de LLM:** `MAX_REVIEWS_PER_HOUR` (30 de fábrica), janela
  deslizante em memória em `review.py`. Acima do limite, revisão é pulada
  (log + evento `review_failed` com `reason="rate_limit"`).
- **Rotação de `events.jsonl`:** acima de 5MB, eventos antigos viram
  contagem agregada em `events_summary.json` e são removidos do arquivo,
  mantendo as últimas 2000 linhas. `WatcherState` soma o resumo arquivado
  ao que ainda está no arquivo, então o "Total histórico" nunca zera.
- **`smoke_test.ps1`:** ver seção 2.3.
- **`docs/code-watcher.md` atualizado** para refletir o pacote `watcher/`
  pós-refactor (estava descrevendo a arquitetura antiga com subprocesso +
  Job Object).

### 2.5 Severidade, notificação, dedup por diff, busca (commit `378968a`)

- **Severidade estruturada:** o prompt agora pede uma primeira linha
  `SEVERIDADE: alta|media|baixa` (`SEVERITY_INSTRUCTIONS` em
  `watcher/llm.py`), extraída e removida do texto exibido
  (`_extract_severity` em `review.py`).
- **Notificação da bandeja** em achado crítico — `App._notify_if_critical`,
  via um callback novo (`on_live_event`) que `tail_events()` chama só para
  eventos **novos** (não no replay do histórico no boot).
- **Deduplicação de diffs idênticos:** hash SHA-256 normalizado
  (`diff_fingerprint()` em `git.py`) — um `git commit --amend` que só muda
  a mensagem, um rebase sem conflito, ou um push sem mudança real de
  conteúdo pulam a chamada ao LLM. Guardado em `seen_diff_hashes.json`.
  **Retry manual ignora a dedup de propósito** (usuário pediu de novo, na
  cara), mas ainda respeita o rate limit.
- **Busca no painel** (`ui.html`) — campo de texto no feed, filtro
  client-side por projeto/arquivo/conteúdo.

### 2.6 Ícones e acentuação (commit `90cefc5`)

Pedido do Arthur olhando o painel: trocar emojis (🔍, ⚙️) por ícones
SVG inline no estilo **Feather Icons** (sem dependência de CDN — o app
roda offline), e adicionar ícones nos cards de estatística. No mesmo
pedido, corrigida a acentuação em português que estava toda errada nos
textos visíveis do painel, nas mensagens de toast (`watcher/gui/app.py`) e
na linha de severidade do `review-log.md`.

### 2.7 Custo estimado, soneca, filtro de críticas, config no painel (commit `41e6cb5`)

Pedido depois do Arthur confirmar que trocou o provedor padrão para a
**API da OpenAI** (paga por token, diferente da assinatura do Claude CLI):

- **Custo estimado por chamada:** `call_openai()` retorna o `usage`
  (tokens) da resposta; `estimate_cost_usd()` converte pra USD com uma
  tabela de preços por modelo (`OPENAI_PRICING_PER_1M` em `watcher/llm.py`
  — aproximada, lista pública, não é a fatura real). `call_llm()` mudou de
  assinatura: agora retorna `(texto, custo_usd)`. Custo salvo no
  `review-log.md` e somado por dia/total no painel.
- **Soneca:** `snooze_pause(minutes)` em `watcher/config.py` pausa com
  retomada automática (`paused_until`, checado dentro do próprio
  `read_control()`, sem timer de fundo). `write_control()` sempre limpa
  `paused_until` num pause/unpause manual — senão um pause manual feito
  depois de uma soneca antiga herdaria um horário de retomada já vencido.
- **Filtro "Só críticas"** no feed (client-side).
- **Rate limit e limiar de notificação viraram configuráveis** pelo modal
  de Configurações (`max_reviews_per_hour`, `notify_severity` em
  `control.json`), antes eram constantes fixas.

### 2.8 Sparklines com dados reais (commit `5ebf4a6`)

Pedido: "gráficos de linha bonitinhos" nos cards de estatística. **Decisão
deliberada:** só implementados onde havia dado real e barato disponível —
nunca fabricar uma tendência falsa ao lado de números reais (ver skill de
dataviz usada nesta sessão). Implementado em 3 dos 5 cards:
- "Total histórico" → contagem diária dos últimos 14 dias
  (`daily_trend` em `WatcherState.snapshot()`).
- "Revisões nesta hora" → distribuição real das chamadas na janela de
  rate limit, 12 baldes de 5min (`rate_limit_buckets()` em `review.py`).
- "Tempo em revisão" → duração das últimas 12 revisões, derivado
  client-side do feed já carregado (sem mudança de backend).

"Revisões nesta sessão" e "Resumo de hoje" ficaram **sem** sparkline de
propósito — não havia série temporal real e barata disponível sem inventar
dado ou adicionar bucketing por hora.

SVG puro, sem lib nova. `pathLength="1"` deixa a animação de "desenhar a
linha" exata independente da geometria; ponto final pulsa via CSS. Só
redesenha (e reanima) quando os valores mudam de verdade — senão o tick
reiniciaria a animação toda hora.

---

## 3. Vazamento de memória real — descoberto, mitigado, **verificação final pendente**

### 3.1 Como foi descoberto

Depois de várias reaberturas do app ao longo da sessão (testes manuais +
`smoke_test.ps1`), o painel começou a mostrar "Não está respondendo" de
forma persistente (diferente do deadlock do item 2.3, que sempre se
resolvia sozinho ou nunca ficava "não respondendo" por mais de ~20s).

Investigação por `tasklist`/`Get-Process` revelou:
- Um processo `pythonw.exe` **sozinho**, sem ter sofrido nenhum
  `taskkill -F` recente, rodando havia só ~3 minutos, com **8,3GB de
  working set / 15,5GB de memória privada**.
- Em paralelo, dezenas de `msedgewebview2.exe` órfãos apareceram em
  determinado momento — **isso sim** era efeito colateral de eu ter
  forçado o encerramento (`taskkill -F`) do app várias vezes durante os
  testes (o Job Object que fazia essa limpeza foi removido no refactor da
  seção 2.1, então matar o processo pai à força não mata mais os filhos do
  WebView2). Esse problema foi resolvido limpando os órfãos manualmente —
  **não é a causa do vazamento de 8GB**, é um problema separado (higiene
  de teste, evitar `taskkill -F` repetido).

O vazamento de 8-15GB em ~3 minutos, num processo único e "limpo", é
diferente: aconteceu de novo com uma instância normal, sem nenhum kill
forçado envolvido.

### 3.2 Hipótese e mitigação aplicada (commits `5bfef84`, `9b79f58`)

**Hipótese:** o painel faz *polling* de `get_state()` a cada tick via a
ponte JS↔Python do pywebview (backend WinForms/pythonnet). O `snapshot()`
retornado inclui a lista `feed` inteira — até 60 cards, cada um com o
**texto completo em Markdown** da revisão (pode ter alguns KB cada).
Mandar isso inteiro pela ponte COM a cada segundo, para sempre, é um
padrão conhecido por vazar memória em bridges pythonnet/WinForms (RCW/COM
wrappers não liberados a tempo) — a app já fazia esse polling de 1s antes
desta sessão, mas os campos novos (custo, sparklines) aumentaram o volume
de dados por chamada, e não há registro de alguém ter medido memória de
perto antes.

**Mitigação aplicada (ainda sem confirmação final):**
1. `ui.html`: intervalo do `setInterval(tick, ...)` mudado de **1000ms
   para 3000ms** — corta o volume de chamadas pela ponte em 3x.
2. `watcher/gui/state.py`: novo `MAX_FEED_REVIEW_CHARS = 2000` —
   `snapshot()` agora trunca o texto de cada revisão a 2000 caracteres
   (`_feed_card_for_output()`), com aviso "veja o review-log.md do
   projeto para o texto completo". O texto completo nunca foi perdido —
   sempre esteve gravado no `review-log.md` do projeto; isso só limita o
   que trafega pela ponte COM a cada tick.

**Resultado observado até o momento em que este documento foi escrito:**
depois de reiniciar o app com essas duas mudanças, memória ficou **estável
em ~120MB por pelo menos 2 minutos contínuos** (119,6MB aos 0,9min →
121,2MB aos 2min) — nada parecido com o crescimento para GB de antes. Uma
checagem final (~5-6 min de uptime) foi agendada mas **o resultado dela
ainda não chegou** quando este handoff foi escrito.

### 3.3 O que fazer se o vazamento voltar

Se a memória voltar a crescer descontroladamente mesmo com as mitigações
acima:
- **Reduzir ainda mais o intervalo de polling** (`setInterval(tick, ...)`
  em `ui.html`) — 3000ms foi um chute razoável, não uma medição exaustiva.
- **Considerar que o vazamento seja do próprio pywebview 6.2.1 no bridge
  JS↔Python, independente do tamanho do payload** — nesse caso, truncar
  texto e reduzir frequência só atrasam o problema, não resolvem. Vale
  pesquisar issues conhecidas do pywebview/pythonnet sobre memory leak com
  o backend WinForms, e considerar atualizar a versão do pywebview.
  Também vale considerar arquitetura por *push* (Python empurra só o que
  mudou) em vez de *poll* completo a cada tick, que é uma mudança maior.
- **PID a monitorar:** o processo `pythonw.exe` filho de
  `watcher_gui.py --show` (não confundir com processos `msedgewebview2.exe`
  — aqueles são helpers do WebView2, e memória neles é esperada e separada
  do vazamento investigado aqui).
- Ferramenta útil: `Get-CimInstance Win32_Process -Filter
  "Name='pythonw.exe'" | Where-Object { $_.CommandLine -like
  "*watcher_gui.py*" }` para achar o PID certo, depois `Get-Process -Id
  <pid> | Select WorkingSet64, PrivateMemorySize64`.

---

## 4. Decisões importantes desta sessão (não perguntar de novo)

- **Nunca fabricar dado falso em visualização.** Ao pedir sparklines, a
  decisão foi implementar só onde havia série real e barata (3 de 5
  cards), deixando os outros 2 sem gráfico em vez de inventar uma
  tendência. Ver seção 2.8. Vale para qualquer pedido futuro de "deixar
  bonito" com gráfico.
- **Revisão de código que só lê arquivos não é suficiente para validar um
  refactor.** Depois do incidente da seção 2.1, qualquer "revisei e está
  tudo certo" sobre um refactor de UI/app precisa incluir rodar o
  programa de verdade, não só ler o diff.
- **Evitar `taskkill -F` repetido em testes manuais.** Causou acúmulo de
  processos `msedgewebview2.exe` órfãos (seção 3.1) porque o Job Object
  que fazia essa limpeza foi removido do app. Preferir fechar pelo menu
  "Sair" da bandeja quando possível; se precisar forçar, checar depois se
  sobrou `msedgewebview2.exe` órfão.
- **Retry manual de commit ignora a deduplicação por diff de propósito**
  (seção 2.5) — só o rate limit ainda se aplica a ele.
- **Custo estimado da OpenAI é aproximado**, nunca a fatura real — tabela
  de preços mantida à mão em `watcher/llm.py`, pode ficar desatualizada se
  a OpenAI mudar preço.

## 5. Estado agora (fim desta sessão)

- App rodando via `pythonw watcher_gui.py --show`, todas as correções e
  funcionalidades desta sessão ativas.
- Provedor de LLM configurado: **OpenAI**, modelo `gpt-4o`.
- Repositório: https://github.com/ndmg-dev/CodeWatcher, branch `main`,
  tudo commitado e enviado, incluindo as mudanças de mitigação de memória
  (commits `5bfef84` e `9b79f58` — aparentemente commitadas por um
  mecanismo de auto-commit do ambiente, não por um `git commit` explícito
  desta sessão; vale confirmar se isso é esperado).
- **Pendência real: seção 3, verificação final do vazamento de memória**
  ainda não confirmada no momento em que este documento foi escrito. Se o
  Arthur está lendo isso numa sessão nova, a primeira coisa a fazer é
  checar se a mitigação realmente resolveu (deixar o app aberto por
  10-15min e observar a memória) antes de considerar o assunto fechado.

## 6. O que NÃO está feito (próximos passos possíveis)

- **Confirmação final do vazamento de memória** (seção 3.3).
- **`gh auth login`** — já foi feito em algum momento desta sessão (as
  chamadas de PR funcionaram), mas vale confirmar que continua válido.
- Ideias de melhoria discutidas mas **não implementadas** (o Arthur pode
  querer retomar):
  - Filtro por severidade combinado com busca por texto ao mesmo tempo
    (já dá pra fazer os dois juntos na versão atual, não é bloqueio).
  - Exportar/backup agregado do `review-log.md` de todos os projetos.
  - Suporte a duas contas `gh` diferentes (`ndmg-dev` e `tnunes8`) sem
    precisar `gh auth switch` manual.
  - Comentar automaticamente no PR do GitHub — decisão explícita de NÃO
    fazer isso sem pedido claro (reverteria "somente leitura").
- Lista completa de limitações conhecidas mais antigas:
  `docs/code-watcher.md`, seção 9.
