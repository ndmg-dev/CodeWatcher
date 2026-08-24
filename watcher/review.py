import collections
import os
import re
import threading
import time
from datetime import datetime

from .config import MAX_DIFF_CHARS, MAX_REVIEWS_PER_HOUR, REVIEW_LOG_NAME, read_control
from .git import (
    project_name, get_diff, get_commit_diff, get_pr_diff,
    read_ref_sha, load_seen_commits, mark_commit_seen,
    load_seen_prs, mark_pr_seen,
    diff_fingerprint, is_duplicate_diff, mark_diff_hash,
)
from .llm import (
    PROMPT_TEMPLATE, PROMPT_TEMPLATE_COMMIT, PROMPT_TEMPLATE_PR, call_llm,
)
from .logger import log, emit_event

_RATE_WINDOW_SECONDS = 3600
_rate_lock = threading.Lock()
_call_times = collections.deque()

_SEVERITY_RE = re.compile(r"^\s*severidade\s*:\s*(alta|media|média|baixa)\s*$", re.IGNORECASE)


def _allow_review():
    """Sliding window de MAX_REVIEWS_PER_HOUR chamadas por hora, somando todos
    os projetos. Reserva a vaga (registra o timestamp) na mesma chamada que
    aprova, para nao deixar brecha de corrida entre checar e consumir.
    Em memoria, nao persiste entre reinicios do watcher."""
    now = time.time()
    with _rate_lock:
        while _call_times and now - _call_times[0] > _RATE_WINDOW_SECONDS:
            _call_times.popleft()
        if len(_call_times) >= MAX_REVIEWS_PER_HOUR:
            return False
        _call_times.append(now)
        return True


def rate_limit_status():
    """(usadas, limite) na janela atual de 1h — para o painel exibir."""
    now = time.time()
    with _rate_lock:
        while _call_times and now - _call_times[0] > _RATE_WINDOW_SECONDS:
            _call_times.popleft()
        return len(_call_times), MAX_REVIEWS_PER_HOUR


def _extract_severity(review_text):
    """Le a linha 'SEVERIDADE: alta|media|baixa' pedida no prompt (primeira
    linha nao vazia da resposta) e a remove do texto exibido. Se o modelo
    nao seguir o formato pedido, assume 'baixa' — nao dispara notificacao
    nem entra na contagem de criticos do resumo diario, mas a revisao em si
    continua sendo salva normalmente."""
    lines = review_text.splitlines()
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        m = _SEVERITY_RE.match(line)
        if not m:
            break
        severity = m.group(1).lower().replace("é", "e")
        rest = "\n".join(lines[i + 1:]).strip()
        return severity, (rest or review_text)
    return "baixa", review_text


def review_with_llm(repo_root, prompt_template, diff, **fields):
    """Formata uma solicitacao de revisao e a envia ao provedor configurado."""
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n... (diff truncado)"
    return call_llm(repo_root, prompt_template.format(diff=diff, **fields))


def append_to_review_log(repo_root, rel_path, review, severity=None):
    """Anexa a revisao ao review-log.md na raiz do projeto."""
    log_path = os.path.join(repo_root, REVIEW_LOG_NAME)
    severity_line = f"**Severidade:** {severity}\n\n" if severity else ""
    entry = (
        f"\n## {datetime.now():%Y-%m-%d %H:%M:%S} — `{rel_path}`\n\n"
        f"{severity_line}{review}\n"
    )
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(entry)
    return log_path


def process_file(repo_root, file_path):
    """Pipeline completo para um arquivo: diff -> revisao -> log."""
    rel_path = os.path.relpath(file_path, repo_root).replace(os.sep, "/")
    project = project_name(repo_root)
    repo_key = os.path.normcase(repo_root)

    control = read_control()
    if control["paused"] or project in control["paused_projects"]:
        log(f"  - {rel_path}: monitoramento pausado, ignorando.")
        return

    diff = get_diff(repo_root, file_path)
    if not diff.strip():
        log(f"  - {rel_path}: sem mudancas vs HEAD, ignorando.")
        return

    fingerprint = diff_fingerprint(diff)
    if is_duplicate_diff(repo_key, "file", rel_path, fingerprint):
        log(f"  - {rel_path}: diff identico ao ja revisado, pulando.")
        return

    if not _allow_review():
        log(f"  - {rel_path}: limite de {MAX_REVIEWS_PER_HOUR} revisoes/hora atingido, pulando.")
        emit_event("review_failed", project=project, file=rel_path, reason="rate_limit")
        return

    log(f"  > analisando {rel_path} ({len(diff)} chars de diff)...")
    emit_event("review_start", project=project, file=rel_path,
               diff_chars=len(diff))

    started = time.time()
    raw_result = review_with_llm(
        repo_root, PROMPT_TEMPLATE, diff, path=rel_path,
    )
    elapsed = round(time.time() - started, 1)

    if raw_result is None:
        log(f"  - {rel_path}: revisao nao gerada.")
        emit_event("review_failed", project=project, file=rel_path,
                   duration=elapsed)
        return

    severity, result = _extract_severity(raw_result)
    mark_diff_hash(repo_key, "file", rel_path, fingerprint)
    log_path = append_to_review_log(repo_root, rel_path, result, severity)
    log(f"  = revisao salva em {log_path} (severidade: {severity})")
    emit_event("review_done", project=project, file=rel_path,
               review=result, severity=severity, duration=elapsed, log_path=log_path)


