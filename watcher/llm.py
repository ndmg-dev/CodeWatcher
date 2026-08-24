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
# Custo estimado (so se aplica a API paga por token da OpenAI — o Claude CLI
# roda sob a assinatura existente, sem custo marginal por chamada aqui).
# Precos aproximados (USD por 1M tokens), lista publica da OpenAI — podem
# ficar desatualizados se a OpenAI mudar preco; e so uma estimativa exibida
# no painel, nunca a fatura real.
# ---------------------------------------------------------------------------

OPENAI_PRICING_PER_1M = {
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "o3-mini": {"input": 1.10, "output": 4.40},
}


def estimate_cost_usd(model, usage):
    """Custo estimado (USD) de uma chamada, a partir do 'usage' devolvido
    pela API da OpenAI. 0.0 se o modelo for desconhecido ou usage ausente
    (ex: Claude CLI, que nao devolve contagem de tokens por chamada)."""
    if not usage:
        return 0.0
    pricing = OPENAI_PRICING_PER_1M.get(model)
    if not pricing:
        return 0.0
    tokens_in = usage.get("prompt_tokens", 0)
    tokens_out = usage.get("completion_tokens", 0)
    return (tokens_in * pricing["input"] + tokens_out * pricing["output"]) / 1_000_000


# ---------------------------------------------------------------------------
# Provedores
# ---------------------------------------------------------------------------

def call_openai(prompt, api_key, model):
    """Chama a API da OpenAI. Retorna (texto, usage) — usage e o dict 'usage'
    cru da resposta (prompt_tokens/completion_tokens), ou None em erro."""
    if not api_key:
        log("  ! Chave OPENAI_API_KEY nao configurada.")
        return None, None

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
            text = data["choices"][0]["message"]["content"].strip()
            return text, data.get("usage")
    except Exception as exc:
        log(f"  ! Erro na API da OpenAI: {exc}")
        return None, None


def call_claude(repo_root, prompt):
    """Chama o Claude Code CLI com um prompt pronto. Retorna (texto, None) —
    None no lugar de usage porque o CLI nao devolve contagem de tokens.

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
        return None, None
    except subprocess.TimeoutExpired:
        log(f"  ! revisao excedeu {CLAUDE_TIMEOUT}s e foi abortada.")
        return None, None
    except OSError as exc:
        log(f"  ! erro ao executar o CLI: {exc}")
        return None, None

    if result.returncode != 0:
        log(f"  ! CLI retornou {result.returncode}: {result.stderr.strip()[:300]}")
        return None, None

    return result.stdout.strip() or None, None


def call_llm(repo_root, prompt):
    """Encaminha o prompt para o provedor configurado.

    Retorna (texto, custo_usd_estimado). custo e sempre 0.0 para o Claude
    CLI (assinatura, sem contagem de tokens por chamada aqui).
    """
    ctrl = read_control()
    if ctrl.get("llm_provider") == "openai":
        model = ctrl.get("openai_model", "gpt-4o")
        text, usage = call_openai(prompt, ctrl.get("openai_api_key", ""), model)
        return text, estimate_cost_usd(model, usage)
    else:
        text, _usage = call_claude(repo_root, prompt)
        return text, 0.0
