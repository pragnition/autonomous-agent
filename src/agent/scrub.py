"""Strip secrets from text that leaves core: known secret values, then token and key patterns."""

from __future__ import annotations

import re
from collections.abc import Iterable

REDACTED = "[redacted]"
MIN_EXACT = 8
PATTERNS = [re.compile(p) for p in (
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    r"sk-ant-[A-Za-z0-9_-]{10,}",
    r"gh[pousr]_[A-Za-z0-9]{20,}",
    r"github_pat_[A-Za-z0-9_]{20,}",
    r"xox[abpr]-[A-Za-z0-9-]{10,}",
    r"lin_api_[A-Za-z0-9]{20,}",
    r"AKIA[0-9A-Z]{16}",
)]


def scrub(text: str, secret_values: Iterable[str]) -> str:
    for value in sorted({v for v in secret_values if v and len(v) >= MIN_EXACT}, key=len, reverse=True):
        text = text.replace(value, REDACTED)
    for pattern in PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text
