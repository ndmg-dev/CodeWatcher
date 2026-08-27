import os
import json
from datetime import datetime, timedelta

CREATE_NO_WINDOW = 0x08000000

WATCHED_DIRS = [
    r"C:\Users\User\Projetos\CRM_MG",
    r"C:\Users\User\Projetos\CRONOS_MG",
    r"C:\Users\User\Projetos\TASK_MANANGER",
]

CODE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
    ".html", ".css", ".scss", ".sass",
    ".java", ".kt", ".go", ".rs", ".rb", ".php",
    ".c", ".h", ".cpp", ".hpp", ".cs",
    ".sql", ".sh", ".ps1",
}

IGNORED_DIRS = {
    ".git", "node_modules", "venv", ".venv", "env", "__pycache__",
    "dist", "build", ".next", ".nuxt", "target", "vendor",
    ".idea", ".vscode", "coverage", ".pytest_cache", ".mypy_cache",
}

DEBOUNCE_SECONDS = 3.0
CLAUDE_CMD = "claude"
CLAUDE_TIMEOUT = 180
GH_CMD = "gh"
PR_POLL_SECONDS = 300
GH_TIMEOUT = 30
REVIEW_LOG_NAME = "review-log.md"
MAX_DIFF_CHARS = 12000

# Teto padrao de chamadas ao provedor de LLM (Claude CLI ou API da OpenAI)
# por hora, somando todos os projetos monitorados — configuravel pelo painel
# (Configuracoes), este e so o valor de fabrica quando nada foi salvo ainda.
# Protege contra custo/uso descontrolado (ex: um repo barulhento, ou um bug
# de loop) — quando atingido, revisoes sao puladas (log + evento
# "review_failed" com reason="rate_limit") ate a janela de 1h abrir espaco
# de novo. Nao persiste entre reinicios do watcher (o teto em si persiste
# em control.json; a contagem da janela atual, essa sim, e em memoria).
DEFAULT_MAX_REVIEWS_PER_HOUR = 30

# Em qual severidade a bandeja notifica um achado (ver watcher/review.py
# para os valores possiveis de severidade). Tambem configuravel pelo painel.
DEFAULT_NOTIFY_SEVERITY = "alta"  # "alta" | "media" (alta ou media) | "nunca"

# Rotacao do events.jsonl: acima de EVENTS_MAX_BYTES, os eventos mais antigos
# sao resumidos (contagem total, por projeto e custo estimado) em
# events_summary.json e removidos do arquivo, mantendo so os ultimos
# EVENTS_KEEP_LINES para o feed. O "Total historico" do painel continua
# correto porque soma o resumo + o que ainda esta no arquivo.
EVENTS_MAX_BYTES = 5 * 1024 * 1024
EVENTS_KEEP_LINES = 2000

STATE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "CodeWatcher"
)
EVENTS_FILE = os.path.join(STATE_DIR, "events.jsonl")
EVENTS_SUMMARY_FILE = os.path.join(STATE_DIR, "events_summary.json")
CONTROL_FILE = os.path.join(STATE_DIR, "control.json")
PROJECTS_FILE = os.path.join(STATE_DIR, "projects.json")
SEEN_COMMITS_FILE = os.path.join(STATE_DIR, "seen_commits.json")
SEEN_PRS_FILE = os.path.join(STATE_DIR, "seen_prs.json")
SEEN_DIFF_HASHES_FILE = os.path.join(STATE_DIR, "seen_diff_hashes.json")
WATCHER_LOG_FILE = os.path.join(STATE_DIR, "watcher.log")
# So o status (resolvido/dispensado) dos itens do backlog do painel -- os
# itens em si sao derivados de events.jsonl em memoria (watcher/gui/state.py),
# nao duplicados aqui. So a GUI le/escreve este arquivo; o motor headless
# (monitor.py/review.py) nao sabe que backlog existe.
BACKLOG_STATUS_FILE = os.path.join(STATE_DIR, "backlog_status.json")

def _write_control_file(control):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = CONTROL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(control, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, CONTROL_FILE)

def read_control():
    try:
        with open(CONTROL_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}

    paused = bool(data.get("paused", False))
    paused_until = data.get("paused_until")

    # Soneca (snooze) expirada: retoma sozinho, sem esperar o usuario voltar
    # e clicar em nada. So mexe no arquivo se realmente havia uma soneca
    # pendente, para nao escrever em disco a cada leitura normal.
    if paused and paused_until:
        try:
            expired = datetime.now() >= datetime.fromisoformat(paused_until)
        except ValueError:
            expired = True  # valor corrompido: nao trava pausado para sempre
        if expired:
            paused = False
            paused_until = None
            data["paused"] = False
            data["paused_until"] = None
            _write_control_file(data)

    return {
        "paused": paused,
        "paused_until": paused_until,
        "paused_projects": list(data.get("paused_projects", [])),
        "llm_provider": data.get("llm_provider", "claude"),
        "openai_api_key": data.get("openai_api_key", ""),
        "openai_model": data.get("openai_model", "gpt-4o"),
        "max_reviews_per_hour": int(data.get("max_reviews_per_hour", DEFAULT_MAX_REVIEWS_PER_HOUR)),
        "notify_severity": data.get("notify_severity", DEFAULT_NOTIFY_SEVERITY),
    }

def write_control(paused=None, paused_projects=None, llm_provider=None, openai_api_key=None,
                   openai_model=None, max_reviews_per_hour=None, notify_severity=None):
    """Pausa/config 'normais' — nunca usado para a soneca (ver snooze_pause).
    Definir `paused` aqui sempre limpa uma soneca pendente: um pause/unpause
    manual e indefinido por natureza, nao deve herdar um horario de retomada
    de uma soneca anterior."""
    control = read_control()
    if paused is not None:
        control["paused"] = paused
        control["paused_until"] = None
    if paused_projects is not None:
        control["paused_projects"] = sorted(paused_projects)
    if llm_provider is not None:
        control["llm_provider"] = llm_provider
    if openai_api_key is not None:
        control["openai_api_key"] = openai_api_key
    if openai_model is not None:
        control["openai_model"] = openai_model
    if max_reviews_per_hour is not None:
        control["max_reviews_per_hour"] = max(1, int(max_reviews_per_hour))
    if notify_severity is not None:
        control["notify_severity"] = notify_severity
    _write_control_file(control)
    return control

def snooze_pause(minutes):
    """Pausa o monitoramento geral por N minutos, com retomada automatica
    (ver o tratamento de paused_until em read_control). Nao passa pelo
    write_control() de proposito — este e o UNICO caminho que deve gravar
    um paused_until no futuro."""
    control = read_control()
    control["paused"] = True
    control["paused_until"] = (datetime.now() + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    _write_control_file(control)
    return control

def load_watched_dirs():
    from .logger import log  # lazy import to avoid circular dependency
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
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = PROJECTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump([os.path.abspath(path) for path in dirs], fh,
                  ensure_ascii=False, indent=2)
    os.replace(tmp, PROJECTS_FILE)

def load_events_summary():
    """Contadores arquivados por rotate_events_if_needed() (eventos antigos ja
    removidos de events.jsonl). {} se nunca rotacionou ainda."""
    try:
        with open(EVENTS_SUMMARY_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"total_count": 0, "per_project": {}, "total_cost_usd": 0.0}
    return {
        "total_count": int(data.get("total_count", 0)),
        "per_project": dict(data.get("per_project", {})),
        "total_cost_usd": float(data.get("total_cost_usd", 0.0)),
    }

def save_events_summary(summary):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = EVENTS_SUMMARY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, EVENTS_SUMMARY_FILE)
