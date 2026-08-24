"""
watcher_gui.py — tray icon + painel visual para o code_watcher.py.

DECISAO TECNICA: pywebview (HTML/CSS/JS) em vez de customtkinter.
-----------------------------------------------------------------
Motivo principal e manutenibilidade do conteudo exibido. O produto do
watcher e a resposta do Claude Code CLI, que vem em Markdown: titulos,
listas, negrito e blocos de codigo. Em customtkinter isso exigiria montar
tags manualmente num widget Text (calcular offsets de caractere, aplicar
estilo por intervalo) — codigo verboso, fragil e que quebra a cada mudanca
de formato da resposta. Em HTML/CSS o mesmo resultado sai com um
renderizador de ~40 linhas e o layout inteiro vira CSS, que qualquer um
ajusta sem entender a arquitetura do app.

Fatores secundarios que confirmaram a escolha:
  - O WebView2 ja vem instalado no Windows 11, entao nao ha runtime extra.
  - Separar ui.html de watcher_gui.py isola "aparencia" de "logica":
    mexer no visual nao arrisca a gestao do processo do watcher.
  - Scroll de lista longa, layout responsivo e animacoes sao nativos do
    navegador; em Tk exigiriam Canvas + scrollbar manual.

Custo aceito: uma dependencia a mais (pywebview) e a ponte Python<->JS.
A ponte foi mantida minima de proposito — o JS chama tres funcoes
(get_state, toggle_master, toggle_project) e faz polling de 1s. Sem
push, sem estado duplicado nos dois lados.

ARQUITETURA
-----------
  watcher_gui.py (este arquivo)
    |- roda code_watcher.main() numa thread propria (nao mais subprocesso —
    |  ver nota de empacotamento abaixo)
    |- le %LOCALAPPDATA%\\CodeWatcher\\events.jsonl (append-only)
    |- escreve %LOCALAPPDATA%\\CodeWatcher\\control.json (pausa)
    |- tray icon (pystray, thread separada)
    +- janela (pywebview, thread principal — exigencia do webview)

O code_watcher.py continua funcionando sozinho no terminal; os eventos
sao um extra opcional. O review-log.md por projeto segue sendo gerado
normalmente.

EMPACOTAMENTO (.exe via PyInstaller)
-------------------------------------
O watcher rodava como subprocesso (`sys.executable code_watcher.py`) so
para nao travar a janela do webview. Isso parava de funcionar num exe
--onefile: nao ha um `code_watcher.py` solto ao lado do exe para apontar,
e reempacotar dois exes (GUI + watcher) so pra manter dois processos era
complexidade sem beneficio real. Trocado por uma thread dentro do mesmo
processo (`code_watcher.main(stop_event=...)`), que e cooperativa (checa
o stop_event em vez de depender de KeyboardInterrupt/terminate). Efeito
colateral bom: nao precisa mais de Job Object pra matar um processo orfao
se a GUI cair — a thread morre junto com o processo, sempre.

Uso:  pythonw watcher_gui.py     (dev, inicia minimizado na bandeja)
      CodeWatcher.exe            (empacotado, ver build_exe.ps1)
"""

import json
import os
import sys
import threading
import time
from datetime import datetime

import pystray
import webview
from PIL import Image, ImageDraw

import code_watcher as cw

# Em dev, HERE e a pasta do script. Empacotado com PyInstaller --onefile,
# o script roda de uma pasta temporaria (sys._MEIPASS) e e la que
# `--add-data` deposita o ui.html — por isso a prioridade a _MEIPASS.
HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
UI_FILE = os.path.join(HERE, "ui.html")

# Quantos cards manter no feed da janela.
FEED_LIMIT = 60


# ---------------------------------------------------------------------------
# Estado compartilhado
# ---------------------------------------------------------------------------

