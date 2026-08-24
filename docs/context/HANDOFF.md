# Code Watcher — contexto para continuar em outra conversa

> Gerado em 2026-08-24, ao final de uma sessão longa de trabalho no Code
> Watcher. Se você está lendo isso numa conversa nova: este documento é o
> ponto de entrada — leia ele primeiro, depois vá para
> `docs/code-watcher.md` (a documentação completa e sempre atualizada) para
> qualquer detalhe técnico mais fundo.

---

## 1. O que é o Code Watcher, em uma frase

Um app de bandeja do sistema (Windows), rodando em background e
independente do VSCode, que observa os repositórios git do Arthur e manda
automaticamente para o **Claude Code CLI** revisar (foco em bugs e
melhorias) sempre que algo muda — um arquivo salvo, um commit novo, ou um
PR aberto/atualizado no GitHub. A resposta vai para um `review-log.md` na
raiz de cada projeto e aparece em tempo real num painel visual.

## 2. Estado agora (2026-08-24, fim da sessão)

- **App rodando**: `watcher_gui.py` (bandeja + painel) + `code_watcher.py`
  (motor), ambos vivos. Reiniciado pela última vez às 09:58:37 já com todas
  as correções desta sessão.
- **12 projetos monitorados**: CRM_MG, CRONOS_MG, TASK_MANANGER,
  ABRIR_EMPRESA, acessMG, ANALYTICS_DP, CalendarAI_PRO, DEDEIMPORTS_LP,
  FRONTEIRA_LP, GERADOR_DE_NOTAS, PROJETO-CARNE-LEAO, RAZÃO (eram só 3 no
  início da sessão).
- **Três fontes de revisão ativas**: arquivo salvo (desde sempre), commit
  novo (implementada hoje), PR do GitHub (implementada hoje, mas **inerte
  até o Arthur rodar `gh auth login`** — ver seção 5).
- **Sobe sozinho no boot** via `startup.ps1` na Área de Trabalho, registrado
  na pasta Inicializar do Windows, com espera de 15s antes de abrir a
  janela (para não competir por GPU com Docker/VSCode/Chrome no boot).
- **`WATCHER_PROJECTS` (esta pasta) não é um repositório git** — só uma
  pasta comum. O Arthur pediu explicitamente uma pasta simples em vez de um
  repo git para este documento de contexto.

## 3. O que aconteceu nesta sessão, em ordem

Esta foi uma sessão de continuação — o app já existia (watcher de linha de
comando + interface gráfica) de conversas anteriores. Nesta sessão:

1. **Buscar repositórios** — botão no painel que varre uma raiz procurando
   `.git` e adiciona vários projetos de uma vez (foi assim que os 12
   projetos entraram, a partir dos 3 originais).
2. **Investigação de travamento no boot** — driver de vídeo + carga de GPU
   no boot; corrigido com uma espera de 15s antes de abrir a janela do
   watcher no `startup.ps1`.
3. **Fase 1: detecção de commits** — git escreve o hash em
   `.git/refs/heads/<branch>` a cada commit; o watchdog já observa esse
   arquivo. Sem autenticação, sem dependência nova.
4. **Fase 2: leitura de PRs do GitHub** — thread de polling separada
   (`gh pr list` a cada 5 min), somente leitura, autenticação via `gh` CLI
   (instalado, mas o Arthur ainda não autenticou).
5. **Dois bugs reais achados no deploy das fases 1/2**: prompt do Claude
   passado como argumento de linha de comando estourava o limite do
   `cmd.exe` num commit grande (corrigido enviando via stdin); e a primeira
   vez que a detecção de commits rodava num repo com histórico disparava
   revisão de commits antigos (corrigido com "priming" — registra o estado
   atual como já visto, sem revisar, na largada).
6. **Investigações de "terminal abrindo sozinho"** (múltiplas, o Arthur
   relatou isso várias vezes ao longo do dia):
   - 1ª vez: rajada de inicialização do VSCode reabrindo — inofensiva, sem
     relação com o watcher.
   - 2ª vez: backend WSL do Docker Desktop reiniciando periodicamente —
     real, resolvida fechando o Docker (sem containers rodando).
   - **3ª vez, a causa raiz de verdade**: nenhuma das 7 chamadas a
     `git`/`gh`/`claude` dentro de `code_watcher.py` passava
     `creationflags=CREATE_NO_WINDOW`. Como esse processo roda sem console
     próprio, o Windows abria uma janela nova a cada chamada — a cada
     arquivo salvo, commit e checagem de PR. Corrigido nas 7 chamadas.
     Validado por diferença de tempo (30x `git branch`): ~377ms/chamada sem
     a flag vs ~38ms/chamada com ela.
