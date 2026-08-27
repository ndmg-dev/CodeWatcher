# Code Watcher — documentação do projeto

> Revisão automática de código enquanto você trabalha em outras tarefas.
> Última atualização: **2026-08-24** (refactor em pacote `watcher/`, correção
> de deadlock na janela, rate limit de revisões, rotação de `events.jsonl`,
> custo estimado da OpenAI, soneca no monitoramento, filtro de críticas)

---

## 1. O que é

Um processo em background, **independente do VSCode**, que monitora pastas de
projeto e usa o tempo ocioso do usuário para revisar alterações de código,
por três fontes:

**1. Arquivo salvo com mudança não commitada:**
1. Espera ~3s de silêncio (debounce), para não disparar a cada `Ctrl+S`.
2. Roda `git diff HEAD -- <arquivo>`.
3. Manda o diff para o **provedor de LLM configurado** (Claude Code CLI, via
   `claude -p`, ou API da OpenAI — escolha feita no painel, em
   "⚙️ Configurações") pedindo revisão focada em bugs e melhorias.

**2. Commit novo:**
1. Git escreve o novo hash em `.git/refs/heads/<branch>` a cada commit — um
   arquivo texto comum, que o watchdog já enxerga.
2. Se o hash for novo (nunca revisado para aquele branch), roda
   `git show <sha>` no lugar do diff não commitado.
3. Mesmo pipeline dali pra frente.

**3. PR aberto/atualizado no GitHub (desde 2026-08-24):**
1. Não há sinal local para "um PR foi aberto" — uma thread separada checa
   `gh pr list` a cada 5 minutos, para cada repositório com remote do
   GitHub.
2. Se o commit de topo do PR mudou desde a última vez (push novo, não só um
   comentário), roda `gh pr diff <numero>`.
3. Mesmo pipeline dali pra frente. **Somente leitura** — a revisão nunca é
   postada de volta no GitHub, só fica no `review-log.md` e no painel.

As três fontes convergem no mesmo destino:
4. Anexa a resposta ao `review-log.md` na raiz daquele projeto.
5. Publica o evento para a interface gráfica exibir em tempo real (com uma
   etiqueta "commit" ou "PR" distinguindo a origem no feed).

O projeto foi construído em quatro etapas: o watcher de linha de comando
(fonte 1), a camada visual (bandeja + painel), commits (fonte 2), e por
último PRs (fonte 3) por cima da mesma arquitetura.

---

## 2. Arquivos

Desde o refactor de 2026-08-24, a lógica mora em pacote (`watcher/`);
`code_watcher.py` e `watcher_gui.py` na raiz viraram pontos de entrada finos
que só importam de lá, mantidos por compatibilidade com o fluxo de uso
(`python code_watcher.py`, `pythonw watcher_gui.py`).

| Arquivo | Papel |
|---|---|
| `code_watcher.py` | Ponto de entrada do CLI. `sys.exit(watcher.monitor.main())`. |
| `watcher_gui.py` | Ponto de entrada da GUI. `sys.exit(watcher.gui.app.run_gui())`. |
| `watcher/config.py` | Constantes, `control.json`/`projects.json`/resumo de eventos (leitura/escrita). |
| `watcher/git.py` | Chamadas a `git`/`gh`, bookkeeping de commits/PRs já vistos, descoberta de repositórios. |
| `watcher/llm.py` | Prompts de revisão e os dois provedores (`call_claude`, `call_openai`), roteados por `call_llm`. |
| `watcher/logger.py` | `log()` (escreve em `watcher.log`, sem depender de stdout) e `emit_event()` (com rotação de `events.jsonl`). |
| `watcher/monitor.py` | Motor: watchdog, debounce, fila serializada, polling de PRs, `main(stop_event=...)`. |
| `watcher/review.py` | Pipelines de revisão (arquivo/commit/PR/retry), rate limit de chamadas ao LLM. |
| `watcher/secrets.py` | Scan local de segredos/credenciais em diffs, roda antes/independente do LLM. |
| `watcher/ask.py` | "Pergunte ao histórico" — pergunta em linguagem natural sobre o `review-log.md` de um projeto. |
| `watcher/patterns.py` | Detecta achados que se repetem entre projetos diferentes, a partir do backlog. |
| `watcher/summary.py` | Resumo de um período (hoje/ontem/7 dias) em todos os projetos, formato standup. |
| `watcher/gui/app.py` | Janela pywebview, ponte JS↔Python, tray icon, ciclo de vida da GUI. |
| `watcher/gui/state.py` | `WatcherState` — agrega eventos em memória para o painel consultar. |
| `watcher/gui/tray.py` | Ícone da bandeja e menu. |
| `watcher/gui/backlog_store.py` | Persiste o status (resolvido/dispensado) dos itens do backlog. |
| `ui.html` | Todo o visual do painel (HTML/CSS/JS). Separado de propósito — mexer na aparência não arrisca a lógica. |
| `smoke_test.ps1` | Abre a GUI N vezes seguidas e confere se trava — rodar depois de mudanças em `watcher_gui.py`/`watcher/gui/*.py`. |
| `docs/code-watcher.md` | Este documento. |

### Arquivos de estado (fora do projeto)

Em `%LOCALAPPDATA%\CodeWatcher\`:

| Arquivo | Conteúdo |
|---|---|
| `projects.json` | **Fonte da verdade** da lista de pastas monitoradas. Editado pelo painel. |
| `events.jsonl` | Log append-only de eventos. Alimenta o feed e o total histórico. Rotacionado automaticamente acima de 5MB (ver seção 4, "Rotação de `events.jsonl`"). |
| `events_summary.json` | Contagem (total e por projeto) dos eventos já rotacionados/removidos de `events.jsonl`. Gerado automaticamente, não editar à mão. |
| `seen_diff_hashes.json` | Último hash de diff revisado por arquivo/branch/PR — evita revisar de novo um diff idêntico (amend só de mensagem, rebase sem conflito). Gerado automaticamente. |
| `control.json` | Estado de pausa (geral e por pasta) + provedor de LLM escolhido (Claude ou OpenAI) e credenciais da OpenAI. |
| `watcher.log` | Saída do console do watcher (que não aparece mais, já que roda oculto). |

---

## 3. Arquitetura

```
watcher_gui.py  (pythonw, thread principal = janela pywebview, via watcher/gui/app.py)
   │
   ├── inicia watcher.monitor.main() numa thread do mesmo processo
   │      (não mais subprocesso — necessário para funcionar dentro do
   │       .exe empacotado --onefile; encerramento cooperativo via
   │       threading.Event, não Job Object)
   │
   ├── thread: tail de events.jsonl  ──► estado em memória ──► painel
   ├── thread: pystray (ícone da bandeja + menu)
   └── thread: atualiza o ícone conforme o estado

