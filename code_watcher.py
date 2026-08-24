"""
code_watcher.py — ponto de entrada do CLI.

A logica real esta em watcher/monitor.py. Este arquivo existe para manter
compatibilidade com o fluxo de uso original (`python code_watcher.py`).
"""

import sys
from watcher.monitor import main

if __name__ == "__main__":
    sys.exit(main())
