"""
watcher_gui.py — ponto de entrada da GUI (tray icon + painel visual).

A logica real esta em watcher/gui/app.py. Este arquivo existe para manter
compatibilidade com o fluxo de uso original (`python watcher_gui.py`,
`pythonw watcher_gui.py --show`, build_exe.ps1).
"""

import sys
from watcher.gui.app import run_gui

if __name__ == "__main__":
    sys.exit(run_gui())
