"""App principal: pywebview, ponte JS-Bridge, tray icon e ciclo de vida."""

import os
import sys
import threading
import time
from datetime import datetime

import webview

from ..config import (
    STATE_DIR, read_control, write_control, load_watched_dirs, save_watched_dirs,
    snooze_pause,
)
from ..git import is_git_repo, discover_git_repos, project_name
from ..logger import log, emit_event
from ..ask import ask_history as _ask_history
from ..monitor import main as watcher_main
from ..patterns import detect_patterns as _detect_patterns
from ..review import retry_commit_review, _allow_review
from .backlog_store import set_backlog_status, reopen_backlog_item as _reopen_backlog_item
from .state import WatcherState, tail_events
from .tray import setup_tray

# Em dev, HERE e a pasta do script. Empacotado com PyInstaller --onefile,
# o script roda de uma pasta temporaria (sys._MEIPASS) e e la que
# `--add-data` deposita o ui.html — por isso a prioridade a _MEIPASS.
HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
# Subimos dois niveis: watcher/gui/ -> watcher/ -> raiz do projeto
_PROJECT_ROOT = os.path.dirname(os.path.dirname(HERE))
UI_FILE = os.path.join(_PROJECT_ROOT, "ui.html")
# Em empacotamento PyInstaller, _MEIPASS ja e a raiz com ui.html
if not os.path.isfile(UI_FILE):
    UI_FILE = os.path.join(HERE, "ui.html")
ICON_FILE = os.path.join(_PROJECT_ROOT, "icon.ico")
if not os.path.isfile(ICON_FILE):
    ICON_FILE = os.path.join(HERE, "icon.ico")


