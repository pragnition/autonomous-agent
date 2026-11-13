"""Strip sensitive text before it leaves the agent: secrets before anything is written to memory (known secret
values, then token and key patterns), and customer data from a debug sandbox's output."""

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


MARK = "[customer-data]"
_CUSTOMER = [re.compile(p) for p in (
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",                       # email
    r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}",                                 # bearer token
    r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]+",            # JWT
    r"\b(?:[0-9A-Fa-f]{1,4}:){4,7}[0-9A-Fa-f]{1,4}\b",                        # IPv6 (full form)
    r"\b(?:[0-9A-Fa-f]{1,4}:){1,6}:(?:[0-9A-Fa-f]{1,4})?\b",                 # IPv6 (compressed)
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b",                                           # IPv4
    r"\+\d[\d \-]{6,}\d",                                                      # phone with a country code
    r"(?<![\w.:/-])\d{8,15}(?![\w.:/-])",                                      # long bare digit run (phone, ID)
)]


def scrub_customer(text: str, patterns: list[str]) -> tuple[str, int]:
    """Strip customer data (spec v2 §7) from a debug sandbox's output, plus the pack's own patterns. Errors, stack
    traces, timings, request paths and counts survive; emails, phone numbers, IPs, tokens and ids don't."""
    hits = 0
    for pattern in [*_CUSTOMER, *(re.compile(p) for p in patterns)]:
        text, n = pattern.subn(MARK, text)
        hits += n
    return text, hits
