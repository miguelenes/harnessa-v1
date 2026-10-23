"""Shared secret-like content policy for persisted and retrieved text."""

import re

_SECRET_WORDS = re.compile(
    r"(?i)(api[_-]?key|secret|password|token|credential)\s*[:=]|"
    r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----|"
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b"
)


def safe_content_for_ui(content: str) -> bool:
    """Reject text matching Harnessa's shared secret exclusion patterns."""
    return _SECRET_WORDS.search(content) is None
