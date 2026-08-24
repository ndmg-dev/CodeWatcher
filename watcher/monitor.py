import os
import queue
import shutil
import sys
import threading

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .config import (
    CLAUDE_CMD, CLAUDE_TIMEOUT, CODE_EXTENSIONS, DEBOUNCE_SECONDS,
    GH_CMD, PR_POLL_SECONDS, read_control, load_watched_dirs,
)
from .git import (
    is_git_repo, is_relevant_file, parse_ref_branch, project_name,
    has_github_remote, list_open_prs, prime_seen_commits,
)
from .logger import log, emit_event
from .review import process_file, process_ref_update, process_pr


def github_poll_loop(get_dirs, stop_flag):
    """Thread separada: verifica PRs novos/atualizados a cada PR_POLL_SECONDS."""
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
                except Exception as exc:
                    log(f"  ! erro inesperado verificando PRs de {project}: {exc}")
        stop_flag.wait(PR_POLL_SECONDS)


class Debouncer:
    """Agenda tarefas para execucao apos DEBOUNCE_SECONDS de silencio."""

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
            except Exception as exc:
                log(f"  ! erro inesperado em {func.__name__}{args}: {exc}")
            finally:
                self._queue.task_done()

    def wait_idle(self):
        """Bloqueia ate a fila esvaziar (usado pelo modo --once de teste)."""
        self._queue.join()


class CodeModifiedHandler(FileSystemEventHandler):
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
    quando este main() roda numa thread da GUI.
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
        observer.schedule(CodeModifiedHandler(path, debouncer), path, recursive=True)
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
