import json
import shutil
import subprocess
import urllib.request

from .config import (
    CREATE_NO_WINDOW, CLAUDE_CMD, CLAUDE_TIMEOUT, read_control,
)
from .logger import log


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

SEVERITY_INSTRUCTIONS = """Antes de mais nada, na PRIMEIRA linha da resposta, exatamente neste formato:
SEVERIDADE: alta
ou
SEVERIDADE: media
ou
SEVERIDADE: baixa

Use "alta" para bugs reais, falhas de seguranca ou erros de logica que quebram
o comportamento. Use "media" para melhorias importantes (performance,
duplicacao, clareza) sem bug real. Use "baixa" quando nao houver nada
relevante a apontar. Depois dessa linha, uma linha em branco e a revisao."""

PROMPT_TEMPLATE = """Voce esta revisando uma alteracao de codigo ainda nao commitada.

Arquivo: {path}

Diff (git diff HEAD):
```diff
{diff}
```

""" + SEVERITY_INSTRUCTIONS + """

Revise focando em BUGS e MELHORIAS. Seja direto e conciso:
- Liste bugs reais, erros de logica, casos de borda nao tratados e riscos de seguranca.
- Depois, sugestoes objetivas de melhoria (clareza, performance, duplicacao).
- Se nao houver nada relevante a apontar, responda apenas "Sem observacoes." (depois da linha de severidade).
Nao reescreva o arquivo inteiro nem repita o diff."""

PROMPT_TEMPLATE_COMMIT = """Voce esta revisando um commit que acabou de ser feito.

Commit: {sha}
Mensagem: {subject}

Diff (git show):
```diff
{diff}
```

""" + SEVERITY_INSTRUCTIONS + """

Revise focando em BUGS e MELHORIAS. Seja direto e conciso:
- Liste bugs reais, erros de logica, casos de borda nao tratados e riscos de seguranca.
- Depois, sugestoes objetivas de melhoria (clareza, performance, duplicacao).
- Se nao houver nada relevante a apontar, responda apenas "Sem observacoes." (depois da linha de severidade).
Nao reescreva o arquivo inteiro nem repita o diff."""

PROMPT_TEMPLATE_PR = """Voce esta revisando um Pull Request aberto no GitHub.

PR #{number}: {title}

Diff (gh pr diff):
```diff
{diff}
```

""" + SEVERITY_INSTRUCTIONS + """

Revise focando em BUGS e MELHORIAS. Seja direto e conciso:
- Liste bugs reais, erros de logica, casos de borda nao tratados e riscos de seguranca.
- Depois, sugestoes objetivas de melhoria (clareza, performance, duplicacao).
- Se nao houver nada relevante a apontar, responda apenas "Sem observacoes." (depois da linha de severidade).
Nao reescreva o arquivo inteiro nem repita o diff."""


# ---------------------------------------------------------------------------
# Provedores
# ---------------------------------------------------------------------------

def call_openai(prompt, api_key, model):
    """Chama a API da OpenAI para gerar a revisao do codigo."""
    if not api_key:
        log("  ! Chave OPENAI_API_KEY nao configurada.")
        return None

    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "Voce e um revisor de codigo especialista. Revise a alteracao de codigo apresentada (em diff), aponte problemas de logica, seguranca ou mas praticas de forma direta. Formate a resposta sempre em Markdown limpo."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0.2
    }

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        log(f"  ! Erro na API da OpenAI: {exc}")
        return None


def call_claude(repo_root, prompt):
    """Chama o Claude Code CLI com um prompt pronto. Retorna o texto ou None.

    O prompt vai por stdin, nao como argumento `-p "<prompt>"`. Um commit ou
    PR grande facilmente passa dos ~8191 caracteres que o wrapper .cmd do
    Claude Code aceita numa linha de comando do Windows (limite do cmd.exe,
    bem menor que o MAX_DIFF_CHARS de 12000) — por stdin nao ha esse teto.
    """
    executable = shutil.which(CLAUDE_CMD) or CLAUDE_CMD
    try:
        result = subprocess.run(
            [executable, "-p"],
            cwd=repo_root,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CLAUDE_TIMEOUT,
            creationflags=CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        log(f"  ! comando '{CLAUDE_CMD}' nao encontrado no PATH. "
            f"Ajuste a constante CLAUDE_CMD no topo do script.")
        return None
    except subprocess.TimeoutExpired:
        log(f"  ! revisao excedeu {CLAUDE_TIMEOUT}s e foi abortada.")
        return None
    except OSError as exc:
        log(f"  ! erro ao executar o CLI: {exc}")
        return None

    if result.returncode != 0:
        log(f"  ! CLI retornou {result.returncode}: {result.stderr.strip()[:300]}")
        return None

    return result.stdout.strip() or None


def call_llm(repo_root, prompt):
    """Encaminha o prompt para o provedor configurado."""
    ctrl = read_control()
    if ctrl.get("llm_provider") == "openai":
        return call_openai(prompt, ctrl.get("openai_api_key", ""), ctrl.get("openai_model", "gpt-4o"))
    else:
        return call_claude(repo_root, prompt)
