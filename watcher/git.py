import hashlib
import json
import os
import subprocess
import threading

from .config import (
    CREATE_NO_WINDOW, GH_CMD, GH_TIMEOUT, IGNORED_DIRS,
    CODE_EXTENSIONS, REVIEW_LOG_NAME, STATE_DIR,
    SEEN_COMMITS_FILE, SEEN_PRS_FILE, SEEN_DIFF_HASHES_FILE,
)
from .logger import log


_seen_commits_lock = threading.Lock()
_seen_diff_hashes_lock = threading.Lock()
_gh_warned_repos = set()


def is_git_repo(path):
    """True se o caminho for a raiz de um repositorio git."""
    return os.path.isdir(os.path.join(path, ".git"))


def discover_git_repos(root, max_depth=5):
    """Varre 'root' recursivamente procurando repositorios git.

    Nao desce dentro de um repositorio ja encontrado (evita listar
    submodulos como repos separados) nem dentro de IGNORED_DIRS/pastas
    ocultas. max_depth limita o quao fundo a varredura vai.
    """
    root = os.path.abspath(root)
    found = []

    def walk(path, depth):
        if is_git_repo(path):
            found.append(path)
            return
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


def is_relevant_file(path):
    """True se o arquivo deve disparar uma revisao."""
    name = os.path.basename(path)
    if name == REVIEW_LOG_NAME:
        return False
    if is_ignored_path(path):
        return False
    return os.path.splitext(name)[1].lower() in CODE_EXTENSIONS


def parse_ref_branch(repo_root, path):
    """Se 'path' for o arquivo de ref de um branch local, retorna o nome do
    branch (ex: 'main', 'feature/x'). Caso contrario, None.
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
    """Lista PRs abertos do repo via `gh pr list`. None em caso de erro."""
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


# --- Controle de commits/PRs ja vistos ------------------------------------

def load_seen_commits():
    try:
        with open(SEEN_COMMITS_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def mark_commit_seen(repo_key, branch, sha, keep_last=50):
    """Registra um commit como revisado."""
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


def prime_seen_commits(dirs):
    """Registra os commits atuais de cada repo NOVO como 'ja vistos'."""
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


# --- Deduplicacao de diffs identicos ---------------------------------------

def diff_fingerprint(diff):
    """Hash estavel do diff, normalizando so quebras de linha e espaco em
    branco supérfluo (nao a indentacao do codigo em si) — pega o caso comum
    de 'mesma mudanca de novo' (amend so de mensagem, rebase sem conflito,
    CRLF/LF) sem esconder uma mudanca real de conteudo por engano."""
    normalized = diff.replace("\r\n", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()


def load_seen_diff_hashes():
    try:
        with open(SEEN_DIFF_HASHES_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def is_duplicate_diff(repo_key, kind, key, fingerprint):
    """True se esse (repo, tipo, chave) ja foi revisado com o mesmo diff
    (ex: mesmo arquivo, mesmo branch, mesmo numero de PR)."""
    seen = load_seen_diff_hashes()
    return seen.get(repo_key, {}).get(f"{kind}:{key}") == fingerprint


def mark_diff_hash(repo_key, kind, key, fingerprint):
    with _seen_diff_hashes_lock:
        seen = load_seen_diff_hashes()
        seen.setdefault(repo_key, {})[f"{kind}:{key}"] = fingerprint
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = SEEN_DIFF_HASHES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(seen, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, SEEN_DIFF_HASHES_FILE)
