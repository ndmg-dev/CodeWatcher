"""
code_watcher.py — revisao automatica de codigo em background.

Monitora uma lista de repositorios git, com tres fontes de revisao:

1. Arquivo salvo com mudanca nao commitada: espera um periodo de silencio
   (debounce), tira o `git diff HEAD` do arquivo e manda para o Claude Code
   CLI revisar.
2. Commit novo: git escreve o hash em `.git/refs/heads/<branch>` a cada
   commit — um arquivo texto comum, que o watchdog ja enxerga (normalmente
   filtrado por estar dentro de `.git`). Interceptamos essa escrita antes do
   filtro, comparamos com o ultimo commit revisado daquele branch
   (seen_commits.json) e, se for novo, revisamos `git show <sha>` no lugar
   do diff nao commitado.
3. PR aberto/atualizado no GitHub: nao ha sinal local para isso, entao uma
   thread separada consulta `gh pr list` periodicamente (PR_POLL_SECONDS)
   para cada repositorio com remote do GitHub, e revisa `gh pr diff` quando
   o commit de topo do PR muda (seen_prs.json). Somente leitura — a revisao
   fica no review-log.md e no painel, nunca e postada de volta no GitHub.
   Requer `gh auth login` feito manualmente (fluxo interativo, fora do
   alcance deste script).

As tres fontes convergem no mesmo pipeline: chamar o Claude, anexar a
resposta ao review-log.md na raiz do projeto, emitir um evento para a
interface grafica.

Uso:  python code_watcher.py
Sair: Ctrl+C
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# Este processo roda sem console proprio (filho do pythonw.exe). Sem essa
# flag, toda chamada a um programa de console (git, gh, claude) faz o
# Windows abrir uma janela de terminal nova so pra ele — e como isso
# acontece a cada arquivo salvo, commit e checagem de PR, sem a flag o
# usuario ve terminais piscando o tempo todo. Repassada em todo
# subprocess.run() deste arquivo que invoca um programa externo.
CREATE_NO_WINDOW = 0x08000000

# ---------------------------------------------------------------------------
# CONFIGURACAO — edite daqui pra baixo
# ---------------------------------------------------------------------------

# Pastas monitoradas — SEMENTE INICIAL.
#
# A lista viva fica em projects.json (veja PROJECTS_FILE mais abaixo), que e
# o que a interface grafica edita quando voce adiciona/remove pastas. Esta
# constante so e usada na primeira execucao, para criar o projects.json.
# Depois disso, editar aqui nao tem mais efeito — use o painel, ou edite o
# projects.json direto.
WATCHED_DIRS = [
    r"C:\Users\User\Projetos\CRM_MG",
    r"C:\Users\User\Projetos\CRONOS_MG",
    r"C:\Users\User\Projetos\TASK_MANANGER",
]

# Extensoes consideradas "codigo". Qualquer outra e ignorada.
CODE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
    ".html", ".css", ".scss", ".sass",
    ".java", ".kt", ".go", ".rs", ".rb", ".php",
    ".c", ".h", ".cpp", ".hpp", ".cs",
    ".sql", ".sh", ".ps1",
}

# Pastas ignoradas em qualquer nivel da arvore.
IGNORED_DIRS = {
    ".git", "node_modules", "venv", ".venv", "env", "__pycache__",
    "dist", "build", ".next", ".nuxt", "target", "vendor",
    ".idea", ".vscode", "coverage", ".pytest_cache", ".mypy_cache",
}

# Segundos de silencio antes de disparar a analise de um arquivo.
DEBOUNCE_SECONDS = 3.0

# Comando do Claude Code CLI. Se nao estiver no PATH, troque pelo caminho
# completo do executavel, ex: r"C:\Users\User\AppData\Roaming\npm\claude.cmd"
CLAUDE_CMD = "claude"

# Timeout (segundos) da chamada ao CLI.
CLAUDE_TIMEOUT = 180

# Comando do GitHub CLI, usado so para ler PRs (fonte 3). Precisa de
# `gh auth login` feito manualmente uma vez — nao e algo que este script
# possa fazer sozinho (fluxo interativo no navegador).
GH_CMD = "gh"

# Intervalo entre verificacoes de PRs novos/atualizados por repositorio.
PR_POLL_SECONDS = 300

# Timeout (segundos) de cada comando `gh` (list ou diff).
GH_TIMEOUT = 30

# Nome do arquivo de log de revisoes, criado na raiz de cada projeto.
REVIEW_LOG_NAME = "review-log.md"

# Limite de caracteres do diff enviado ao CLI. O Windows corta linhas de
# comando muito longas, entao diffs gigantes sao truncados.
MAX_DIFF_CHARS = 12000

# --- Integracao com a interface grafica (watcher_gui.py) -------------------
# O watcher publica eventos em um arquivo JSONL e le o estado de pausa de um
# arquivo JSON de controle. Rodar o watcher sozinho no terminal continua
# funcionando normalmente; a GUI e opcional.

STATE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "CodeWatcher"
)
EVENTS_FILE = os.path.join(STATE_DIR, "events.jsonl")
CONTROL_FILE = os.path.join(STATE_DIR, "control.json")

# Lista viva de pastas monitoradas, editada pelo painel. Criado na primeira
# execucao a partir de WATCHED_DIRS.
PROJECTS_FILE = os.path.join(STATE_DIR, "projects.json")

# Ultimo commit revisado por branch/repo, para nao revisar o mesmo commit
# duas vezes (ex: se o mesmo arquivo de ref for tocado mais de uma vez).
SEEN_COMMITS_FILE = os.path.join(STATE_DIR, "seen_commits.json")

# Ultimo commit de topo revisado por PR/repo, para nao revisar o mesmo
# estado do PR duas vezes (um comentario ou label mudando updatedAt nao
# deve disparar revisao nova, so um push novo deve).
SEEN_PRS_FILE = os.path.join(STATE_DIR, "seen_prs.json")

# Log de texto deste modulo (mensagens de log() abaixo). Antes so existia
# como redirecionamento do stdout do subprocesso pela GUI; desde que o
# watcher passou a rodar numa thread dentro do mesmo processo da GUI
# (empacotada como exe --windowed, sem console/stdout), log() escreve aqui
# diretamente em vez de depender de print().
WATCHER_LOG_FILE = os.path.join(STATE_DIR, "watcher.log")

# Prompt enviado ao Claude. {path} e {diff} sao substituidos.
PROMPT_TEMPLATE = """Voce esta revisando uma alteracao de codigo ainda nao commitada.