watcher/monitor.py (watcher/review.py, watcher/git.py, watcher/llm.py)
   ├── watchdog observa as pastas do projects.json
   ├── debounce 3s por arquivo
   ├── fila serializada (uma revisão por vez)
   ├── rate limit: no máx. MAX_REVIEWS_PER_HOUR chamadas ao LLM por hora
   ├── git diff HEAD ──► call_llm (Claude CLI ou API OpenAI) ──► review-log.md
   └── emit_event() ──► events.jsonl (rotacionado acima de 5MB)
```

> **Nota histórica:** até 2026-08-24, `code_watcher.py` rodava como
> **subprocesso** da GUI, amarrado a um **Job Object** do Windows
> (`KILL_ON_JOB_CLOSE`) para não sobrar órfão se a GUI caísse. Isso foi
> removido quando o motor virou thread do mesmo processo — o próprio
> processo morrer já encerra a thread, e o Job Object não fazia mais
> sentido nesse modelo.

### Comunicação entre os dois processos

Escolhida a abordagem mais simples que resolve: **arquivos**.

- **GUI → watcher**: `control.json` (pausa). O watcher relê a cada arquivo processado.
- **watcher → GUI**: `events.jsonl`, uma linha JSON por evento. A GUI faz tail.

Sem sockets, sem portas, sem servidor. Sobrevive a reinício de qualquer um
dos dois lados e dá o histórico de graça.

Eventos emitidos: `started`, `review_start`, `review_done`, `review_failed`,
`stopped`.

---

## 4. Decisões técnicas e o porquê

### pywebview em vez de customtkinter

A spec deixou a escolha em aberto. **Motivo dominante: manutenibilidade do
conteúdo exibido.**

O produto do watcher é a resposta do Claude, que vem em **Markdown** —
títulos, listas, negrito, blocos de código. Em customtkinter isso exigiria
montar tags manualmente num widget `Text` (calcular offsets de caractere,
aplicar estilo por intervalo): verboso, frágil e quebra a cada mudança de
formato da resposta. Em HTML/CSS o mesmo resultado sai com um renderizador
de ~40 linhas, e o layout vira CSS que qualquer um ajusta.

Fatores secundários:

- O **WebView2 já vem no Windows 11** (v151 confirmada na máquina) — sem runtime extra.
- Separar `ui.html` de `watcher_gui.py` isola aparência de lógica.
- Scroll de lista longa e layout responsivo são nativos do navegador; em Tk
  exigiriam Canvas + scrollbar manual.

Custo aceito: uma dependência a mais e a ponte Python↔JS — mantida mínima de
propósito (3 funções + polling de 1s, sem estado duplicado nos dois lados).

### A lista de pastas saiu do código-fonte

Originalmente `WATCHED_DIRS` era uma constante em `code_watcher.py`. Quando
a interface ganhou o botão "Adicionar pasta", isso virou problema: **uma
interface não deve reescrever um arquivo `.py`**.

A lista foi para `projects.json`. A constante virou **só a semente**: cria o
arquivo na primeira execução e depois não tem mais efeito. Está comentado no
código, porque é exatamente o tipo de coisa que confunde meses depois.

### Job Object para evitar processo órfão

Descoberto em teste: matando a GUI à força (crash, Gerenciador de Tarefas),
o `code_watcher.py` continuava rodando **invisível**, gastando chamadas ao
CLI sem nenhuma interface.

Solução: o subprocesso é colocado num **Job Object** do Windows com
`KILL_ON_JOB_CLOSE`. Quando o último handle do job fecha — o que acontece
quando a GUI morre por qualquer motivo — o Windows encerra o watcher junto.
Zero mudança no `code_watcher.py`. O "Sair" do menu continua fazendo
encerramento limpo.

### Reiniciar o watcher ao mudar a lista de pastas

O watchdog monta a árvore de observadores no `start()`. Trocar a lista exige
subir o processo de novo (~1s). Optou-se por isso em vez da complexidade de
adicionar/remover observadores a quente.

**Efeito colateral aceito:** uma revisão em andamento no instante do restart
é perdida. O arquivo é revisado de novo no próximo save.

### Revisões serializadas

Um único worker processa a fila, uma revisão por vez. Vários arquivos salvos
juntos viram chamadas em fila, não simultâneas — não estoura a conta nem a
máquina.

### Diff truncado em 12.000 caracteres

A spec pede o diff dentro do argumento `-p`. O Windows corta linhas de
comando muito longas, então diffs maiores são truncados (`MAX_DIFF_CHARS`).

### Detecção de commits: reaproveitar o watchdog, não fazer polling (2026-08-24)

Pedido: o watcher deveria também entender commits (não só arquivos salvos).
A pergunta de arquitetura era como *detectar* um commit novo.

Descoberta que guiou a escolha: git escreve o hash do commit em
`.git/refs/heads/<branch>` toda vez que você commita — um arquivo texto
comum. Isso é um **evento de sistema de arquivos**, e o watchdog já observa
recursivamente a partir da raiz do projeto (`.git` só é filtrado por lógica
nossa, `is_ignored_path`, não pelo watchdog em si). Bastou interceptar esse
caminho **antes** do filtro de `.git` e comparar o hash novo com o último
revisado daquele branch (`seen_commits.json`).

**Por que não polling:** a alternativa óbvia — checar `git log` a cada N
segundos — funcionaria, mas rodaria `git log` em todo repositório monitorado
mesmo quando nada muda, e adicionaria uma segunda malha de temporização à
arquitetura. A abordagem por ref-watching é **100% reativa, sem dependência
nova, sem autenticação** — o mesmo padrão arquitetural que já existia para
arquivos.

O `Debouncer` (antes só para arquivos) foi generalizado para agendar
qualquer função com qualquer argumento, mantendo a mesma garantia de sempre:
um único worker processa uma coisa de cada vez — nunca uma revisão de commit
e uma de arquivo em paralelo.

**Limitação aceita:** só olha o SHA *atual* do branch, não uma fila de
commits pendentes. Dois commits em sequência rápida (dentro dos 3s de
debounce) resultam em uma única revisão, a do mais recente — mesma
simplificação já usada para arquivos. Um `git reset --hard` para um commit
antigo nunca visto por este watcher também dispara uma "revisão" dele.

**Descoberta no teste real:** ao commitar, o `git diff HEAD` do arquivo já
some (o working tree passa a bater com HEAD), então o pipeline de arquivo
corretamente reporta "sem mudanças" e não duplica a revisão que o pipeline
de commit acabou de fazer — comportamento emergente do debounce, sem código
extra para garantir isso.

### PRs do GitHub: polling numa thread separada, somente leitura (2026-08-24)

Ao contrário de commits, **não existe sinal local** para "um PR foi
aberto/atualizado" — nenhum arquivo no disco muda quando alguém interage
com um PR no GitHub. Isso não cabe no mesmo mecanismo reativo dos commits;
precisa de uma checagem periódica de rede.

Decisões de escopo (definidas antes de codar):
- **Somente leitura** — a revisão vai para o `review-log.md` e para o
  painel; nada é escrito de volta no GitHub.
- **Autenticação via `gh` CLI** — o usuário roda `gh auth login` uma vez
  (fluxo OAuth no navegador); o app usa a sessão já autenticada, sem
  guardar token nenhum.

**Arquitetura:** uma thread dedicada (`github_poll_loop`), separada do
watchdog, verifica `gh pr list` a cada `PR_POLL_SECONDS` (5 min) para cada
repositório monitorado que tenha remote do GitHub (`has_github_remote`,
checado via `git remote get-url origin`). Repos sem remote do GitHub, ou
sem PRs abertos, são simplesmente pulados — **nenhuma mudança de schema**
foi necessária no `projects.json`, o escopo é derivado automaticamente do
remote de cada repo.

Só revisa de novo quando o **commit de topo do PR muda** (`headRefOid`),
não quando `updatedAt` muda — um comentário ou label alterado não deve
disparar uma revisão nova, só um push de código novo deve.

**Erros de autenticação/rede são avisados uma vez por repositório por
execução**, não a cada ciclo de 5 minutos — evita spammar o `watcher.log`
para sempre num repo que nunca vai ser autenticado (ex: se a conta usada
no `gh auth login` não tem acesso a um dos dois donos dos repos
monitorados, `ndmg-dev` e `tnunes8`).

**Por que não mudar `projects.json`:** a arquitetura original (documento
de decisão anterior a este trabalho) cogitava migrar cada entrada de
string para `{"path":, "sources": [...]}`. Não foi necessário — o
`has_github_remote()` já filtra automaticamente quem participa do polling,
sem exigir configuração por projeto.

### Prompt do Claude via stdin, não como argumento (2026-08-24)

Bug real encontrado em produção no primeiro deploy da detecção de commits:
um commit de ~28.000 caracteres de diff falhou com "Linha de comando muito
longa". A causa: `call_claude` passava o prompt inteiro como argumento
`-p "<prompt>"`, e o wrapper `.cmd` do Claude Code no Windows passa por
`cmd.exe`, cujo limite de linha de comando (~8191 caracteres) é bem menor
que o `MAX_DIFF_CHARS` de 12.000 — e menor ainda depois de somar o resto do
prompt (cabeçalho, instruções).

Isso **já era um risco latente também para arquivos** (mesmo `call_claude`,
mesmo limite), só nunca tinha sido exercitado porque um diff de arquivo
não commitado raramente chega perto de 8 mil caracteres — commits e PRs
inteiros, com vários arquivos, chegam com muito mais frequência.

Corrigido testando o CLI diretamente: `claude -p` lê o prompt do **stdin**
quando nenhum argumento de prompt é passado. Trocar `subprocess.run([...,
"-p", prompt])` por `subprocess.run([..., "-p"], input=prompt)` elimina o
limite do SO por completo, em vez de escolher um `MAX_DIFF_CHARS` menor que
poderia falhar de novo com um prompt levemente maior.

### Priming: evitar revisar commits antigos no primeiro boot (2026-08-24)

Segundo bug real encontrado no mesmo deploy: ao ligar o watcher pela
primeira vez num repositório com histórico, uma rajada de "revisões de
commit" disparou para commits **antigos**, não novos — incluindo um de
28 mil caracteres que expôs o bug do stdin acima.

Causa: o watchdog não distingue "arquivo de ref tocado porque alguém
commitou agora" de "arquivo de ref tocado por qualquer outro motivo"
(VSCode, `git gc`, sincronização de nuvem podem reescrever um arquivo de
ref com o mesmo conteúdo, o que ainda dispara um evento de sistema de
arquivos). Sem histórico prévio em `seen_commits.json`, o primeiro toque em
qualquer branch era tratado como "commit novo" — mesmo sendo o commit que
já estava lá havia semanas.

Corrigido com `prime_seen_commits()`: ao iniciar, todo repositório **sem
nenhuma entrada** em `seen_commits.json` tem os commits atuais de *todos*
os seus branches locais registrados como "já vistos", sem revisar nenhum.
Só commits genuinamente novos a partir daí disparam revisão. Repositórios
que já têm histórico não são mexidos.

**Assimetria deliberada com PRs:** o mesmo priming *não* foi aplicado a
PRs. Um PR aberto é, por natureza, algo pendente que faz sentido revisar
mesmo que já existisse antes do polling começar — diferente de um commit
antigo, que é só histórico. A primeira rodada de polling revisa todos os
PRs abertos encontrados.

---

## 5. Como rodar

### Dependências

```powershell
pip install watchdog pywebview pystray pillow
npm install -g @anthropic-ai/claude-code
winget install --id GitHub.cli
gh auth login
```

Versões confirmadas na máquina: Python 3.14.6 · watchdog 6.0.0 ·
pywebview 6.2.1 · pystray 0.19.5 · pillow 12.3.0 · Claude Code CLI 2.1.238 ·
GitHub CLI 2.98.0.

O `gh auth login` é **interativo** (abre o navegador para OAuth) — precisa
ser feito manualmente, uma vez, por quem for rodar o watcher. Sem isso, a
leitura de PRs (fonte 3) fica desativada silenciosamente: o watcher.log
mostra um aviso por repositório na primeira falha, mas o monitoramento de
arquivos e commits continua funcionando normalmente.

### App completo (bandeja + painel)

```powershell
pythonw c:\Users\User\Projetos\WATCHER_PROJECTS\watcher_gui.py
```

- Sobe **só na bandeja**. Com `--show`, abre o painel junto.
- Botão direito no ícone → **Abrir painel** / **Pausar** / **Sair**.
- O `X` da janela **esconde**, não encerra. Para encerrar de vez: **Sair**.

> **Windows 11 esconde ícones novos atrás do `^`.** Arraste o ícone verde do
> flyout para a barra de tarefas para deixá-lo fixo — senão o feedback de
> estado (verde/âmbar/cinza) fica invisível.

### Só o motor, no terminal

```powershell
python c:\Users\User\Projetos\WATCHER_PROJECTS\code_watcher.py
```

Funciona igual e lê o mesmo `projects.json`. A GUI é opcional.

---

## 6. Configuração

### Pastas monitoradas — pelo painel

**"+ Adicionar pasta"** abre o seletor nativo do Windows. Valida que é um
repositório git, salva e reinicia o watcher automaticamente.

**"🔍 Buscar repositórios"** abre o seletor de pastas para escolher uma
**raiz** (ex: `C:\Users\User\Projetos`) e varre recursivamente procurando
subpastas com `.git` (`discover_git_repos()` em `code_watcher.py`). Abre um
modal com um checkbox por repositório encontrado — os já monitorados vêm
desmarcados e acinzentados, com a etiqueta "já monitorado". Desmarque o que
não quiser e confirme: todos os selecionados entram de uma vez, com um
**único restart** do watcher (não um por pasta).

A varredura ignora as mesmas pastas de `IGNORED_DIRS` (`node_modules`,
`.git`, `venv`, …) e não desce dentro de um repositório já encontrado —
evita listar submódulos como projetos separados. Limitada a 5 níveis de
profundidade (`max_depth`) para não travar em árvores gigantes.

Para remover: passe o mouse sobre a pasta na lista e clique no `×`. Pede
confirmação. **Não apaga nada do disco** — o `review-log.md` continua lá.

Clicar no **nome** da pasta pausa/retoma só ela.

### Demais ajustes — constantes no topo do `watcher/config.py`

| Constante | Padrão | O que faz |
|---|---|---|
| `CODE_EXTENSIONS` | 25 extensões | Quais arquivos disparam revisão |
| `IGNORED_DIRS` | `node_modules`, `.git`, `venv`, … | Pastas ignoradas em qualquer nível |
| `DEBOUNCE_SECONDS` | `3.0` | Silêncio antes de disparar |
| `CLAUDE_TIMEOUT` | `180` | Timeout da chamada ao CLI |
| `CLAUDE_CMD` | `"claude"` | Troque pelo caminho completo se não estiver no PATH |
| `MAX_DIFF_CHARS` | `12000` | Limite do diff enviado (prompt via stdin, sem limite de SO) |
| `PROMPT_TEMPLATE` | — | O prompt de revisão de arquivo |
| `PROMPT_TEMPLATE_COMMIT` | — | O prompt de revisão de commit |
| `PROMPT_TEMPLATE_PR` | — | O prompt de revisão de PR |
| `GH_CMD` | `"gh"` | Comando do GitHub CLI, usado só para ler PRs |
| `PR_POLL_SECONDS` | `300` | Intervalo entre checagens de PR por repositório |
| `GH_TIMEOUT` | `30` | Timeout de cada comando `gh` |
| `DEFAULT_MAX_REVIEWS_PER_HOUR` | `30` | Valor de fábrica do teto de chamadas/hora — editável pelo painel (Configurações), gravado em `control.json` |
| `DEFAULT_NOTIFY_SEVERITY` | `"alta"` | Valor de fábrica do gatilho de notificação — editável pelo painel |
| `EVENTS_MAX_BYTES` | `5 MB` | Tamanho do `events.jsonl` que dispara rotação (ver seção 4) |
| `EVENTS_KEEP_LINES` | `2000` | Linhas recentes mantidas em `events.jsonl` após rotação |

---

## 7. Integração com o boot

O `startup.ps1` (na Área de Trabalho) abre a rotina de trabalho no logon:
Docker → VSCode → Claude → Chrome → **Code Watcher**.

Registrado via atalho em:
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Rotina de Trabalho.lnk`