def process_ref_update(repo_root, branch):
    """Pipeline completo para um commit novo: le o SHA -> git show -> revisao -> log."""
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
        return

    subject, diff = get_commit_diff(repo_root, sha)
    if diff is None:
        return

    if not diff.strip():
        log(f"  - commit {sha[:7]} em {branch}: sem diff, ignorando.")
        mark_commit_seen(repo_key, branch, sha)
        return

    label = f"commit {sha[:7]} ({branch})"

    fingerprint = diff_fingerprint(diff)
    if is_duplicate_diff(repo_key, "commit", branch, fingerprint):
        log(f"  - {label}: diff identico ao ultimo revisado neste branch (amend/rebase sem mudanca real), pulando.")
        mark_commit_seen(repo_key, branch, sha)
        return

    if not _allow_review():
        log(f"  - {label}: limite de {MAX_REVIEWS_PER_HOUR} revisoes/hora atingido, pulando.")
        emit_event("review_failed", project=project, file=label, source="commit",
                   reason="rate_limit")
        return

    log(f"  > analisando {label} ({len(diff)} chars)...")
    emit_event("review_start", project=project, file=label, source="commit",
               diff_chars=len(diff))

    started = time.time()
    raw_result = review_with_llm(
        repo_root, PROMPT_TEMPLATE_COMMIT, diff,
        sha=sha[:7], subject=subject,
    )
    elapsed = round(time.time() - started, 1)

    if raw_result is None:
        log(f"  - {label}: revisao nao gerada.")
        emit_event("review_failed", project=project, file=label,
                   source="commit", duration=elapsed)
        return

    severity, result = _extract_severity(raw_result)
    mark_commit_seen(repo_key, branch, sha)
    mark_diff_hash(repo_key, "commit", branch, fingerprint)
    log_path = append_to_review_log(repo_root, label, result, severity)
    log(f"  = revisao salva em {log_path} (severidade: {severity})")
    emit_event("review_done", project=project, file=label, source="commit",
               review=result, severity=severity, duration=elapsed, log_path=log_path)


def process_pr(repo_root, pr):
    """Pipeline completo para um PR novo/atualizado: diff -> revisao -> log."""
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
        return

    diff = get_pr_diff(repo_root, number)
    if diff is None:
        return

    if not diff.strip():
        mark_pr_seen(repo_key, number, head_sha)
        return

    label = f"PR #{number} — {title}"

    fingerprint = diff_fingerprint(diff)
    if is_duplicate_diff(repo_key, "pr", number, fingerprint):
        log(f"  - {label}: diff identico ao ultimo revisado (push sem mudanca real), pulando.")
        mark_pr_seen(repo_key, number, head_sha)
        return

    if not _allow_review():
        log(f"  - {label}: limite de {MAX_REVIEWS_PER_HOUR} revisoes/hora atingido, pulando.")
        emit_event("review_failed", project=project, file=label, source="pr",
                   reason="rate_limit")
        return

    log(f"  > analisando {label} ({len(diff)} chars)...")
    emit_event("review_start", project=project, file=label, source="pr",
               diff_chars=len(diff))

    started = time.time()
    raw_result = review_with_llm(
        repo_root, PROMPT_TEMPLATE_PR, diff, number=number, title=title,
    )
    elapsed = round(time.time() - started, 1)

    if raw_result is None:
        log(f"  - {label}: revisao nao gerada.")
        emit_event("review_failed", project=project, file=label,
                   source="pr", duration=elapsed)
        return

    severity, result = _extract_severity(raw_result)
    mark_pr_seen(repo_key, number, head_sha)
    mark_diff_hash(repo_key, "pr", number, fingerprint)
    log_path = append_to_review_log(repo_root, label, result, severity)
    log(f"  = revisao salva em {log_path} (severidade: {severity})")
    emit_event("review_done", project=project, file=label, source="pr",
               review=result, severity=severity, duration=elapsed, log_path=log_path)


def retry_commit_review(repo_root, sha):
    """Forca a revisao de um commit especifico, sob pedido explicito do
    painel. Ignora deliberadamente a deduplicacao por hash (o usuario esta
    pedindo de novo, na cara — nao faz sentido recusar por diff repetido),
    mas ainda respeita o rate limit."""
    project = project_name(repo_root)
    label = f"commit {sha[:7]}"

    subject, diff = get_commit_diff(repo_root, sha)
    if diff is None or not diff.strip():
        log(f"  ! retry {label}: sem diff ou 'git show' falhou.")
        emit_event("review_failed", project=project, file=label, source="commit")
        return False

    if not _allow_review():
        log(f"  - {label}: limite de {MAX_REVIEWS_PER_HOUR} revisoes/hora atingido, pulando (retry manual).")
        emit_event("review_failed", project=project, file=label, source="commit",
                   reason="rate_limit")
        return False

    log(f"  > re-analisando {label} ({len(diff)} chars, retry manual)...")
    emit_event("review_start", project=project, file=label, source="commit",
               diff_chars=len(diff))

    started = time.time()
    raw_result = review_with_llm(
        repo_root, PROMPT_TEMPLATE_COMMIT, diff,
        sha=sha[:7], subject=subject,
    )
    elapsed = round(time.time() - started, 1)

    if raw_result is None:
        log(f"  - {label}: revisao nao gerada (retry manual).")
        emit_event("review_failed", project=project, file=label,
                   source="commit", duration=elapsed)
        return False

    severity, result = _extract_severity(raw_result)
    log_path = append_to_review_log(repo_root, label, result, severity)
    log(f"  = revisao salva em {log_path} (retry manual, severidade: {severity})")
    emit_event("review_done", project=project, file=label, source="commit",
               review=result, severity=severity, duration=elapsed, log_path=log_path)
    return True
