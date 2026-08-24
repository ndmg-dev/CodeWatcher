"""Icone, menu e threads da bandeja do sistema."""

import threading

import pystray
from PIL import Image, ImageDraw

from ..config import read_control


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


class TrayController:
    """Mantem o pystray isolado da implementacao da janela."""

    def __init__(self, app):
        self.app = app
        self.idle_icon = make_icon_image("#4ade80")
        self.busy_icon = make_icon_image("#fbbf24", badge="#fbbf24")
        self.paused_icon = make_icon_image("#8b93a5")
        self._last_icon = self.idle_icon
        self.icon = pystray.Icon(
            "code_watcher", self.idle_icon, "Code Watcher", self._build_menu()
        )

    def _build_menu(self):
        def pause_label(_):
            return ("Retomar monitoramento" if read_control()["paused"]
                    else "Pausar monitoramento")

        return pystray.Menu(
            pystray.MenuItem("Abrir painel", self.app.show_window, default=True),
            pystray.MenuItem(pause_label, lambda *_: self.app.toggle_master()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Sair", self.app.quit),
        )

    def refresh(self):
        if not self.app.watcher_alive():
            want = self.paused_icon
            title = "Code Watcher — watcher parado (veja watcher.log)"
        elif read_control()["paused"]:
            want = self.paused_icon
            title = "Code Watcher — pausado"
        elif self.app.state.reviewing:
            want = self.busy_icon
            title = "Code Watcher — revisando..."
        else:
            want = self.idle_icon
            title = "Code Watcher — monitorando"
        if want is not self._last_icon:
            self.icon.icon = want
            self._last_icon = want
        self.icon.title = title

    def _watch_loop(self):
        while not self.app.stop_flag.is_set():
            self.refresh()
            self.icon.update_menu()
            self.app.stop_flag.wait(1)

    def start(self):
        threading.Thread(target=self.icon.run, daemon=True).start()
        threading.Thread(target=self._watch_loop, daemon=True).start()
        return self

    def stop(self):
        self.icon.stop()


def setup_tray(app):
    """Cria a bandeja e inicia suas threads de background."""
    return TrayController(app).start()