Arquivo: {path}

Diff (git diff HEAD):
```diff
{diff}
```

Revise focando em BUGS e MELHORIAS. Seja direto e conciso:
- Liste bugs reais, erros de logica, casos de borda nao tratados e riscos de seguranca.
- Depois, sugestoes objetivas de melhoria (clareza, performance, duplicacao).
- Se nao houver nada relevante a apontar, responda apenas "Sem observacoes."
Nao reescreva o arquivo inteiro nem repita o diff."""

# Prompt para revisao de commit (fonte 2). {sha}, {subject} e {diff} sao
# substituidos.
PROMPT_TEMPLATE_COMMIT = """Voce esta revisando um commit que acabou de ser feito.

Commit: {sha}
Mensagem: {subject}

Diff (git show):
```diff
{diff}
```

Revise focando em BUGS e MELHORIAS. Seja direto e conciso:
- Liste bugs reais, erros de logica, casos de borda nao tratados e riscos de seguranca.
- Depois, sugestoes objetivas de melhoria (clareza, performance, duplicacao).
- Se nao houver nada relevante a apontar, responda apenas "Sem observacoes."
Nao reescreva o arquivo inteiro nem repita o diff."""

# Prompt para revisao de PR (fonte 3). {number}, {title} e {diff} sao
# substituidos.
PROMPT_TEMPLATE_PR = """Voce esta revisando um Pull Request aberto no GitHub.

PR #{number}: {title}

Diff (gh pr diff):
```diff
{diff}
```