class WatcherState:
    """Agrega os eventos do watcher e o estado de pausa.

    Fonte da verdade: events.jsonl (historico) + control.json (pausa).
    Esta classe so mantem um cache em memoria para a janela consultar.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.started_at = time.time()
        self.feed = []               # cards mais recentes primeiro
        self.total_count = 0         # revisoes historicas (todo o events.jsonl)
        self.session_count = 0       # revisoes desde que a GUI abriu
        self.review_seconds = 0.0    # tempo somado das revisoes da sessao
        self.per_project = {}        # nome -> total historico
        self.reviewing = False
        self._seq = 0

    @staticmethod
    def current_projects():
        """Lista viva de pastas, relida a cada consulta.

        Relemos do disco em vez de guardar em memoria para que uma pasta
        adicionada pelo painel apareca na hora, sem reiniciar a GUI.
        """
        return [{"name": cw.project_name(p), "path": p,
                 "exists": os.path.isdir(p), "is_git": cw.is_git_repo(p)}
                for p in cw.load_watched_dirs()]

    # -- consumo de eventos --------------------------------------------------

    def apply(self, event, historical):
        """Aplica um evento ao estado. historical=True para o backlog do arquivo."""
        etype = event.get("type")
        with self.lock:
            if etype == "review_start":
                self.reviewing = True
                if not historical:
                    self._push({
                        "id": self._next_id(), "status": "running",
                        "project": event.get("project", "?"),
                        "file": event.get("file", "?"),
                        "source": event.get("source", "file"),
                        "ts": event.get("ts", ""), "duration": None,
                        "review": "",
                    })

            elif etype == "review_done":
                self.reviewing = False
                self.total_count += 1
                project = event.get("project", "?")
                self.per_project[project] = self.per_project.get(project, 0) + 1
                if not historical:
                    self.session_count += 1
                    self.review_seconds += float(event.get("duration") or 0)
                # O card entra no feed tanto pro backlog quanto ao vivo — so
                # as estatisticas de sessao (acima) ficam restritas ao "nao
                # historical". Sem isso, reiniciar o painel esvaziava o feed
                # visivel mas mantinha o contador da pasta, o que parecia
                # inconsistente ("mostra 1, mas diz que nao tem nenhuma").
                self._resolve(project, event.get("file"), {
                    "status": "done",
                    "source": event.get("source", "file"),
                    "review": event.get("review", ""),
                    "duration": event.get("duration"),
                    "ts": event.get("ts", ""),
                })

            elif etype == "review_failed":
                self.reviewing = False
                self._resolve(event.get("project", "?"), event.get("file"), {
                    "status": "failed",
                    "source": event.get("source", "file"),
                    "duration": event.get("duration"),
                    "ts": event.get("ts", ""),
                })

            elif etype in ("started", "stopped"):
                self.reviewing = False

    def _next_id(self):
        self._seq += 1
        return self._seq

    def _push(self, card):
        self.feed.insert(0, card)
        del self.feed[FEED_LIMIT:]

    def _resolve(self, project, file, updates):
        """Atualiza o card 'running' correspondente, ou cria um se nao houver."""
        for card in self.feed:
            if (card["status"] == "running"
                    and card["project"] == project and card["file"] == file):
                card.update(updates)
                return
        card = {"id": self._next_id(), "project": project, "file": file,
                "source": "file", "review": "", "duration": None, "ts": ""}
        card.update(updates)
        self._push(card)

    # -- leitura pela janela -------------------------------------------------

    def snapshot(self, watcher_alive):
        control = cw.read_control()
        paused = control["paused"]
        paused_projects = set(control["paused_projects"])
        projects = self.current_projects()
        with self.lock:
            return {
                "uptime": time.time() - self.started_at,
                "paused": paused,
                "reviewing": self.reviewing,
                "watcher_alive": watcher_alive,
                "session_count": self.session_count,
                "total_count": self.total_count,
                "review_seconds": self.review_seconds,
                "projects": [
                    {"name": p["name"], "path": p["path"],
                     "exists": p["exists"], "is_git": p["is_git"],
                     "paused": p["name"] in paused_projects,
                     "count": self.per_project.get(p["name"], 0)}
                    for p in projects
                ],
                "feed": list(self.feed),
            }


# ---------------------------------------------------------------------------
# Controle de pausa (control.json)
# ---------------------------------------------------------------------------

def write_control(paused=None, paused_projects=None):
    control = cw.read_control()
    if paused is not None:
        control["paused"] = paused
    if paused_projects is not None:
        control["paused_projects"] = sorted(paused_projects)
    os.makedirs(cw.STATE_DIR, exist_ok=True)
    tmp = cw.CONTROL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(control, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, cw.CONTROL_FILE)
    return control


# ---------------------------------------------------------------------------
# Leitura do arquivo de eventos
# ---------------------------------------------------------------------------

def tail_events(state, stop_flag):
    """Le o historico e depois acompanha o arquivo de eventos em tempo real."""
    offset = 0
    if os.path.exists(cw.EVENTS_FILE):
        with open(cw.EVENTS_FILE, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        state.apply(json.loads(line), historical=True)
                    except ValueError:
                        pass
            offset = fh.tell()

    while not stop_flag.is_set():
        try:
            size = os.path.getsize(cw.EVENTS_FILE)
        except OSError:
            time.sleep(0.5)
            continue
        if size < offset:      # arquivo rotacionado/apagado
            offset = 0
        if size > offset:
            with open(cw.EVENTS_FILE, encoding="utf-8", errors="replace") as fh:
                fh.seek(offset)
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            state.apply(json.loads(line), historical=False)
                        except ValueError:
                            pass
                offset = fh.tell()
        time.sleep(0.4)


# ---------------------------------------------------------------------------
# Icone da bandeja
# ---------------------------------------------------------------------------

def make_icon_image(color, badge=None):
    """Desenha o icone: um 'olho' estilizado, colorido conforme o estado."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([4, 4, 60, 60], fill="#171a21", outline=color, width=4)
    d.ellipse([22, 22, 42, 42], fill=color)
    if badge:
        d.ellipse([42, 42, 62, 62], fill=badge, outline="#0f1115", width=3)
    return img