```
powershell.exe -ExecutionPolicy Bypass -WindowStyle Hidden -File "C:\Users\User\Desktop\startup.ps1"
```

### Proteção contra abertura dupla

O Docker Desktop já se registra sozinho no `Run` do registro, então rodar o
`startup.ps1` abriria uma segunda instância. O script agora **checa se o app
já está rodando antes de abrir** (`Test-AppRunning`).

Preferiu-se isso a remover a entrada do registro, porque o instalador do
Docker se re-registra em atualizações — a checagem funciona
independentemente de quem dispara primeiro.

Bônus: quando o Docker já está de pé, o script pula também o
`Start-Sleep -Seconds 25`. Rodando com tudo aberto, leva **1s** em vez de 25s.

**Exceções deliberadas** (sem checagem): Chrome e Claude desktop. Ambos já
abertos, o comando só foca a janela / abre a aba do CRM — que é o
comportamento desejado.

### Incidente: travamento no boot (2026-08-24)

Em 2026-08-24 o boot travou visivelmente (percebido como "cmd abrindo em
loop"). Investigação pelos logs do Windows revelou:

- **3 eventos `VIDEO_TDR_TIMEOUT`** (driver de vídeo travando e sendo
  resetado pelo kernel) entre 08:01–08:07, exatamente na janela do boot.
- Um **`AppHang` real do `pythonw.exe`** (nosso app) às 08:05:41, no meio
  dos dois eventos de TDR.
- Nenhuma evidência de processo duplicando de verdade — o Prefetch não
  mostra nenhum `cmd.exe` rodando nesse período.

Leitura: Docker + VSCode + Chrome + Claude + a janela WebView2 do watcher —
todos pesados de GPU — abrindo juntos sobrecarregaram um driver de vídeo já
instável nessa máquina, e nosso app travou tentando desenhar a janela no
meio disso. O "loop de cmd" percebido foi provavelmente o sistema
piscando/recuperando dos resets de driver, não um processo nosso se
multiplicando.

**Correção:** `$watcherWaitSeconds = 15` — o Code Watcher agora espera 15s
depois de disparar Docker/VSCode/Claude/Chrome antes de criar sua janela,
evitando empilhar a criação da janela WebView2 em cima do pico de carga do
boot. A espera só acontece se o app ainda não estiver rodando (checagem via
`Test-AppRunning` antes do `Start-Sleep`).

A instabilidade do driver de vídeo em si é um problema de driver/hardware,
fora do controle do script — vale considerar atualizar o driver AMD
separadamente.

### Trocar o comportamento

| Quero | Faço |
|---|---|
| Painel **não** abrir no boot | Remover ` --show` dos argumentos no `startup.ps1` |
| Voltar ao watcher "cru" no terminal | Apontar `$watcherScript` para `code_watcher.py` e `$pythonwPath` para `python.exe` |
| Desativar o auto-start | Apagar o `Rotina de Trabalho.lnk` da pasta Inicializar |

---

## 8. Testes realizados

### Fluxo do watcher (etapa 1)

| Verificação | Resultado |
|---|---|
| Detecção de alteração | Capturada pelo watchdog |
| Debounce | 3 saves em ~1,2s → **1 única** análise |
| Anti-loop | `review-log.md` e `.txt` tocados → nenhum disparo |
| `git diff HEAD` | 288 chars extraídos |
| `claude -p` | Retornou em ~20s |
| Escrita | Anexado no formato da spec |

### Interface (etapa 2)

| Verificação | Resultado |
|---|---|
| Ícone na bandeja | Confirmado por screenshot |
| Janela abre | Confirmado |
| Feed em tempo real | Card em ~17s, Markdown renderizado |
| `review-log.md` continua | 1.241 bytes gerados |
| Pausa bloqueia revisão | Nenhuma chamada ao CLI desperdiçada |
| Estados do ícone | 4 estados testados na API |
| Sobe só na bandeja | Janela criada invisível |
| Sobe com painel (`--show`) | Janela 1180×760 abre sozinha |

### Adicionar/remover pastas — 17/17 verificações

| Caso | Comportamento |
|---|---|
| Repo git válido | Aceita, salva, reinicia o watcher |
| Pasta sem `.git` | Recusa e explica que precisa do git pro diff |
| Pasta já monitorada | Recusa e **não** reinicia à toa |
| Cancelar o seletor | Silencioso, sem erro |
| Remover pasta | Sai da lista, `review-log.md` preservado |
| Pasta apagada do disco | Marcada em vermelho com `(!)`, watcher ignora e segue |
| `projects.json` corrompido | Cai de volta na constante em vez de parar |

### Buscar repositórios (2026-08-24) — 17/17 verificações + teste real

| Caso | Comportamento |
|---|---|
| Repo aninhado (`nested/RepoC`) | Encontrado |
| Repo escondido dentro de `node_modules` | **Ignorado**, não desce ali |
| Pasta comum (sem `.git`) | Não aparece na lista |
| Raiz sem nenhum repo | Mensagem clara, sem erro |
| Repos já monitorados | Aparecem desmarcados/cinza no modal, com etiqueta |
| Adicionar vários selecionados | **Um único restart**, não um por pasta |
| Tudo já monitorado | Recusa sem reiniciar à toa |
| Cancelar o seletor | Silencioso |

Testado também **ponta a ponta contra a árvore real**
(`C:\Users\User\Projetos`, automação de clique + seletor nativo): achou os
12 repositórios corretos, excluiu corretamente os não-git e os que estavam
dentro de `node_modules`. Esse teste real efetivamente adicionou 9 projetos
novos ao monitoramento em produção, mantidos desde então (ver seção 6).

### Detecção de commits (2026-08-24) — 20/20 verificações + 2 testes reais

| Caso | Comportamento |
|---|---|
| `parse_ref_branch` em `refs/heads/<branch>` | Reconhece corretamente |
| Branch com `/` no nome (`feature/x`) | Reconhece corretamente |
| Arquivo `.lock` de ref | Ignorado |
| `.git/HEAD`, `.git/index`, arquivo de código comum | Ignorados |
| `git show` do commit | Assunto + diff extraídos certos |
| Commit novo | Dispara revisão |
| Mesmo SHA de novo | **Não** dispara (já visto) |
| Segundo commit novo | Dispara de novo, pega o commit certo |
| Eventos com `source=commit` | Presentes e corretos |
| Monitoramento pausado | Bloqueia revisão de commit também |
| `Debouncer` generalizado | Ainda serializa e colapsa corretamente |

Testado também com o **Observer real do watchdog + um `git commit` de
verdade no disco** (não chamando a função direto): o evento de ref disparou
a revisão de commit, e o fluxo antigo de arquivo não-commitado continuou
funcionando lado a lado, sem interferência. E, por fim, com o **Claude Code
CLI real** (não simulado): revisou um commit com SQL injection proposital e
identificou o problema corretamente.

### Leitura de PRs do GitHub (2026-08-24) — 16/16 verificações

| Caso | Comportamento |
|---|---|
| `has_github_remote` com origin do GitHub | `True` |
| `has_github_remote` sem remote | `False` |
| `gh` sem autenticação (estado real desta máquina) | Falha graciosamente, `None`, sem exceção |
| Mesmo repo sem autenticação de novo | **Não** repete o aviso (sem spam a cada ciclo) |
| `seen_prs`: marcar e checar dedup | Correto |
| PR novo | Dispara revisão |
| Mesmo `headRefOid` de novo (comentário, label) | **Não** dispara |
| `headRefOid` novo (push no PR) | Dispara de novo |
| Monitoramento pausado | Bloqueia revisão de PR também |
| Eventos com `source=pr` | Presentes e corretos |

O teste 3 (`gh` sem autenticação) validou o caminho de erro real desta
máquina — `gh` está instalado mas não autenticado no momento deste teste,
então o teste exercitou o comportamento de produção de verdade, não um
cenário simulado.

### Bugs encontrados nos testes e corrigidos

1. **`show()` pelo evento `loaded`** não funcionava (o evento não dispara em
   janela criada oculta). Trocado por thread. Afetava também o "Abrir painel"
   do menu da bandeja.
2. **Watcher órfão** ao matar a GUI à força. Resolvido com o Job Object.
3. **Watcher morto exibia "pausado"** no tooltip, o que enganava. Agora tem
   estado próprio: *"watcher parado (veja watcher.log)"*.
4. **4 bytes nulos (`\x00`) em `ui.html`**, no lugar de espaços, dentro do
   mecanismo de marcador de bloco de código do renderizador Markdown. A
   corrupção era simétrica (inserção e extração usavam o mesmo byte),
   então funcionava por acidente — mas é uma armadilha para qualquer
   ferramenta que trate NUL como fim de string. Corrigido para o espaço
   original.
5. **Falso alarme, não corrigido porque não era bug (2x):** `RAZÃO`
   apareceu como `RAZÃƒO` em comandos de diagnóstico do PowerShell, duas
   vezes em dias diferentes. Era sempre o `Get-Content` lendo sem
   `-Encoding utf8`; o `projects.json`, a leitura em Python, o
   `watcher.log` e a UI estavam sempre corretos.
6. **"Linha de comando muito longa" num commit grande (28 mil caracteres).**
   O prompt ia como argumento `-p "<prompt>"`, e o wrapper `.cmd` do Claude
   Code passa por `cmd.exe`, com limite bem menor que `MAX_DIFF_CHARS`.
   Corrigido enviando o prompt via stdin. Risco latente que já existia
   também para arquivos, só nunca tinha sido exercitado. Ver seção 4.
7. **Rajada de revisão de commits antigos no primeiro boot** com a detecção
   de commits ativada num repositório já existente — inclusive o commit de
   28 mil caracteres do bug 6. Corrigido com `prime_seen_commits()`. Ver
   seção 4. Esse bug também expôs o bug 6 em produção, contra os 12
   projetos reais — 3 chegaram a ter um commit revisado de verdade antes de
   eu interromper o processo; o `seen_commits.json` foi limpo e
   re-gerado do zero de forma consistente antes do redeploy.
8. **Terminal/console piscando o tempo todo** — o bug mais visível e
   incômodo de todos, percebido em vários momentos ao longo do dia
   (chegou a ser confundido com o Docker/WSL reiniciando, numa
   investigação anterior que também era real, mas era um problema
   diferente). Causa raiz: nenhuma das 7 chamadas a `git`/`gh`/`claude`
   dentro de `code_watcher.py` passava `creationflags=CREATE_NO_WINDOW`.
   Como esse processo roda sem console proprio (filho do `pythonw.exe`), o
   Windows abre uma janela de console nova para cada uma — a cada arquivo
   salvo, commit e checagem de PR. Ficou sem esse cuidado desde que os
   pipelines de commit e PR foram adicionados (o cuidado só tinha sido
   aplicado ao *lançar* o `code_watcher.py` a partir do `watcher_gui.py`,
   nunca *dentro* dele). Corrigido adicionando a flag as 7 chamadas.
   Validado por diferença de tempo: 30 chamadas de `git branch` sem a flag
   levaram ~11,3s (~377ms/chamada, custo de criar e destruir uma janela de
   console a cada vez); com a flag, ~1,1s (~38ms/chamada) — 10x mais
   rápido, evidência de que janelas reais deixaram de ser criadas.

---

## 9. Limitações conhecidas

- **"Tempo economizado" não foi implementado** — seria fórmula inventada. No
  lugar: uptime e tempo real gasto em revisões, ambos medidos.
- **Revisão em andamento é perdida** ao adicionar/remover pasta (restart do
  watcher). O arquivo é revisado de novo no próximo save.
- **Diffs acima de 12.000 caracteres são truncados**, mas o envio em si não
  tem mais limite de tamanho de linha de comando (prompt via stdin).
- **Windows apenas** — Job Object e `pythonw` são específicos da plataforma.
- **`WATCHER_PROJECTS` não é um repositório git.** É uma pasta comum, então
  não há histórico de versões deste código.
- **A varredura de repositórios tem profundidade limitada** (`max_depth=5`
  em `discover_git_repos`). Uma raiz com repositórios aninhados mais fundo
  que isso não os encontra.
- **Detecção de commit só olha o SHA atual do branch**, não uma fila de
  commits pendentes. Dois commits em sequência rápida (dentro do debounce)
  resultam em uma única revisão, a do mais recente. Um `git reset --hard`
  para um commit nunca visto por este watcher também dispara uma revisão.
- **Repos com refs "empacotados"** (`git gc` agressivo, `packed-refs`) só
  voltam a ter um arquivo solto em `refs/heads/<branch>` no próximo commit
  daquele branch — o que é o caso comum, mas um branch que nunca recebe
  commit novo não é observado dessa forma.
- **12 projetos monitorados agora, não mais 3** (desde 2026-08-24) —
  ABRIR_EMPRESA, acessMG, ANALYTICS_DP, CalendarAI_PRO, CRM_MG, CRONOS_MG,
  DEDEIMPORTS_LP, FRONTEIRA_LP, GERADOR_DE_NOTAS, PROJETO-CARNE-LEAO, RAZÃO,
  TASK_MANANGER. Cada salvamento com mudança não commitada nesses repos
  agora dispara uma chamada real ao Claude CLI — mais volume de chamadas do
  que antes.
- **Leitura de PRs desativada até `gh auth login` ser feito manualmente.**
  O `gh` CLI foi instalado (v2.98.0) mas a autenticação é um fluxo
  interativo que só o usuário pode completar. Até lá, o `watcher.log` mostra
  um aviso por repositório (uma vez por execução) e o restante do
  monitoramento continua normal.
- **Autenticação do `gh` é de uma conta só por vez.** Os repositórios
  monitorados hoje pertencem a duas contas GitHub diferentes (`ndmg-dev` e
  `tnunes8`). Se `gh auth login` autenticar só uma delas, os PRs dos repos
  da outra conta vão falhar silenciosamente (aviso no log, sem crash) até
  `gh auth login` ser rodado de novo para a segunda conta, ou até usar
  `gh auth switch` conforme necessário.
- **Polling de PR é só leitura e só sobre o estado atual.** Se dois pushes
  acontecerem no mesmo PR dentro do intervalo de 5 minutos, só o mais
  recente é revisado — mesma simplificação já aceita para commits e
  arquivos.
- **A contagem da janela de rate limit é em memória**, não persiste entre
  reinícios do watcher (o teto em si, esse sim, fica salvo em
  `control.json`). Um commit ou PR pulado por rate limit não é marcado
  como "visto" — só é revisado de novo se o ref mudar de novo, ou via
  "Revisar novamente" no painel.
- **Custo estimado da OpenAI é aproximado**, calculado com uma tabela de
  preços por modelo mantida à mão em `watcher/llm.py`
  (`OPENAI_PRICING_PER_1M`). Se a OpenAI mudar o preço de um modelo, a
  estimativa do painel fica desatualizada até alguém atualizar a tabela —
  nunca é a fatura real, só uma referência.

---

## 10. Próximos passos

Duas ideias sobre "descobrir/entender repositórios via git" evoluíram nesta
área do projeto:

**Descoberta de repositórios locais** (levantada em 2026-08-21) — varrer
uma raiz procurando `.git` em vez de apontar pasta por pasta. Implementada
em 2026-08-24 (seção 6).

**Consciência nativa de git** (pedida em 2026-08-24: "ler commits, PRs,
tudo mais") — quebrada em fases:

- ✅ **Fase 1, commits locais** — implementada em 2026-08-24 (seção 4,
  "Detecção de commits"). 100% local, sem autenticação.
- ✅ **Fase 2, PRs do GitHub** — implementada em 2026-08-24 (seção 4, "PRs do
  GitHub: polling numa thread separada, somente leitura"). Somente leitura,
  autenticação via `gh` CLI (instalado, v2.98.0). Falta rodar
  `gh auth login` manualmente — é um fluxo OAuth interativo, então não dá
  pra automatizar. Até lá, a leitura de PRs fica desativada (ver seção 9).
  - Diferente do que a arquitetura original cogitava, `projects.json`
    **não** precisou crescer para `{"path":, "sources": [...]}` — o escopo
    de quem participa do polling é derivado automaticamente de
    `has_github_remote()` por repositório, sem configuração extra.

### Rate limit de revisões (2026-08-24)

Com 12 projetos monitorados e um provedor de LLM pago por token (API da
OpenAI, opção adicionada em paralelo a isto), um repositório barulhento ou
um bug de loop poderiam gerar chamadas em excesso sem nenhum aviso.

`review.py` mantém uma janela deslizante em memória (`_call_times`, thread-safe)
com as chamadas ao LLM na última hora, somando **todos** os projetos. Acima
de `MAX_REVIEWS_PER_HOUR` (30, constante em `watcher/config.py`), revisões
novas são puladas: log em `watcher.log` + evento `review_failed` com
`reason="rate_limit"`, sem chamar o provedor. O painel mostra
"Revisões nesta hora: X/30" na barra lateral.

**Efeitos aceitos:** o contador não persiste entre reinícios do watcher
(reseta a cada troca de pasta monitorada, que já reinicia o processo). Um
commit ou PR pulado por rate limit não fica marcado como "visto" — só é
revisado de novo se o ref mudar de novo, ou via o botão "Revisar novamente".
Chamado de arquivo (`process_file`) não tem esse problema porque reavalia o
diff a cada save.

### Rotação de `events.jsonl` (2026-08-24)

Limitação conhecida desde o início do projeto (seção 9): o log de eventos
cresce para sempre, sendo a fonte tanto do feed em tempo real quanto do
"Total histórico" exibido no painel.

Resolvido com `_rotate_events_if_needed()` em `watcher/logger.py`: antes de
cada `emit_event()`, se `events.jsonl` já passou de `EVENTS_MAX_BYTES` (5MB),
os eventos mais antigos são resumidos (contagem total e por projeto) em
`events_summary.json` e removidos do arquivo, mantendo só os últimos
`EVENTS_KEEP_LINES` (2000) para o feed continuar mostrando histórico recente.
`WatcherState` soma o resumo arquivado à contagem do que ainda está no
arquivo, então o "Total histórico" nunca volta a zero por causa da rotação.

### Severidade estruturada, notificação e resumo diário (2026-08-24)

O prompt de revisão passou a pedir uma primeira linha no formato
`SEVERIDADE: alta|media|baixa` (constante `SEVERITY_INSTRUCTIONS` em
`watcher/llm.py`), extraída em `review.py` (`_extract_severity`) e removida
do texto exibido antes de salvar. Se o modelo não seguir o formato, assume
`baixa` — não quebra o fluxo, só não conta como crítico.

Três funcionalidades usam esse campo:

- **Notificação da bandeja em achado crítico:** `App._notify_if_critical()`
  em `watcher/gui/app.py`, acionada via um callback opcional (`on_live_event`)
  que `tail_events()` chama só para eventos novos (não no replay do
  histórico no boot, senão notificaria tudo de uma vez ao abrir o app depois
  de um tempo parado). Usa `pystray.Icon.notify()` — sem dependência nova.
- **Resumo diário no painel:** `WatcherState.daily_counts`, um dicionário
  `{"YYYY-MM-DD": {"total", "critical"}}` alimentado em `apply()` pela data
  do próprio evento. `snapshot()` calcula `datetime.now()` a cada chamada
  para escolher o dia — a virada de meia-noite se resolve sozinha, sem
  precisar de um timer de fundo.
- **Badge de severidade no card do feed** (`ui.html`) — só aparece quando
  não é "baixa", para não poluir o feed com uma tag em toda revisão.

### Deduplicação de diffs idênticos (2026-08-24)

Um `git commit --amend` que só muda a mensagem, um rebase sem conflito, ou
um push que força um PR sem mudança real de conteúdo, disparavam uma
revisão nova do zero — mesmo cobrando o LLM por algo já revisado.

`diff_fingerprint()` em `watcher/git.py` normaliza o diff (`\r\n` → `\n`,
strip) e calcula um hash SHA-256. Antes de cada chamada ao LLM, `review.py`
compara com o último hash revisado para aquela chave
(`seen_diff_hashes.json`, chaveado por `repo:tipo:identificador` — arquivo
usa o caminho relativo, commit usa o branch, PR usa o número). Hash igual
→ pula a chamada, loga e (para commit/PR) marca como visto mesmo assim, para
não ficar reprocessando o mesmo diff em toda tentativa.

**Retry manual ignora a deduplicação de propósito** — se o usuário clicou
"Revisar novamente", ele quer a revisão de novo, ponto; só o rate limit
ainda se aplica.

### Busca no painel (2026-08-24)

Campo de texto no cabeçalho do feed (`#search-box` em `ui.html`), filtrando
client-side por projeto, arquivo ou conteúdo da revisão — o feed já vive
inteiro em memória no JS, então não precisou de nenhuma mudança no backend.
Combina com o filtro por projeto já existente (clicar no nome da pasta).

### Custo estimado da OpenAI, soneca e configuração pelo painel (2026-08-24)

Quatro melhorias pedidas depois de trocar o provedor padrão para a API da
OpenAI (paga por token, ao contrário da assinatura do Claude CLI):

- **Custo estimado por chamada:** `call_openai()` em `watcher/llm.py` agora
  retorna também o `usage` (prompt/completion tokens) devolvido pela API.
  `estimate_cost_usd()` converte isso em USD usando uma tabela de preços
  aproximada por modelo (`OPENAI_PRICING_PER_1M`) — só uma estimativa
  exibida no painel, nunca a fatura real. Claude CLI sempre retorna custo
  `0.0` (roda sob assinatura, sem contagem de tokens por chamada aqui).
  `call_llm()` mudou de assinatura: retorna `(texto, custo_usd)` em vez de
  só o texto.
- **Rastreamento de custo:** o custo de cada revisão vai no evento
  `review_done` (`cost_usd`) e é gravado no `review-log.md`
  (`**Custo estimado:** $0.0032`). `WatcherState` soma um total histórico
  (com baseline em `events_summary.json`, sobrevive à rotação) e um total
  do dia (mesmo dicionário `daily_counts` do resumo diário). O painel
  mostra os dois, com o aviso "(OpenAI, estimado)".
- **Soneca (pausa temporária):** `snooze_pause(minutes)` em
  `watcher/config.py` grava `paused=True` + `paused_until` (ISO). A
  retomada automática acontece dentro do próprio `read_control()` — se
  `paused_until` já passou, ele desfaz a pausa e regrava o arquivo antes
  de devolver o estado, sem precisar de um timer de fundo. **Importante:**
  `write_control()` (usado pelo pause/resume manual e por
  `toggle_project`/config) sempre limpa `paused_until` quando `paused` é
  passado explicitamente — senão um pause manual feito depois de uma
  soneca antiga herdaria um horário de retomada já vencido e despausaria
  sozinho na hora errada. Só `snooze_pause()` deve gravar `paused_until`
  no futuro.
- **Rate limit e limiar de notificação configuráveis pelo painel:**
  `MAX_REVIEWS_PER_HOUR` e o gatilho de severidade da notificação (ver
  seção anterior) eram constantes fixas; agora são campos em
  `control.json` (`max_reviews_per_hour`, `notify_severity`), editáveis
  no modal de Configurações, com os mesmos valores de fábrica de antes
  (30/hora, só severidade alta) quando nada foi salvo ainda.

### Deadlock intermitente ao mostrar a janela (2026-08-24)

`show_window()` chamava `self.window.show()`/`self.window.restore()` (API do
pywebview) a partir de uma thread de fundo, ~2s após a criação da janela.
Essas chamadas fazem `Invoke` síncrono na thread da UI do WinForms — se essa
thread ainda estiver ocupada inicializando o WebView2 quando a chamada
chega, trava de verdade (mesma classe de bug já resolvida para o ícone,
trocando `SendMessageW` por `PostMessageW` — ver seção 8, bug do ícone).

Como `_force_show_at()` já mostra, restaura, posiciona e dá foco na janela
via **Win32 puro** (`ShowWindow` + `SetWindowPos` com `SWP_SHOWWINDOW`), sem
depender do loop de mensagens do WinForms, bastava usar só ele —
`show_window()` não chama mais `self.window.show()`/`restore()`. Testado
com `smoke_test.ps1` (8 aberturas seguidas, 0 travamentos; antes travava de
forma intermitente).

**Ideia futura, deliberadamente não implementada:** integrar de volta com
o GitHub — comentar automaticamente no PR com a revisão, em vez de só
ficar no `review-log.md`. Isso reverteria a decisão de "somente leitura"
tomada em 2026-08-24. Escreveria em um lugar visível para outras pessoas
no repositório, categoria de decisão diferente de tudo que foi construído
até aqui — vale pensar melhor antes de fazer.
