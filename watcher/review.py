import os
import time
from datetime import datetime

from .config import MAX_DIFF_CHARS, REVIEW_LOG_NAME, read_control
from .git import (
    project_name, get_diff, get_commit_diff, get_pr_diff,
    read_ref_sha, load_seen_commits, mark_commit_seen,
    load_seen_prs, mark_pr_seen,
)
from .llm import (
    PROMPT_TEMPLATE, PROMPT_TEMPLATE_COMMIT, PROMPT_TEMPLATE_PR, call_llm,
)
from .logger import log, emit_event


def review_with_llm(repo_root, prompt_template, diff, **fields):
    """Formata uma solicitacao de revisao e a envia ao provedor configurado."""
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n... (diff truncado)"
    return call_llm(repo_root, prompt_template.format(diff=diff, **fields))


def append_to_review_log(repo_root, rel_path, review):
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
    result = review_with_llm(
        repo_root, PROMPT_TEMPLATE, diff, path=rel_path,
    )
    elapsed = round(time.time() - started, 1)

    if result is None:
        log(f"  - {rel_path}: revisao nao gerada.")
        emit_event("review_failed", project=project, file=rel_path,
                   duration=elapsed)
        return

    log_path = append_to_review_log(repo_root, rel_path, result)
    log(f"  = revisao salva em {log_path}")
    emit_event("review_done", project=project, file=rel_path,
               review=result, duration=elapsed, log_path=log_path)


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
    log(f"  > analisando {label} ({len(diff)} chars)...")
    emit_event("review_start", project=project, file=label, source="commit",
               diff_chars=len(diff))

    started = time.time()
    result = review_with_llm(
        repo_root, PROMPT_TEMPLATE_COMMIT, diff,
        sha=sha[:7], subject=subject,
    )
    elapsed = round(time.time() - started, 1)

    if result is None:
        log(f"  - {label}: revisao nao gerada.")
        emit_event("review_failed", project=project, file=label,
                   source="commit", duration=elapsed)
        return

    mark_commit_seen(repo_key, branch, sha)
    log_path = append_to_review_log(repo_root, label, result)
    log(f"  = revisao salva em {log_path}")
    emit_event("review_done", project=project, file=label, source="commit",
               review=result, duration=elapsed, log_path=log_path)


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
    log(f"  > analisando {label} ({len(diff)} chars)...")
    emit_event("review_start", project=project, file=label, source="pr",
               diff_chars=len(diff))

    started = time.time()
    result = review_with_llm(
        repo_root, PROMPT_TEMPLATE_PR, diff, number=number, title=title,
    )
    elapsed = round(time.time() - started, 1)

    if result is None:
        log(f"  - {label}: revisao nao gerada.")
        emit_event("review_failed", project=project, file=label,
                   source="pr", duration=elapsed)
        return

    mark_pr_seen(repo_key, number, head_sha)
    log_path = append_to_review_log(repo_root, label, result)
    log(f"  = revisao salva em {log_path}")
    emit_event("review_done", project=project, file=label, source="pr",
               review=result, duration=elapsed, log_path=log_path)


def retry_commit_review(repo_root, sha):
    """Forca a revisao de um commit especifico, sob pedido explicito do painel."""
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
    result = review_with_llm(
        repo_root, PROMPT_TEMPLATE_COMMIT, diff,
        sha=sha[:7], subject=subject,
    )
    elapsed = round(time.time() - started, 1)

    if result is None:
        log(f"  - {label}: revisao nao gerada (retry manual).")
        emit_event("review_failed", project=project, file=label,
                   source="commit", duration=elapsed)
        return False

    log_path = append_to_review_log(repo_root, label, result)
    log(f"  = revisao salva em {log_path} (retry manual)")
    emit_event("review_done", project=project, file=label, source="commit",
               review=result, duration=elapsed, log_path=log_path)
    return True