ICON_IDLE = None    # preenchidos em setup_tray (evita custo no import)
ICON_BUSY = None
ICON_PAUSED = None


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class App:
    def __init__(self):
        self.state = WatcherState()
        self.stop_flag = threading.Event()
        self.watcher_thread = None
        self.watcher_stop_event = None
        self.window = None
        self.icon = None
        self._last_icon = None
        self._win_pos = None   # (x, y) no monitor primario, calculado em run()

    # -- thread do watcher ----------------------------------------------------

    def start_watcher(self):
        os.makedirs(cw.STATE_DIR, exist_ok=True)
        cw.log(f"=== watcher iniciado pela GUI em {datetime.now()} ===")
        self.watcher_stop_event = threading.Event()
        self.watcher_thread = threading.Thread(
            target=cw.main, args=(self.watcher_stop_event,), daemon=True
        )
        self.watcher_thread.start()

    def watcher_alive(self):
        return self.watcher_thread is not None and self.watcher_thread.is_alive()

    def restart_watcher(self):
        """Reinicia o watcher para ele reler a lista de pastas.

        O watchdog monta a arvore de observadores no start, entao trocar a
        lista exige subir a thread de novo. Uma revisao em andamento no
        momento do restart e perdida — o arquivo sera revisado de novo no
        proximo save.
        """
        log_restart = self.state.reviewing
        self.stop_watcher()
        self.start_watcher()
        if log_restart:
            cw.emit_event("review_failed", project="—",
                          file="(revisao interrompida por mudanca de pastas)")

    def stop_watcher(self):
        if self.watcher_alive():
            self.watcher_stop_event.set()
            self.watcher_thread.join(timeout=8)

    # -- API exposta ao JavaScript ------------------------------------------

    def get_state(self):
        return self.state.snapshot(self.watcher_alive())

    def toggle_master(self):
        control = cw.read_control()
        write_control(paused=not control["paused"])
        self.refresh_icon()
        return True

    def add_project(self):
        """Abre o seletor de pastas do Windows e adiciona o repositorio.

        Retorna {"ok": bool, "msg": str} — o painel exibe a mensagem.
        """
        try:
            picked = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception as exc:
            return {"ok": False, "msg": f"Nao foi possivel abrir o seletor: {exc}"}
        if not picked:
            return {"ok": False, "msg": ""}          # usuario cancelou

        path = os.path.abspath(picked[0])

        if not os.path.isdir(path):
            return {"ok": False, "msg": "Pasta nao encontrada."}
        if not cw.is_git_repo(path):
            return {"ok": False,
                    "msg": f"'{os.path.basename(path)}' nao e um repositorio git "
                           f"(sem pasta .git). O watcher precisa do git para "
                           f"calcular o diff."}

        dirs = cw.load_watched_dirs()
        if any(os.path.normcase(d) == os.path.normcase(path) for d in dirs):
            return {"ok": False, "msg": "Essa pasta ja esta sendo monitorada."}

        dirs.append(path)
        cw.save_watched_dirs(dirs)
        self.restart_watcher()
        return {"ok": True,
                "msg": f"'{cw.project_name(path)}' adicionado e sendo monitorado."}

    def scan_for_repos(self):
        """Abre o seletor de pastas, escolhe uma raiz e varre em busca de
        repositorios git dentro dela.

        Retorna {"ok", "msg", "repos": [{"path","name","already"}]}. Nao
        adiciona nada sozinho — so devolve a lista para o painel confirmar.
        """
        try:
            picked = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception as exc:
            return {"ok": False, "msg": f"Nao foi possivel abrir o seletor: {exc}",
                    "repos": []}
        if not picked:
            return {"ok": False, "msg": "", "repos": []}          # cancelou

        root = os.path.abspath(picked[0])
        if not os.path.isdir(root):
            return {"ok": False, "msg": "Pasta nao encontrada.", "repos": []}

        found = cw.discover_git_repos(root)
        if not found:
            return {"ok": False,
                    "msg": f"Nenhum repositorio git encontrado dentro de "
                           f"'{os.path.basename(root)}'.",
                    "repos": []}

        current = {os.path.normcase(d) for d in cw.load_watched_dirs()}
        repos = [{"path": p, "name": cw.project_name(p),
                  "already": os.path.normcase(p) in current}
                 for p in sorted(found, key=str.lower)]
        return {"ok": True,
                "msg": f"{len(found)} repositorio(s) encontrado(s) em "
                       f"'{os.path.basename(root)}'.",
                "repos": repos}

    def add_projects_bulk(self, paths):
        """Adiciona varias pastas de uma vez (um unico restart do watcher)."""
        dirs = cw.load_watched_dirs()
        current = {os.path.normcase(d) for d in dirs}
        added, skipped = [], []

        for raw in paths:
            path = os.path.abspath(raw)
            if os.path.normcase(path) in current:
                continue
            if not cw.is_git_repo(path):
                skipped.append(cw.project_name(path))
                continue
            dirs.append(path)
            current.add(os.path.normcase(path))
            added.append(cw.project_name(path))

        if added:
            cw.save_watched_dirs(dirs)
            self.restart_watcher()

        if not added and not skipped:
            return {"ok": False, "msg": "Nenhuma pasta nova para adicionar."}

        parts = []
        if added:
            parts.append(f"{len(added)} adicionado(s): {', '.join(added)}.")
        if skipped:
            parts.append(f"{len(skipped)} ignorado(s) (nao sao repos git): "
                         f"{', '.join(skipped)}.")
        return {"ok": bool(added), "msg": " ".join(parts)}

    def retry_commit(self, project, sha):
        """Forca a re-revisao de um commit especifico (botao 'Revisar
        novamente' nos cards com falha). Roda numa thread separada para nao
        travar a janela durante a chamada ao Claude CLI.
        """
        if not sha:
            return {"ok": False, "msg": "Commit invalido."}
        path = next((p["path"] for p in self.state.current_projects()
                     if p["name"] == project), None)
        if not path:
            return {"ok": False, "msg": f"Projeto '{project}' nao encontrado."}

        threading.Thread(target=cw.retry_commit_review, args=(path, sha),
                         daemon=True).start()
        return {"ok": True, "msg": f"Revisando {sha[:7]} novamente..."}

    def remove_project(self, path):
        """Remove uma pasta do monitoramento (nao apaga nada em disco)."""
        dirs = cw.load_watched_dirs()
        kept = [d for d in dirs
                if os.path.normcase(d) != os.path.normcase(os.path.abspath(path))]
        if len(kept) == len(dirs):
            return {"ok": False, "msg": "Pasta nao estava na lista."}
        cw.save_watched_dirs(kept)
        self.restart_watcher()
        return {"ok": True,
                "msg": f"'{cw.project_name(path)}' removido do monitoramento. "
                       f"O review-log.md dele continua no disco."}

    def toggle_project(self, name):
        control = cw.read_control()
        paused = set(control["paused_projects"])
        paused.discard(name) if name in paused else paused.add(name)
        write_control(paused_projects=paused)
        return True

    # -- janela --------------------------------------------------------------

    def show_window(self, *_):
        if not self.window:
            return
        self.window.show()
        try:
            self.window.restore()   # traz de volta se estava minimizada
        except Exception:
            pass
        # `restore()`/`show()` do pywebview passam pelo WindowState do
        # WinForms via Invoke() assincrono — em maquina com 2+ monitores,
        # observamos a janela acabar minimizada de verdade (GetWindowRect
        # retornando o sentinel -32000,-32000 do Windows) mesmo chamando
        # `.move()` do pywebview logo em seguida, provavelmente uma corrida
        # entre essas chamadas encadeadas. Em vez de depender da sequencia
        # show()/restore()/move() do pywebview, usamos ShowWindow(SW_RESTORE)
        # + SetWindowPos direto do Win32 sobre o handle real da janela — e
        # sincrono e determinístico, sem essa corrida.
        if self._win_pos is not None:
            self._force_show_at(*self._win_pos)

    def _force_show_at(self, x, y):
        """Restaura (se minimizada) e reposiciona a janela via Win32 direto.

        Contorna a corrida entre show()/restore()/move() do pywebview (ver
        show_window acima). FindWindowW pelo titulo evita depender de algum
        handle interno do pywebview que pode nao estar pronto ainda.
        """
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, "Code Watcher")
            if not hwnd:
                cw.log("  ! _force_show_at: janela 'Code Watcher' nao encontrada")
                return
            SW_RESTORE = 9
            SWP_NOSIZE, SWP_NOZORDER, SWP_SHOWWINDOW = 0x0001, 0x0004, 0x0040
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetWindowPos(
                hwnd, None, x, y, 0, 0,
                SWP_NOSIZE | SWP_NOZORDER | SWP_SHOWWINDOW,
            )
            user32.SetForegroundWindow(hwnd)
            cw.log(f"  = janela restaurada e movida para ({x}, {y}) via Win32")
        except Exception as exc:
            cw.log(f"  ! _force_show_at falhou: {exc}")

    def on_closing(self):
        """X da janela apenas esconde — o app continua na bandeja."""
        self.window.hide()
        return False

    # -- bandeja -------------------------------------------------------------

    def refresh_icon(self):
        """Atualiza o icone conforme o estado (ocioso / revisando / pausado)."""
        if not self.icon:
            return
        if not self.watcher_alive():
            # Estado distinto de "pausado": o processo do watcher caiu.
            want, title = ICON_PAUSED, "Code Watcher — watcher parado (veja watcher.log)"
        elif cw.read_control()["paused"]:
            want, title = ICON_PAUSED, "Code Watcher — pausado"
        elif self.state.reviewing:
            want, title = ICON_BUSY, "Code Watcher — revisando..."
        else:
            want, title = ICON_IDLE, "Code Watcher — monitorando"
        if want is not self._last_icon:
            self.icon.icon = want
            self._last_icon = want
        self.icon.title = title

    def icon_watch_loop(self):
        while not self.stop_flag.is_set():
            self.refresh_icon()
            if self.icon:
                self.icon.update_menu()
            time.sleep(1)

    def build_menu(self):
        def pause_label(_):
            return ("Retomar monitoramento" if cw.read_control()["paused"]
                    else "Pausar monitoramento")

        return pystray.Menu(
            pystray.MenuItem("Abrir painel", self.show_window, default=True),
            pystray.MenuItem(pause_label, lambda *_: self.toggle_master()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Sair", self.quit),
        )

    def quit(self, *_):
        self.stop_flag.set()
        self.stop_watcher()
        if self.icon:
            self.icon.stop()
        if self.window:
            self.window.destroy()

    # -- ciclo de vida -------------------------------------------------------

    def run(self):
        global ICON_IDLE, ICON_BUSY, ICON_PAUSED
        ICON_IDLE = make_icon_image("#4ade80")
        ICON_BUSY = make_icon_image("#fbbf24", badge="#fbbf24")
        ICON_PAUSED = make_icon_image("#8b93a5")

        write_control(paused=False)   # garante control.json valido no boot
        self.start_watcher()

        threading.Thread(target=tail_events, args=(self.state, self.stop_flag),
                         daemon=True).start()

        self.icon = pystray.Icon("code_watcher", ICON_IDLE,
                                 "Code Watcher", self.build_menu())
        self._last_icon = ICON_IDLE
        threading.Thread(target=self.icon.run, daemon=True).start()
        threading.Thread(target=self.icon_watch_loop, daemon=True).start()

        # Janela criada escondida: o app sobe direto para a bandeja.
        #
        # screen=... explicito, centralizado no monitor PRIMARIO. Sem isso,
        # em maquina com 2+ monitores o pywebview escolhe sozinho em qual
        # monitor abrir (nem sempre o primario) — a janela abre normalmente,
        # so que fora da tela que o usuario esta olhando, parecendo que "nao
        # abriu nada". `webview.screens[0]` nao e garantido ser o primario
        # em toda maquina, entao procuramos explicitamente o que comeca em
        # (0, 0) — e o unico jeito confiavel de identificar o primario.
        win_w, win_h = 1180, 760
        primary_screen = next(
            (s for s in webview.screens if s.x == 0 and s.y == 0), None
        )
        cw.log(f"  telas: {webview.screens} | primario: {primary_screen}")
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

        # `--show` abre o painel junto com o app (util para testar). Sem a
        # flag, o app sobe so na bandeja, como pede a spec.
        # O evento `loaded` nao dispara em janela criada oculta, entao o
        # show() vai numa thread com um pequeno atraso.
        if "--show" in sys.argv:
            def show_soon():
                time.sleep(2)
                self.show_window()
            threading.Thread(target=show_soon, daemon=True).start()

        try:
            webview.start()          # bloqueia na thread principal
        finally:
            self.stop_flag.set()
            self.stop_watcher()


def main():
    if not os.path.exists(UI_FILE):
        print(f"ui.html nao encontrado em {UI_FILE}", file=sys.stderr)
        return 1
    App().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
