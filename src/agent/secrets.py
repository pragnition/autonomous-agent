"""Secrets come from the service's own mounted file, into memory only — never os.environ, which the SDK
passes on to every agent process."""

from __future__ import annotations

import re
from pathlib import Path


def load_secrets(path: Path | str) -> dict[str, str]:
    path = Path(path)
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if value[:1] in ("\"", "'") and value[0] in value[1:]:   # quoted: keep what's inside, drop the rest
            value = value[1:value.index(value[0], 1)]
        else:                                                   # unquoted: " #" starts a comment
            value = re.split(r"\s+#", value, maxsplit=1)[0]
        out[key.strip()] = value
    return out
