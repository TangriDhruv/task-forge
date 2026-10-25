"""Redaction helper for anything written to logs/evidence (assignment.md §3.4).

Applied at the point a step record is built, before it ever reaches disk —
not as an afterthought pass over already-written files.
"""

from __future__ import annotations

# Field/attribute names whose values must never be written verbatim.
SENSITIVE_KEYS = frozenset({"password", "passwd", "secret", "token", "api_key", "authorization"})

REDACTED = "[REDACTED]"


def redact_value(key: str, value: str) -> str:
    if key.lower() in SENSITIVE_KEYS:
        return REDACTED
    return value


def redact_mapping(data: dict) -> dict:
    return {k: redact_value(k, v) if isinstance(v, str) else v for k, v in data.items()}