Revise focando em BUGS e MELHORIAS. Seja direto e conciso:
- Liste bugs reais, erros de logica, casos de borda nao tratados e riscos de seguranca.
- Depois, sugestoes objetivas de melhoria (clareza, performance, duplicacao).
- Se nao houver nada relevante a apontar, responda apenas "Sem observacoes."
Nao reescreva o arquivo inteiro nem repita o diff."""

# ---------------------------------------------------------------------------
# Implementacao
# ---------------------------------------------------------------------------


_log_lock = threading.Lock()


def log(msg):
    """Registra uma linha com timestamp em watcher.log e, se houver console
    (rodando `python code_watcher.py` direto no terminal), tambem no
    stdout. Um app --windowed empacotado nao tem stdout (e None ou falha
    ao escrever), entao o arquivo e a fonte de verdade — nao o print().
    """
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    if sys.stdout is not None:
        try:
            print(line, flush=True)
        except Exception:
            pass
    try:
        with _log_lock:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(WATCHER_LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError:
        pass


_emit_lock = threading.Lock()


def emit_event(event_type, **fields):
    """Publica um evento (uma linha JSON) para a interface grafica consumir.

    Nunca levanta excecao: se o arquivo de eventos estiver indisponivel, o
    monitoramento continua normalmente — a GUI e um extra, nao um requisito.
    """
    event = {"ts": datetime.now().isoformat(timespec="seconds"),
             "type": event_type}
    event.update(fields)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with _emit_lock, open(EVENTS_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_control():
    """Le o estado de pausa definido pela GUI. Default: nada pausado."""
    try:
        with open(CONTROL_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"paused": False, "paused_projects": []}
    return {
        "paused": bool(data.get("paused", False)),
        "paused_projects": list(data.get("paused_projects", [])),
    }


def load_watched_dirs():
    """Retorna a lista viva de pastas monitoradas.

    Le projects.json. Na primeira execucao o arquivo nao existe: cria a
    partir da constante WATCHED_DIRS. Se o arquivo estiver corrompido,
    cai de volta na constante em vez de parar de monitorar.
    """
    try:
        with open(PROJECTS_FILE, encoding="utf-8") as fh:
            dirs = json.load(fh)
        if isinstance(dirs, list) and all(isinstance(d, str) for d in dirs):
            return [os.path.abspath(d) for d in dirs]
        log(f"! {PROJECTS_FILE} com formato inesperado; usando WATCHED_DIRS.")
    except FileNotFoundError:
        save_watched_dirs(WATCHED_DIRS)
    except (OSError, ValueError) as exc:
        log(f"! nao foi possivel ler {PROJECTS_FILE} ({exc}); usando WATCHED_DIRS.")
    return [os.path.abspath(d) for d in WATCHED_DIRS]


def save_watched_dirs(dirs):
    """Grava a lista de pastas (escrita atomica, para nao corromper)."""
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = PROJECTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump([os.path.abspath(d) for d in dirs], fh,
                  ensure_ascii=False, indent=2)
    os.replace(tmp, PROJECTS_FILE)


_seen_commits_lock = threading.Lock()


def load_seen_commits():
    """Le {repo: {branch: [shas ja revisados]}}. Vazio se o arquivo nao existe."""
    try:
        with open(SEEN_COMMITS_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def mark_commit_seen(repo_key, branch, sha, keep_last=50):
    """Registra um commit como revisado.

    Mantem so os ultimos `keep_last` shas por branch, para o arquivo nao
    crescer para sempre em repos com muitos commits.
    """
    with _seen_commits_lock:
        seen = load_seen_commits()
        shas = seen.setdefault(repo_key, {}).setdefault(branch, [])
        if sha not in shas:
            shas.append(sha)
        del shas[:-keep_last]
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = SEEN_COMMITS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(seen, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, SEEN_COMMITS_FILE)


def list_local_branches(repo_root):
    """Lista os branches locais do repositorio. [] em caso de erro."""
    try:
        result = subprocess.run(
            ["git", "branch", "--format=%(refname:short)"],
            cwd=repo_root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [b.strip() for b in result.stdout.splitlines() if b.strip()]


def prime_seen_commits(dirs):
    """Registra os commits atuais de cada repo NOVO como 'ja vistos', sem
    revisar nenhum deles.

    Sem isso, o simples ato de ligar o watcher pela primeira vez num repo
    com historico poderia disparar revisao de commits antigos: qualquer
    ferramenta que toque um arquivo de ref (VSCode, `git gc`, etc.) sem um
    commit novo de verdade acontecer conta como evento para o watchdog, e
    sem "priming" o watcher trataria o commit atual (que pode ser gigante e
    nao tem nada de novo) como se fosse novo. So roda para repos que ainda
    nao tem nenhuma entrada em seen_commits.json — um repo que ja tinha
    historico de commits revisados nao e mexido.
    """
    with _seen_commits_lock:
        seen = load_seen_commits()
        changed = False
        for repo_root in dirs:
            repo_key = os.path.normcase(repo_root)
            if repo_key in seen:
                continue
            branches = list_local_branches(repo_root)
            if not branches:
                continue
            seen[repo_key] = {}
            for branch in branches:
                sha = read_ref_sha(repo_root, branch)
                if sha:
                    seen[repo_key][branch] = [sha]
            changed = True
        if changed:
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = SEEN_COMMITS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(seen, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, SEEN_COMMITS_FILE)


def load_seen_prs():
    """Le {repo: {numero_pr: ultimo_head_sha_revisado}}."""
    try:
        with open(SEEN_PRS_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def mark_pr_seen(repo_key, number, head_sha):
    """Registra o head SHA revisado de um PR."""
    seen = load_seen_prs()
    seen.setdefault(repo_key, {})[str(number)] = head_sha
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = SEEN_PRS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(seen, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, SEEN_PRS_FILE)


def is_git_repo(path):
    """True se o caminho for a raiz de um repositorio git."""
    return os.path.isdir(os.path.join(path, ".git"))


def discover_git_repos(root, max_depth=5):
    """Varre 'root' recursivamente procurando repositorios git.

    Nao desce dentro de um repositorio ja encontrado (evita listar
    submodulos como repos separados) nem dentro de IGNORED_DIRS/pastas
    ocultas. max_depth limita o quao fundo a varredura vai, para nao travar
    em arvores gigantes (node_modules ja e coberto por IGNORED_DIRS, mas uma
    raiz mal escolhida ainda pode ter muitas pastas soltas).
    """
    root = os.path.abspath(root)
    found = []

    def walk(path, depth):
        if is_git_repo(path):
            found.append(path)
            return  # nao desce dentro de um repo encontrado
        if depth >= max_depth:
            return
        try:
            entries = list(os.scandir(path))
        except OSError:
            return
        for entry in entries:
            if entry.name in IGNORED_DIRS or entry.name.startswith("."):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    walk(entry.path, depth + 1)
            except OSError:
                continue

    walk(root, 0)
    return found


def project_name(repo_root):
    """Nome curto do projeto (usado como chave nos eventos e na GUI)."""
    return os.path.basename(os.path.normpath(repo_root))


def is_ignored_path(path):
    """True se o caminho estiver dentro de alguma pasta ignorada."""
    parts = os.path.normpath(path).split(os.sep)
    return any(part in IGNORED_DIRS for part in parts)


def parse_ref_branch(repo_root, path):
    """Se 'path' for o arquivo de ref de um branch local, retorna o nome do
    branch (ex: 'main', 'feature/x'). Caso contrario, None.

    Branches ficam em .git/refs/heads/<nome>, e um nome com "/" (comum em
    fluxos tipo feature/x) vira subpastas reais nesse caminho.
    """
    try:
        rel = os.path.relpath(path, repo_root)
    except ValueError:
        return None
    parts = rel.split(os.sep)
    if len(parts) < 4 or parts[0] != ".git" or parts[1] != "refs" or parts[2] != "heads":
        return None
    if parts[-1].endswith(".lock"):
        return None
    return "/".join(parts[3:])


def read_ref_sha(repo_root, branch):
    """Le o SHA apontado por um branch local. None se nao existir/ilegivel."""
    ref_path = os.path.join(repo_root, ".git", "refs", "heads", *branch.split("/"))
    try:
        with open(ref_path, encoding="ascii") as fh:
            return fh.read().strip()
    except OSError:
        return None


def get_commit_diff(repo_root, sha):
    """Retorna (assunto, diff) de um commit via `git show`, ou (None, None)."""
    try:
        result = subprocess.run(
            ["git", "show", "--format=%s", sha],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"  ! falha ao rodar git show {sha[:7]}: {exc}")
        return None, None
    if result.returncode != 0:
        log(f"  ! git show retornou {result.returncode}: {result.stderr.strip()}")
        return None, None
    subject, _, diff = result.stdout.partition("\n")
    return subject.strip(), diff


# Repos ja avisados de falha do `gh` (auth, rede) nesta execucao, para nao
# repetir o mesmo aviso a cada ciclo de polling (PR_POLL_SECONDS).
_gh_warned_repos = set()


def has_github_remote(repo_root):
    """True se o repositorio tiver um remote 'origin' hospedado no GitHub."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    return "github.com" in result.stdout.lower()


