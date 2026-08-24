# Spec — Code Watcher (revisão automática enquanto o Claude Code roda)

## Objetivo
Processo em background, independente do VSCode, que monitora pastas de
projeto e usa o tempo ocioso do usuário (enquanto o Claude Code processa
outras tarefas) para revisar alterações de código recém-salvas.

## Comportamento

1. Monitorar uma **lista fixa de pastas**, cada uma um repositório git.
2. Para cada arquivo alterado (extensões relevantes de código):
   - Aplicar **debounce de ~3s** (só age depois de um período de silêncio,
     pra não disparar a cada tecla salva).
   - Rodar `git diff HEAD -- <arquivo>` (mudanças não commitadas).
   - Se o diff estiver vazio, não fazer nada.
3. Enviar o diff para o **Claude Code CLI** em modo não interativo
   (`claude -p "<prompt>"`), pedindo revisão focada em **bugs e melhorias**.
4. Anexar o resultado em um arquivo `review-log.md`, **dentro da raiz de
   cada projeto monitorado**, no formato:

   ```
   ## AAAA-MM-DD HH:MM:SS — `caminho/relativo/arquivo.ext`

   <resposta da revisão>
   ```

5. Ignorar o próprio `review-log.md` como gatilho de evento (evitar loop).
6. Rodar continuamente até ser interrompido (Ctrl+C ou encerrado pelo SO).

## Requisitos técnicos
- Linguagem: Python 3.
- Biblioteca de monitoramento: `watchdog`.
- SO alvo: Windows.
- Chamada ao Claude Code CLI via `subprocess`, com timeout (ex: 180s) e
  tratamento de erro se o comando `claude` não existir no PATH.
- Configuração das pastas monitoradas e extensões relevantes deve ficar em
  constantes no topo do arquivo, fáceis de editar.
- Log de execução no console (o que está sendo monitorado, quando uma
  análise é disparada, onde foi salva).

## Fora de escopo (não incluir)
- Sem controle de mouse/teclado, sem interação com a UI do VSCode.
- Sem diff contra branch main/master — sempre contra HEAD.
- Sem múltiplas pastas configuráveis dinamicamente por parâmetro — lista
  fixa editada no próprio script.
- Sem interface gráfica — roda em terminal/background.

## Entregáveis esperados
- `code_watcher.py` — script principal.
- Instruções curtas de uso (como instalar dependência, como rodar, como
  editar a lista de pastas monitoradas).