class App:
    def __init__(self):
        self.state = WatcherState()
        self.stop_flag = threading.Event()
        self.watcher_thread = None
        self.watcher_stop_event = None
        self.window = None
        self.tray = None
        self._win_pos = None

    # -- thread do watcher ----------------------------------------------------

    def start_watcher(self):
        os.makedirs(STATE_DIR, exist_ok=True)
        log(f"=== watcher iniciado pela GUI em {datetime.now()} ===")
        self.watcher_stop_event = threading.Event()
        self.watcher_thread = threading.Thread(
            target=watcher_main, args=(self.watcher_stop_event,), daemon=True
        )
        self.watcher_thread.start()

    def watcher_alive(self):
        return self.watcher_thread is not None and self.watcher_thread.is_alive()

    def restart_watcher(self):
        log_restart = self.state.reviewing
        self.stop_watcher()
        self.start_watcher()
        if log_restart:
            emit_event("review_failed", project="—",
                       file="(revisao interrompida por mudanca de pastas)")

    def stop_watcher(self):
        if self.watcher_alive():
            self.watcher_stop_event.set()
            self.watcher_thread.join(timeout=8)

    # -- API exposta ao JavaScript ------------------------------------------

    def get_state(self):
        return self.state.snapshot(self.watcher_alive())

    def toggle_master(self):
        control = read_control()
        write_control(paused=not control["paused"])
        if self.tray:
            self.tray.refresh()
        return True

    def snooze(self, minutes):
        """Pausa o monitoramento geral por N minutos, com retomada automatica
        (soneca) — diferente de toggle_master, que pausa indefinidamente."""
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            return {"ok": False, "msg": "Duracao invalida."}
        if minutes <= 0:
            return {"ok": False, "msg": "Duracao invalida."}
        control = snooze_pause(minutes)
        if self.tray:
            self.tray.refresh()
        return {"ok": True, "msg": f"Monitoramento pausado por {minutes} min.",
                "paused_until": control["paused_until"]}

    def save_settings(self, provider, api_key, model, max_reviews_per_hour, notify_severity):
        write_control(
            llm_provider=provider, openai_api_key=api_key, openai_model=model,
            max_reviews_per_hour=max_reviews_per_hour, notify_severity=notify_severity,
        )
        return True

    def add_project(self):
        try:
            picked = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception as exc:
            return {"ok": False, "msg": f"Não foi possível abrir o seletor: {exc}"}
        if not picked:
            return {"ok": False, "msg": ""}

        path = os.path.abspath(picked[0])

        if not os.path.isdir(path):
            return {"ok": False, "msg": "Pasta não encontrada."}
        if not is_git_repo(path):
            return {"ok": False,
                    "msg": f"'{os.path.basename(path)}' não é um repositório git "
                           f"(sem pasta .git). O watcher precisa do git para "
                           f"calcular o diff."}

        dirs = load_watched_dirs()
        if any(os.path.normcase(d) == os.path.normcase(path) for d in dirs):
            return {"ok": False, "msg": "Essa pasta já está sendo monitorada."}

        dirs.append(path)
        save_watched_dirs(dirs)
        self.restart_watcher()
        return {"ok": True,
                "msg": f"'{project_name(path)}' adicionado e sendo monitorado."}

    def scan_for_repos(self):
        try:
            picked = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception as exc:
            return {"ok": False, "msg": f"Não foi possível abrir o seletor: {exc}",
                    "repos": []}
        if not picked:
            return {"ok": False, "msg": "", "repos": []}

        root = os.path.abspath(picked[0])
        if not os.path.isdir(root):
            return {"ok": False, "msg": "Pasta não encontrada.", "repos": []}

        found = discover_git_repos(root)
        if not found:
            return {"ok": False,
                    "msg": f"Nenhum repositório git encontrado dentro de "
                           f"'{os.path.basename(root)}'.",
                    "repos": []}

        current = {os.path.normcase(d) for d in load_watched_dirs()}
        repos = [{"path": p, "name": project_name(p),
                  "already": os.path.normcase(p) in current}
                 for p in sorted(found, key=str.lower)]
        return {"ok": True,
                "msg": f"{len(found)} repositório(s) encontrado(s) em "
                       f"'{os.path.basename(root)}'.",
                "repos": repos}

    def add_projects_bulk(self, paths):
        dirs = load_watched_dirs()
        current = {os.path.normcase(d) for d in dirs}
        added, skipped = [], []

        for raw in paths:
            path = os.path.abspath(raw)
            if os.path.normcase(path) in current:
                continue
            if not is_git_repo(path):
                skipped.append(project_name(path))
                continue
            dirs.append(path)
            current.add(os.path.normcase(path))
            added.append(project_name(path))

        if added:
            save_watched_dirs(dirs)
            self.restart_watcher()

        if not added and not skipped:
            return {"ok": False, "msg": "Nenhuma pasta nova para adicionar."}

        parts = []
        if added:
            parts.append(f"{len(added)} adicionado(s): {', '.join(added)}.")
        if skipped:
            parts.append(f"{len(skipped)} ignorado(s) (não são repos git): "
                         f"{', '.join(skipped)}.")
        return {"ok": bool(added), "msg": " ".join(parts)}

    def retry_commit(self, project, sha):
        if not sha:
            return {"ok": False, "msg": "Commit inválido."}
        path = next((p["path"] for p in self.state.current_projects()
                     if p["name"] == project), None)
        if not path:
            return {"ok": False, "msg": f"Projeto '{project}' não encontrado."}

        threading.Thread(target=retry_commit_review, args=(path, sha),
                         daemon=True).start()
        return {"ok": True, "msg": f"Revisando {sha[:7]} novamente..."}

    def remove_project(self, path):
        dirs = load_watched_dirs()
        kept = [d for d in dirs
                if os.path.normcase(d) != os.path.normcase(os.path.abspath(path))]
        if len(kept) == len(dirs):
            return {"ok": False, "msg": "Pasta não estava na lista."}
        save_watched_dirs(kept)
        self.restart_watcher()
        return {"ok": True,
                "msg": f"'{project_name(path)}' removido do monitoramento. "
                       f"O review-log.md dele continua no disco."}

    def toggle_project(self, name):
        control = read_control()
        paused = set(control["paused_projects"])
        paused.discard(name) if name in paused else paused.add(name)
        write_control(paused_projects=paused)
        return True

    # -- pergunte ao historico ---------------------------------------------------

    def ask_history(self, project, question):
        """Chamada sincrona (bloqueia a thread da ponte JS enquanto o LLM
        responde) -- mesma classe de chamada que retry_commit ja faz. Reusa
        o rate limit das revisoes normais (_allow_review): e a mesma conta
        de LLM, o mesmo teto de custo deve valer aqui tambem."""
        question = (question or "").strip()
        if not question:
            return {"ok": False, "msg": "Escreva uma pergunta."}
        repo_root = next(
            (p for p in load_watched_dirs() if project_name(p) == project), None
        )
        if not repo_root:
            return {"ok": False, "msg": f"Projeto '{project}' não encontrado."}
        if not _allow_review():
            return {"ok": False, "msg": "Limite de chamadas ao LLM atingido nesta hora. Tente de novo mais tarde."}
        answer, cost_usd, error = _ask_history(repo_root, question)
        if error:
            return {"ok": False, "msg": error}
        emit_event("history_query", project=project, question=question, cost_usd=cost_usd)
        return {"ok": True, "answer": answer}

    # -- padroes repetidos entre projetos ----------------------------------------

    def detect_patterns(self):
        """Analisa os itens ABERTOS do backlog (todos os projetos) em busca
        de achados que se repetem em mais de um -- reusa a mesma lista que
        alimenta a aba Backlog do painel, nao uma fonte de dados separada."""
        state = self.state.snapshot(self.watcher_alive())
        items = [i for i in state["backlog"] if i["status"] == "open"]
        if len(items) < 2:
            return {"ok": False, "msg": "Poucos itens pendentes no backlog ainda para detectar padrões (precisa de pelo menos 2)."}
        if not _allow_review():
            return {"ok": False, "msg": "Limite de chamadas ao LLM atingido nesta hora. Tente de novo mais tarde."}
        answer, cost_usd, error = _detect_patterns(items)
        if error:
            return {"ok": False, "msg": error}
        emit_event("history_query", project="(todos os projetos)",
                   question="detectar padrões repetidos entre projetos", cost_usd=cost_usd)
        return {"ok": True, "answer": answer}

    # -- backlog ---------------------------------------------------------------

    def resolve_backlog_item(self, item_id):
        set_backlog_status(item_id, "done", datetime.now().isoformat(timespec="seconds"))
        return True

    def dismiss_backlog_item(self, item_id):
        set_backlog_status(item_id, "dismissed", datetime.now().isoformat(timespec="seconds"))
        return True

    def reopen_backlog_item(self, item_id):
        _reopen_backlog_item(item_id)
        return True

    # -- janela --------------------------------------------------------------

    def show_window(self, *_):
        if not self.window:
            return
        # NAO chamar self.window.show()/self.window.restore() (API do pywebview)
        # aqui: elas fazem Invoke sincrono na thread da UI do WinForms, e essa
        # thread pode ainda estar ocupada inicializando o WebView2 quando esta
        # funcao roda (ela e chamada de uma thread de fundo, 2s apos a criacao
        # da janela) — mesma classe de problema que o _force_icon ja teve com
        # SendMessageW (sincrono) travando a janela de verdade. Ir direto no
        # Win32 (ShowWindow + SetWindowPos com SWP_SHOWWINDOW) evita esse
        # deadlock porque nao depende do loop de mensagens do WinForms.
        pos = self._win_pos if self._win_pos is not None else (None, None)
        self._force_show_at(*pos)

    def _force_show_at(self, x, y):
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, "Code Watcher")
            if not hwnd:
                log("  ! _force_show_at: janela 'Code Watcher' nao encontrada")
                return
            SW_RESTORE = 9
            SWP_NOSIZE, SWP_NOZORDER, SWP_SHOWWINDOW, SWP_NOMOVE = 0x0001, 0x0004, 0x0040, 0x0002
            user32.ShowWindow(hwnd, SW_RESTORE)
            flags = SWP_NOSIZE | SWP_NOZORDER | SWP_SHOWWINDOW
            moved = x is not None and y is not None
            if not moved:
                flags |= SWP_NOMOVE
                x = y = 0
            user32.SetWindowPos(hwnd, None, x, y, 0, 0, flags)
            user32.SetForegroundWindow(hwnd)
            self._force_icon(hwnd)
            if moved:
                log(f"  = janela restaurada e movida para ({x}, {y}) via Win32")
            else:
                log("  = janela restaurada via Win32")
        except Exception as exc:
            log(f"  ! _force_show_at falhou: {exc}")

    def _force_icon(self, hwnd):
        if getattr(self, "_icon_forced", False) or not os.path.isfile(ICON_FILE):
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40
            WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1

            hicon_big = user32.LoadImageW(
                None, ICON_FILE, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE
            )
            hicon_small = user32.LoadImageW(
                None, ICON_FILE, IMAGE_ICON, 16, 16, LR_LOADFROMFILE
            )
            if hicon_big:
                user32.PostMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)
                set_class_long = getattr(user32, "SetClassLongPtrW", user32.SetClassLongW)
                GCLP_HICON = -14
                set_class_long(hwnd, GCLP_HICON, hicon_big)
            if hicon_small:
                user32.PostMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)
                GCLP_HICONSM = -34
                set_class_long = getattr(user32, "SetClassLongPtrW", user32.SetClassLongW)
                set_class_long(hwnd, GCLP_HICONSM, hicon_small)
            self._icon_forced = True
        except Exception as exc:
            log(f"  ! _force_icon falhou: {exc}")

    def _window_is_visible(self):
        """True se a janela existe, esta visivel (nao escondida na bandeja)
        e nao minimizada. Usado para nao notificar quando o usuario ja esta
        de olho no painel."""
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, "Code Watcher")
            if not hwnd:
                return False
            return bool(user32.IsWindowVisible(hwnd)) and not bool(user32.IsIconic(hwnd))
        except Exception as exc:
            log(f"  ! _window_is_visible falhou: {exc}")
            return False

    def on_closing(self):
        self.window.hide()
        return False

    def quit(self, *_):
        self.stop_flag.set()
        self.stop_watcher()
        if self.tray:
            self.tray.stop()
        if self.window:
            self.window.destroy()

    # -- notificacoes ---------------------------------------------------------

    def _notify_if_critical(self, event):
        """Chamado para cada evento novo (nao historico) — mostra um balao da
        bandeja quando uma revisao atinge o limiar configurado
        (Configuracoes > Notificar em), ja que o painel normalmente fica
        minimizado na bandeja e um bug real pode passar despercebido."""
        etype = event.get("type")
        is_secret = etype == "secret_found"
        if etype != "review_done" and not is_secret:
            return
        if not is_secret:
            # Segredo exposto sempre notifica, sem depender do limiar
            # configurado (Configuracoes > Notificar em) — e sempre grave.
            severity = event.get("severity")
            threshold = read_control().get("notify_severity", "alta")
            should_notify = (
                (threshold == "alta" and severity == "alta")
                or (threshold == "media" and severity in ("alta", "media"))
            )
            if not should_notify:
                return
        if not self.tray:
            return
        if self._window_is_visible():
            return
        project = event.get("project", "?")
        file_ = event.get("file", "?")
        try:
            if is_secret:
                findings = event.get("findings") or []
                kinds = ", ".join(sorted({f.get("kind", "?") for f in findings}))
                self.tray.notify(
                    f"{project} — {file_}\n{kinds}\nAbra o painel para ver os detalhes.",
                    "Code Watcher: possivel segredo exposto",
                )
            else:
                self.tray.notify(
                    f"{project} — {file_}\nAbra o painel para ver os detalhes.",
                    "Code Watcher: achado critico",
                )
        except Exception as exc:
            log(f"  ! notificacao de achado critico falhou: {exc}")

    # -- ciclo de vida -------------------------------------------------------

    def run(self):
        write_control(paused=False)
        self.start_watcher()

        threading.Thread(
            target=tail_events,
            args=(self.state, self.stop_flag, self._notify_if_critical),
            daemon=True,
        ).start()

        self.tray = setup_tray(self)

        win_w, win_h = 1180, 760
        primary_screen = next(
            (s for s in webview.screens if s.x == 0 and s.y == 0), None
        )
        log(f"  telas: {webview.screens} | primario: {primary_screen}")
        if primary_screen is not None:
            win_x = max(0, (primary_screen.width - win_w) // 2)
            win_y = max(0, (primary_screen.height - win_h) // 2)
            self._win_pos = (win_x, win_y)
        else:
            win_x = win_y = None

        self.window = webview.create_window(
            "Code Watcher", UI_FILE, js_api=self,
            width=win_w, height=win_h, x=win_x, y=win_y, screen=primary_screen,
            min_size=(900, 600), hidden=True,
        )
        self.window.events.closing += self.on_closing

        if "--show" in sys.argv:
            def show_soon():
                time.sleep(2)
                self.show_window()
            threading.Thread(target=show_soon, daemon=True).start()

        try:
            webview.start(icon=ICON_FILE if os.path.isfile(ICON_FILE) else None)
        finally:
            self.stop_flag.set()
            self.stop_watcher()


def run_gui():
    if not os.path.exists(UI_FILE):
        print(f"ui.html nao encontrado em {UI_FILE}", file=sys.stderr)
        return 1
    if sys.platform == "win32":
        # Sem isso, o Windows atribui as notificacoes de bandeja ao
        # executavel real do processo (pythonw.exe), entao o toast aparece
        # com o cabecalho "Python" em vez de "Code Watcher".
        import ctypes
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "ndmg-dev.CodeWatcher"
            )
        except Exception:
            pass
    App().run()
    return 0


# Compatibilidade com chamadas que importavam o nome historico.
main = run_gui