def list_open_prs(repo_root):
    """Lista PRs abertos do repo via `gh pr list`. None em caso de erro.

    O erro (auth, rede, gh ausente) e' logado uma unica vez por repo nesta
    execucao, para nao spammar o watcher.log a cada ciclo de polling.
    """
    repo_key = os.path.normcase(repo_root)
    try:
        result = subprocess.run(
            [GH_CMD, "pr", "list", "--state", "open",
             "--json", "number,title,headRefOid"],
            cwd=repo_root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=GH_TIMEOUT,
            creationflags=CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        if repo_key not in _gh_warned_repos:
            log(f"  ! comando '{GH_CMD}' nao encontrado no PATH. "
                f"Instale o GitHub CLI para revisar PRs.")
            _gh_warned_repos.add(repo_key)
        return None
    except subprocess.TimeoutExpired:
        return None

    if result.returncode != 0:
        if repo_key not in _gh_warned_repos:
            log(f"  ! gh pr list falhou em {project_name(repo_root)}: "
                f"{result.stderr.strip()[:200]}")
            _gh_warned_repos.add(repo_key)
        return None

    try:
        return json.loads(result.stdout)
    except ValueError:
        return None


def get_pr_diff(repo_root, number):
    """Retorna o diff de um PR via `gh pr diff`, ou None em erro."""
    try:
        result = subprocess.run(
            [GH_CMD, "pr", "diff", str(number)],
            cwd=repo_root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=GH_TIMEOUT,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"  ! falha ao rodar gh pr diff #{number}: {exc}")
        return None
    if result.returncode != 0:
        log(f"  ! gh pr diff #{number} retornou {result.returncode}: "
            f"{result.stderr.strip()[:200]}")
        return None
    return result.stdout


def is_relevant_file(path):
    """True se o arquivo deve disparar uma revisao."""
    name = os.path.basename(path)
    if name == REVIEW_LOG_NAME:  # evita loop: nosso proprio output
        return False
    if is_ignored_path(path):
        return False
    return os.path.splitext(name)[1].lower() in CODE_EXTENSIONS


def get_diff(repo_root, file_path):
    """Retorna o `git diff HEAD` do arquivo, ou string vazia."""
    rel = os.path.relpath(file_path, repo_root)
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD", "--", rel],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"  ! falha ao rodar git diff em {rel}: {exc}")
        return ""
    if result.returncode != 0:
        log(f"  ! git diff retornou {result.returncode}: {result.stderr.strip()}")
        return ""
    return result.stdout


def call_claude(repo_root, prompt):
    """Chama o Claude Code CLI com um prompt pronto. Retorna o texto ou None.

    Compartilhada pelas tres fontes de revisao (arquivo, commit, PR) — so o
    prompt muda entre elas.

    O prompt vai por stdin, nao como argumento `-p "<prompt>"`. Um commit ou
    PR grande facilmente passa dos ~8191 caracteres que o wrapper .cmd do
    Claude Code aceita numa linha de comando do Windows (limite do cmd.exe,
    bem menor que o MAX_DIFF_CHARS de 12000) — por stdin nao ha esse teto.
    """
    executable = shutil.which(CLAUDE_CMD) or CLAUDE_CMD
    try:
        result = subprocess.run(
            [executable, "-p"],
            cwd=repo_root,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CLAUDE_TIMEOUT,
            creationflags=CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        log(f"  ! comando '{CLAUDE_CMD}' nao encontrado no PATH. "
            f"Ajuste a constante CLAUDE_CMD no topo do script.")
        return None
    except subprocess.TimeoutExpired:
        log(f"  ! revisao excedeu {CLAUDE_TIMEOUT}s e foi abortada.")
        return None
    except OSError as exc:
        log(f"  ! erro ao executar o CLI: {exc}")
        return None

    if result.returncode != 0:
        log(f"  ! CLI retornou {result.returncode}: {result.stderr.strip()[:300]}")
        return None

    return result.stdout.strip() or None


def review_with_claude(repo_root, rel_path, diff):
    """Monta o prompt de arquivo nao commitado e chama o Claude."""
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n... (diff truncado)"
    prompt = PROMPT_TEMPLATE.format(path=rel_path, diff=diff)
    return call_claude(repo_root, prompt)


def review_commit_with_claude(repo_root, sha, subject, diff):
    """Monta o prompt de commit e chama o Claude."""
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n... (diff truncado)"
    prompt = PROMPT_TEMPLATE_COMMIT.format(sha=sha[:7], subject=subject, diff=diff)
    return call_claude(repo_root, prompt)


def review_pr_with_claude(repo_root, number, title, diff):
    """Monta o prompt de PR e chama o Claude."""
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n... (diff truncado)"
    prompt = PROMPT_TEMPLATE_PR.format(number=number, title=title, diff=diff)
    return call_claude(repo_root, prompt)


def retry_commit_review(repo_root, sha):
    """Forca a revisao de um commit especifico, sob pedido explicito do painel.

    Usado pelo botao "Revisar novamente" nos cards com falha (ex: erro/
    timeout do CLI). Ignora seen_commits de proposito — e uma acao pontual
    de um unico SHA, nao reabre o priming para revisar o historico em
    massa. Chamada diretamente pela GUI (fora do processo do watcher), por
    isso nao ha checagem de pausa aqui: um retry manual e sempre explicito.
    """
    project = project_name(repo_root)
    label = f"commit {sha[:7]}"

    subject, diff = get_commit_diff(repo_root, sha)
    if diff is None or not diff.strip():
        log(f"  ! retry {label}: sem diff ou 'git show' falhou.")
        emit_event("review_failed", project=project, file=label, source="commit")
        return False

    log(f"  > re-analisando {label} ({len(diff)} chars, retry manual)...")
    emit_event("review_start", project=project, file=label, source="commit",
               diff_chars=len(diff))

    started = time.time()
    review = review_commit_with_claude(repo_root, sha, subject, diff)
    elapsed = round(time.time() - started, 1)

    if review is None:
        log(f"  - {label}: revisao nao gerada (retry manual).")
        emit_event("review_failed", project=project, file=label,
                   source="commit", duration=elapsed)
        return False

    log_path = append_review(repo_root, label, review)
    log(f"  = revisao salva em {log_path} (retry manual)")
    emit_event("review_done", project=project, file=label, source="commit",
               review=review, duration=elapsed, log_path=log_path)
    return True


def append_review(repo_root, rel_path, review):
    """Anexa a revisao ao review-log.md na raiz do projeto."""
    log_path = os.path.join(repo_root, REVIEW_LOG_NAME)
    entry = (
        f"\n## {datetime.now():%Y-%m-%d %H:%M:%S} — `{rel_path}`\n\n"
        f"{review}\n"
    )
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(entry)
    return log_path


def process_file(repo_root, file_path):
    """Pipeline completo para um arquivo: diff -> revisao -> log."""
    rel_path = os.path.relpath(file_path, repo_root).replace(os.sep, "/")
    project = project_name(repo_root)

    control = read_control()
    if control["paused"] or project in control["paused_projects"]:
        log(f"  - {rel_path}: monitoramento pausado, ignorando.")
        return

    diff = get_diff(repo_root, file_path)
    if not diff.strip():
        log(f"  - {rel_path}: sem mudancas vs HEAD, ignorando.")
        return

    log(f"  > analisando {rel_path} ({len(diff)} chars de diff)...")
    emit_event("review_start", project=project, file=rel_path,
               diff_chars=len(diff))

    started = time.time()
    review = review_with_claude(repo_root, rel_path, diff)
    elapsed = round(time.time() - started, 1)

    if review is None:
        log(f"  - {rel_path}: revisao nao gerada.")
        emit_event("review_failed", project=project, file=rel_path,
                   duration=elapsed)
        return

    log_path = append_review(repo_root, rel_path, review)
    log(f"  = revisao salva em {log_path}")
    emit_event("review_done", project=project, file=rel_path,
               review=review, duration=elapsed, log_path=log_path)


def process_ref_update(repo_root, branch):
    """Pipeline completo para um commit novo: le o SHA -> git show -> revisao -> log.

    Limitacao conhecida: so olha o SHA atual do branch, nao uma lista de
    commits pendentes. Dois commits em sequencia rapida (dentro do debounce)
    resultam em uma unica revisao, a do mais recente — mesma simplificacao
    ja usada para arquivos. Um `git reset --hard` para um commit antigo
    tambem pode disparar uma "revisao" de um commit ja existente, se ele
    nunca tiver sido visto por este watcher.
    """
    project = project_name(repo_root)

    control = read_control()
    if control["paused"] or project in control["paused_projects"]:
        log(f"  - commit em {branch}: monitoramento pausado, ignorando.")
        return

    sha = read_ref_sha(repo_root, branch)
    if not sha:
        return

    repo_key = os.path.normcase(repo_root)
    seen = load_seen_commits()
    if sha in seen.get(repo_key, {}).get(branch, []):
        return  # ja revisado (ex: reset voltando para um commit conhecido)

    subject, diff = get_commit_diff(repo_root, sha)
    if diff is None:
        return  # git show falhou, ja logado

    if not diff.strip():
        log(f"  - commit {sha[:7]} em {branch}: sem diff, ignorando.")
        mark_commit_seen(repo_key, branch, sha)
        return

    label = f"commit {sha[:7]} ({branch})"
    log(f"  > analisando {label} ({len(diff)} chars)...")
    emit_event("review_start", project=project, file=label, source="commit",
               diff_chars=len(diff))

    started = time.time()
    review = review_commit_with_claude(repo_root, sha, subject, diff)
    elapsed = round(time.time() - started, 1)

    if review is None:
        log(f"  - {label}: revisao nao gerada.")
        emit_event("review_failed", project=project, file=label,
                   source="commit", duration=elapsed)
        return

    mark_commit_seen(repo_key, branch, sha)
    log_path = append_review(repo_root, label, review)
    log(f"  = revisao salva em {log_path}")
    emit_event("review_done", project=project, file=label, source="commit",
               review=review, duration=elapsed, log_path=log_path)


def process_pr(repo_root, pr):
    """Pipeline completo para um PR novo/atualizado: diff -> revisao -> log.

    So leitura: a revisao vai para o review-log.md e para o painel, nunca e
    postada de volta no GitHub.
    """
    project = project_name(repo_root)
    number = pr["number"]
    title = pr["title"]
    head_sha = pr["headRefOid"]

    control = read_control()
    if control["paused"] or project in control["paused_projects"]:
        return

    repo_key = os.path.normcase(repo_root)
    seen = load_seen_prs()
    if seen.get(repo_key, {}).get(str(number)) == head_sha:
        return  # ja revisado nesse estado (comentario/label nao conta)

    diff = get_pr_diff(repo_root, number)
    if diff is None:
        return

    if not diff.strip():
        mark_pr_seen(repo_key, number, head_sha)
        return

    label = f"PR #{number} — {title}"
    log(f"  > analisando {label} ({len(diff)} chars)...")
    emit_event("review_start", project=project, file=label, source="pr",
               diff_chars=len(diff))

    started = time.time()
    review = review_pr_with_claude(repo_root, number, title, diff)
    elapsed = round(time.time() - started, 1)

    if review is None:
        log(f"  - {label}: revisao nao gerada.")
        emit_event("review_failed", project=project, file=label,
                   source="pr", duration=elapsed)
        return

    mark_pr_seen(repo_key, number, head_sha)
    log_path = append_review(repo_root, label, review)
    log(f"  = revisao salva em {log_path}")
    emit_event("review_done", project=project, file=label, source="pr",
               review=review, duration=elapsed, log_path=log_path)


def github_poll_loop(get_dirs, stop_flag):
    """Thread separada: verifica PRs novos/atualizados a cada PR_POLL_SECONDS.

    Roda a parte, fora do watchdog/Debouncer, porque nao ha sinal local para
    "PR foi aberto" — precisa checar a rede periodicamente. Erros de rede ou
    autenticacao aqui nao afetam o monitoramento local de arquivos/commits.
    """
    while not stop_flag.is_set():
        control = read_control()
        if not control["paused"]:
            for repo_root in get_dirs():
                project = project_name(repo_root)
                if project in control["paused_projects"]:
                    continue
                if not has_github_remote(repo_root):
                    continue
                try:
                    prs = list_open_prs(repo_root)
                    if prs:
                        for pr in prs:
                            process_pr(repo_root, pr)
                except Exception as exc:  # um repo com problema nao para os outros
                    log(f"  ! erro inesperado verificando PRs de {project}: {exc}")
        stop_flag.wait(PR_POLL_SECONDS)  # retorna cedo se stop_flag for setado


class Debouncer:
    """Agenda tarefas para execucao apos DEBOUNCE_SECONDS de silencio.

    Generico o suficiente para as duas fontes de revisao (arquivo e commit):
    quem agenda passa a funcao a chamar e seus argumentos. As tarefas rodam
    por um unico worker, uma de cada vez, para nao disparar varias chamadas
    ao CLI em paralelo.
    """

    def __init__(self):
        self._timers = {}
        self._lock = threading.Lock()
        self._queue = queue.Queue()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def schedule(self, key, func, *args):
        with self._lock:
            existing = self._timers.get(key)
            if existing:
                existing.cancel()
            timer = threading.Timer(DEBOUNCE_SECONDS, self._fire, args=(key, func, args))
            timer.daemon = True
            self._timers[key] = timer
            timer.start()

    def _fire(self, key, func, args):
        with self._lock:
            self._timers.pop(key, None)
        self._queue.put((func, args))

    def _run(self):
        while True:
            func, args = self._queue.get()
            try:
                func(*args)
            except Exception as exc:  # nunca deixa o worker morrer
                log(f"  ! erro inesperado em {func.__name__}{args}: {exc}")
            finally:
                self._queue.task_done()

    def wait_idle(self):
        """Bloqueia ate a fila esvaziar (usado pelo modo --once de teste)."""
        self._queue.join()


class CodeChangeHandler(FileSystemEventHandler):
    def __init__(self, repo_root, debouncer):
        self.repo_root = repo_root
        self.debouncer = debouncer

    def _dispatch(self, path):
        branch = parse_ref_branch(self.repo_root, path)
        if branch is not None:
            key = f"commit:{os.path.normcase(self.repo_root)}:{branch}"
            self.debouncer.schedule(key, process_ref_update, self.repo_root, branch)
            return
        if not is_relevant_file(path):
            return
        key = os.path.normcase(os.path.abspath(path))
        self.debouncer.schedule(key, process_file, self.repo_root, path)

    def _handle(self, event):
        if event.is_directory:
            return
        self._dispatch(event.src_path)

    on_modified = _handle
    on_created = _handle

    def on_moved(self, event):
        if event.is_directory:
            return
        self._dispatch(event.dest_path)


def validate_dirs():
    """Filtra a lista viva, mantendo apenas repositorios git existentes."""
    valid = []
    for raw in load_watched_dirs():
        path = os.path.abspath(raw)
        if not os.path.isdir(path):
            log(f"! pasta inexistente, ignorada: {path}")
            continue
        if not is_git_repo(path):
            log(f"! nao e um repositorio git, ignorada: {path}")
            continue
        valid.append(path)
    return valid


def main(stop_event=None):
    """Loop principal do watcher.

    `stop_event` (threading.Event opcional) permite encerrar cooperativamente
    quando este main() roda numa thread da GUI, em vez de como processo
    proprio — nesse caso nao ha KeyboardInterrupt para capturar. Rodando
    `python code_watcher.py` direto no terminal, sem stop_event, o
    comportamento de sempre (Ctrl+C) continua igual.
    """
    if stop_event is None:
        stop_event = threading.Event()

    if shutil.which(CLAUDE_CMD) is None:
        log(f"! aviso: '{CLAUDE_CMD}' nao foi encontrado no PATH. "
            f"As revisoes vao falhar ate voce ajustar CLAUDE_CMD.")
    if shutil.which(GH_CMD) is None:
        log(f"! aviso: '{GH_CMD}' nao foi encontrado no PATH. "
            f"Revisao de PRs desativada ate instalar o GitHub CLI.")

    dirs = validate_dirs()
    if not dirs:
        log("Nenhuma pasta valida em WATCHED_DIRS. Edite a lista no topo do script.")
        return 1

    prime_seen_commits(dirs)

    debouncer = Debouncer()
    observer = Observer()
    for path in dirs:
        observer.schedule(CodeChangeHandler(path, debouncer), path, recursive=True)
        log(f"monitorando: {path}")

    log(f"debounce={DEBOUNCE_SECONDS}s | timeout do CLI={CLAUDE_TIMEOUT}s | "
        f"{len(CODE_EXTENSIONS)} extensoes | polling de PRs a cada "
        f"{PR_POLL_SECONDS}s | Ctrl+C para sair")

    emit_event("started", projects=[project_name(d) for d in dirs],
               dirs=dirs, pid=os.getpid())

    observer.start()

    pr_stop_flag = threading.Event()
    pr_thread = threading.Thread(
        target=github_poll_loop, args=(validate_dirs, pr_stop_flag), daemon=True
    )
    pr_thread.start()

    try:
        while not stop_event.wait(1):
            pass
    except KeyboardInterrupt:
        pass
    log("encerrando...")
    observer.stop()
    pr_stop_flag.set()
    emit_event("stopped")
    observer.join()
    return 0


if __name__ == "__main__":
    sys.exit(main())