7. **Filtro do feed por projeto** — clicar no nome do projeto agora filtra
   o feed de revisões; clicar na bolinha continua pausando/retomando (antes
   os dois cliques faziam a mesma coisa, o que confundia).
8. **Feed reconstrói o histórico ao reabrir o painel** — antes, reiniciar o
   app esvaziava o feed visível mas mantinha o contador da sidebar, o que
   parecia inconsistente ("mostra 1, mas diz que não tem nenhuma").

Detalhes técnicos completos de cada item, incluindo os testes que validaram
cada correção, estão em `docs/code-watcher.md` (seções 4 e 8).

## 4. Decisões importantes que já foram tomadas (não perguntar de novo)

- **PRs: somente leitura.** A revisão de PR nunca é postada de volta no
  GitHub — só vai para `review-log.md` e o painel. Confirmado
  explicitamente pelo Arthur em 2026-08-24.
- **Autenticação de PR: via `gh` CLI**, não token direto na API. O usuário
  roda `gh auth login` manualmente (fluxo OAuth no navegador) — isso não é
  algo que o Claude Code consiga fazer sozinho.
- **`projects.json` não precisou virar um schema mais rico** (`{"path":,
  "sources": [...]}`) para suportar PRs — o escopo de quem participa do
  polling é derivado automaticamente de `has_github_remote()` por
  repositório.
- **Terceira leitura da integração com GitHub (comentar automaticamente em
  PRs) está descartada por enquanto** — reverteria a decisão de "somente
  leitura". Não implementar sem pedido explícito.

## 5. Pendência do lado do usuário (não é trabalho meu)

```
gh auth login
```

É um fluxo OAuth interativo (abre o navegador) que só o Arthur pode
completar. Até lá, a leitura de PRs fica desativada — o `watcher.log`
mostra um aviso por repositório (uma vez por execução), sem quebrar nada
mais. Os repositórios monitorados pertencem a **duas contas GitHub
diferentes** (`ndmg-dev` e `tnunes8`) — se só uma for autenticada, os PRs
da outra continuam falhando até `gh auth login`/`gh auth switch` de novo.

## 6. Arquivos do projeto

| Arquivo | Papel |
|---|---|
| `code_watcher.py` | Motor: watchdog, git, gh, chamada ao Claude CLI |
| `watcher_gui.py` | App de bandeja + painel (pywebview) |
| `ui.html` | Todo o HTML/CSS/JS do painel |
| `docs/code-watcher.md` | Documentação completa e viva do projeto — **fonte de verdade técnica** |
| `docs/context/HANDOFF.md` | Este arquivo |
| `watcher-spec.md`, `watcher-gui-spec.md`, `watcher-gui-prompt.md` | Specs originais (históricas, pré-sessão) |

Estado em disco (fora do projeto), em `%LOCALAPPDATA%\CodeWatcher\`:
`projects.json` (lista viva de pastas), `events.jsonl` (histórico de
revisões), `control.json` (pausa), `seen_commits.json`,
`seen_prs.json`, `watcher.log`.

## 7. Memória do Claude Code sobre este projeto

Duas memórias de projeto já existem e serão carregadas automaticamente em
conversas futuras nesta pasta:

- **`code-watcher-proximos-passos`** — histórico de decisões sobre PRs e
  descoberta de repositórios, incluindo a correção de que o `gh` CLI
  precisou ser instalado (não estava, ao contrário do que uma memória
  anterior dizia erroneamente).
- **`code-watcher-console-flash-bug`** — a causa raiz do bug dos terminais
  piscando, com instrução explícita: **se o Arthur relatar terminais
  piscando de novo no futuro, checar `creationflags=CREATE_NO_WINDOW` em
  qualquer `subprocess.run` novo primeiro**, antes de investigar
  Docker/WSL/VSCode — essas explicações já mascararam a causa real duas
  vezes na mesma tarde.

## 8. O que NÃO está feito (próximos passos possíveis)

- **`gh auth login`** — só o Arthur pode fazer.
- **Fase 3 (comentar em PRs automaticamente)** — decisão já tomada de NÃO
  fazer isso por padrão. Só implementar se ele pedir explicitamente.
- **`events.jsonl` cresce indefinidamente** — nunca é rotacionado. Se virar
  problema de espaço/performance no futuro, dá para rotacionar guardando só
  os contadores.
- Lista completa de limitações conhecidas: `docs/code-watcher.md`, seção 9.
