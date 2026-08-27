"""Scan local e instantaneo de segredos/credenciais em diffs.

Roda antes/independente da chamada ao LLM (regex, sem custo nem espera) —
pega o caso mais grave (senha/chave exposta) na hora, em vez de depender do
modelo notar isso em ~20s. Complementar a revisao normal, nunca a substitui:
o LLM continua rodando por cima do mesmo diff como sempre.
"""

import re

# Padroes de credenciais conhecidas (formato proprio, baixa chance de falso
# positivo) + heuristica generica por nome de variavel comum a senha/chave/
# token/segredo seguido de literal de string.
_PATTERNS = [
    ("Chave AWS", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Chave privada", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Token Slack", re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,48}")),
    ("Token GitHub", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("Chave Google API", re.compile(r"AIza[0-9A-Za-z\-_]{35}")),
    ("Chave Stripe", re.compile(r"sk_live_[0-9a-zA-Z]{24,}")),
    ("JWT", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("String de conexão com credencial",
     re.compile(r"(?:mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis)://[^:\s'\"]+:[^@\s'\"]+@")),
    ("Senha/chave/token em literal",
     re.compile(
         r"(?i)\b\w*(?:senha|password|passwd|pwd|secret|api_?key|apikey|token)\w*"
         r"\s*[:=]\s*(['\"][^'\"\s]{6,}['\"])"
     )),
]

# Linhas que so referenciam variavel de ambiente (nao um valor cravado no
# codigo) nunca contam como achado, mesmo batendo no padrao generico acima.
_ENV_MARKERS = (
    "os.environ", "os.getenv", "process.env", "getenv(",
    "import.meta.env", "env(", "envfile", "dotenv",
)


def _redact(line, match):
    """Mascara so o trecho que casou o padrao, preservando o resto da linha
    como contexto — nunca expõe o segredo inteiro de volta na UI/log. Se o
    padrao tiver um grupo de captura (o valor em si, sem o nome da
    variavel), mascara so o grupo — senao mascara o match inteiro."""
    start, end = match.span(1) if match.groups() else match.span()
    secret = line[start:end]
    masked = "•" * min(len(secret), 8) if len(secret) <= 8 else f"{secret[:3]}…{secret[-3:]}"
    redacted = (line[:start] + masked + line[end:]).strip()
    return redacted if len(redacted) <= 160 else redacted[:160] + "…"


def scan_diff(diff_text):
    """Retorna uma lista de achados (dicts: kind/file/excerpt) olhando so as
    linhas ADICIONADAS do diff unificado — remocoes e contexto nao contam,
    so importa segredo novo entrando no codigo."""
    findings = []
    current_file = None
    for raw_line in diff_text.splitlines():
        if raw_line.startswith("+++ "):
            current_file = raw_line[4:].strip()
            if current_file.startswith("b/"):
                current_file = current_file[2:]
            continue
        if not raw_line.startswith("+") or raw_line.startswith("+++"):
            continue
        content = raw_line[1:]
        low = content.lower()
        if any(marker in low for marker in _ENV_MARKERS):
            continue
        for kind, pattern in _PATTERNS:
            m = pattern.search(content)
            if m:
                findings.append({
                    "kind": kind,
                    "file": current_file,
                    "excerpt": _redact(content, m),
                })
                break  # uma linha ja basta como achado, nao empilha padroes
    return findings
