Já tenho um watcher funcionando (code_watcher.py) que monitora pastas de
projetos git, roda git diff, chama o Claude Code CLI para revisão, e grava
em review-log.md por projeto.

Agora quero evoluir isso para um app com ícone na bandeja do sistema (tray)
e uma janela própria, mais visual, no lugar de deixar um terminal aberto
rodando o script. Siga a spec abaixo à risca. Onde houver decisão técnica
em aberto (ex: customtkinter vs pywebview), decida você com base em
manutenibilidade e explique a escolha num comentário no topo do arquivo.

Antes de codar, se algo estiver ambíguo, me pergunte — não presuma.

Ao final:
1. Rode o app localmente para confirmar que o tray icon aparece, a janela
   abre, e que uma alteração de teste num arquivo monitorado aparece no
   feed em tempo real.
2. Me dê um resumo de como rodar e como trocar o comando no startup.ps1
   para abrir esse app (em vez do code_watcher.py direto).

--- SPEC ---

# Spec — Interface Desktop do Code Watcher

## Objetivo
Substituir a experiência de "terminal aberto rodando code_watcher.py" por um
app leve com ícone na bandeja do sistema (system tray) e uma janela própria,
com visual mais gráfico, mostrando o que o watcher está fazendo em tempo
real — sem precisar deixar um terminal do VSCode ocupado.

## Escopo

### Tray icon (bandeja do sistema)
- Ícone fica ativo enquanto o watcher roda em background.
- Clique com botão direito abre menu com:
  - "Abrir painel" (mostra a janela principal)
  - "Pausar monitoramento" / "Retomar monitoramento"
  - "Sair"
- Ícone muda de estado visual (ex: cor ou badge) quando uma revisão está
  em andamento vs. ocioso — feedback rápido sem precisar abrir a janela.

### Janela principal
- Lista de pastas monitoradas (as já configuradas no code_watcher.py:
  CRM_MG, CRONOS_MG, TASK_MANAGER, etc), com status de cada uma
  (ativa/pausada).
- Feed de revisões em tempo real: conforme o watcher processa uma
  alteração, aparece um card novo com timestamp, arquivo, e a sugestão
  do Claude Code CLI (mesmo conteúdo que hoje vai pro review-log.md).
- Painel de estatísticas:
  - Total de arquivos revisados (sessão atual e total histórico)
  - Tempo total economizado/rodando
  - Contagem por projeto (quantas revisões em cada pasta monitorada)
- Botão de pausar/retomar o monitoramento geral, e por pasta individual.

### Integração com o watcher existente
- Não reescrever a lógica de monitoramento (watchdog + git diff + chamada
  ao Claude Code CLI) — ela já funciona. A interface deve *consumir* os
  eventos do code_watcher.py, não substituí-lo.
- Abordagem sugerida: o code_watcher.py roda como processo em background
  e escreve/emite eventos (ex: arquivo JSON incremental, ou uma fila
  local, ou sockets locais) que a interface lê e exibe em tempo real.
- O review-log.md por projeto continua sendo gerado normalmente (não
  remover essa parte) — a interface é uma camada visual adicional, não
  uma substituição do log em arquivo.

## Requisitos técnicos
- Stack sugerida: Python, para reaproveitar o código do watcher.
  - Tray icon: `pystray`
  - Janela: `customtkinter` (visual mais moderno que tkinter padrão) ou,
    se quiser algo mais "fora da curva" visualmente, uma janela local via
    `pywebview` renderizando HTML/CSS/JS (permite um visual bem mais rico
    e customizado do que widgets nativos).
  - Decisão entre customtkinter vs pywebview deve ser tomada pelo Claude
    Code com base em facilidade de manutenção — registrar a escolha e o
    porquê num comentário no topo do arquivo principal.
- SO alvo: Windows.
- App deve iniciar minimizado na tray (não abrir a janela automaticamente
  ao ligar, só o ícone).
- Deve ser possível registrar esse app na mesma automação de inicialização
  já existente (startup.ps1), no lugar de rodar `python code_watcher.py`
  direto no terminal.

## Fora de escopo
- Não é necessário multiplataforma (só Windows).
- Não precisa de autenticação/multi-usuário.
- Não precisa editar configuração de pastas monitoradas pela interface
  nesta primeira versão (isso continua sendo editado direto no
  code_watcher.py) — a menos que seja trivial de incluir junto.

## Entregáveis esperados
- App principal (ex: `watcher_gui.py` ou equivalente) que inicia o
  code_watcher.py como processo gerenciado, mostra tray icon, e abre a
  janela com feed de revisões + estatísticas.
- Ajuste mínimo necessário no code_watcher.py para emitir eventos que a
  interface consiga consumir em tempo real (documentar o que mudou).
- Instruções curtas de uso: como rodar, como sai da bandeja, como
  integrar no startup.ps1 no lugar do comando atual.
