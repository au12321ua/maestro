"""Basic redaction: known secret values, Maestro tokens, common headers,
key/value pairs (plain, JSON and Python-repr quoted), provider key shapes, URL passwords.

This is best-effort for a trusted team; encoded or transformed secrets can escape.
Every pattern starts on a literal and repeats over bounded or single-pass classes, so a
hostile megabyte-long line cannot make it backtrack badly.
"""

import base64
import json
import re
from urllib.parse import quote

from maestro.security.tokens import TOKEN_TEXT

REDACTED = "[REDACTED]"

# What follows a key name: an optional closing quote (escaped when the line is itself
# inside logged JSON), the separator and optional space.
_AFTER_KEY = r"(?:\\?[\"'])?\s*[:=]\s*"
_SECRET_KEYS = (
    r"authorization|api[_-]?key|access[_-]?token|auth[_-]?token|refresh[_-]?token"
    r"|client[_-]?secret|secret(?:[_-]?access)?(?:[_-]?key)?|password|passwd"
)
_TOKEN_CHARS = r"[A-Za-z0-9._~+/=-]"


def _key_value(key: str, bare: str, quoted_min: int = 1) -> list[re.Pattern]:
    """One pattern per way of writing the value; each keeps the text up to the value."""
    head = rf"(?i)((?:{key}){_AFTER_KEY}"
    return [
        re.compile(rf'{head}\\?")[^"\\\n]{{{quoted_min},}}'),  # "key": "value", may hold spaces
        re.compile(rf"{head}\\?')[^'\\\n]{{{quoted_min},}}"),  # 'key': 'value'
        re.compile(rf"{head}){bare}"),  # KEY=value, key: value, Authorization: Bearer value
    ]


_PATTERNS = [
    TOKEN_TEXT,
    *_key_value(_SECRET_KEYS, r"(?:(?:bearer|basic|token)\s+)?[^\s,;\"'&]+"),
    # A bare "token" is also an ordinary word ("token: ok"): only long values count.
    *_key_value("token", rf"{_TOKEN_CHARS}{{16,}}", 16),
    re.compile(r"(?i)([?&](?:api[_-]?key|access_token|token|key|sig|signature)=)[^&\s\"']+"),
    # A bare scheme and value as in a pasted header; "the bearer of bad news" stays.
    re.compile(
        rf"(?i)(\bbearer\s+)(?:(?={_TOKEN_CHARS}*\d){_TOKEN_CHARS}{{8,}}|{_TOKEN_CHARS}{{32,}})"
    ),
    # user:password@host in any URL.
    re.compile(r"(?i)(\b[a-z][a-z0-9+.-]{1,20}://[^\s/:@]{1,128}:)[^\s/@]{1,512}(?=@)"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
]


def value_variants(secret: str) -> set[str]:
    raw = secret.encode()
    return {
        secret,
        base64.b64encode(raw).decode(),
        quote(secret, safe=""),
        json.dumps(secret)[1:-1],
    }


def redact(text: str, secrets: list[str] | tuple[str, ...] = ()) -> str:
    variants = sorted(
        {v for s in secrets if s and len(s) >= 4 for v in value_variants(s)}, key=len, reverse=True
    )
    for variant in variants:
        text = text.replace(variant, REDACTED)
    for pattern in _PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.re.groups else "") + REDACTED, text)
    return text


def redact_json(value, secrets=()):
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, dict):
        return {k: redact_json(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_json(v, secrets) for v in value]
    return value
