"""Redaction applied to every prompt before it leaves the platform (threat model TB3)."""

from __future__ import annotations

import re
from collections.abc import Iterable

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
        "[REDACTED_PRIVATE_KEY]",
    ),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY_ID]"),
    (re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[0-9A-Za-z]{10,}\b"), "[REDACTED_STRIPE_KEY]"),
    (re.compile(r"\bsk-ant-[0-9A-Za-z\-_]{10,}\b"), "[REDACTED_ANTHROPIC_KEY]"),
    (re.compile(r"\bgh[pousr]_[0-9A-Za-z]{20,}\b"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\b(?:rek|rit)_[0-9a-f]{8}_[0-9A-Za-z\-_]{20,}\b"), "[REDACTED_PLATFORM_TOKEN]"),
    (re.compile(r"\beyJ[0-9A-Za-z\-_]{8,}\.[0-9A-Za-z\-_]{8,}\.[0-9A-Za-z\-_]{8,}\b"), "[REDACTED_JWT]"),
    (re.compile(r"(?i)\b(authorization|bearer)\b(\s*[:=]?\s*)(bearer\s+)?[0-9A-Za-z\-_.=]{12,}"), r"\1\2[REDACTED]"),
    (
        re.compile(
            r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token|client[_-]?secret)\b(\s*[:=]\s*)"
            r"(['\"]?)[^\s'\"]{4,}\3"
        ),
        r"\1\2\3[REDACTED]\3",
    ),
    (re.compile(r"(?i)(postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^:\s/]+:[^@\s/]+@"), r"\1://[REDACTED]@"),
]


# High-confidence credential formats (no generic "password=" heuristics): a file containing one
# is excluded from code snapshots entirely.
SECRET_PATTERNS: list[re.Pattern[str]] = [p for p, _ in _PATTERNS[:7]]


def redact(text: str, extra_values: Iterable[str] | None = None) -> str:
    for value in sorted(extra_values or [], key=len, reverse=True):
        if len(value) >= 4:
            text = text.replace(value, "[REDACTED_SECRET]")
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text
