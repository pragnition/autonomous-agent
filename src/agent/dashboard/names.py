"""People's names for the dashboard.

Claude accounts: the account proxy only ever shows code names, by design. Its own config lists each account by its
holder's work email, so a script on the host (`python -m agent.dashboard.names`, run by `just account-names`) writes
code → name and email to data/config/account-names.toml, which core reads. Core never sees the proxy's config or
its token directory.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path

DEFAULT_PROXY_CONFIG = Path.home() / ".claude" / "accounts" / "proxy.toml"
NAMES_FILE = "account-names.toml"


def name_from_email(email: str) -> str:
    """firstname.lastname@company → "Firstname Lastname"."""
    local = email.split("@", 1)[0]
    return " ".join(part.capitalize() for part in re.split(r"[._\-+]+", local) if part)


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_account_names(proxy_config: Path, out: Path) -> int:
    accounts = tomllib.loads(Path(proxy_config).read_text()).get("accounts") or {}
    lines = ["# Written by `just account-names` from the account proxy's config: code name -> account holder.",
             "[accounts]"]
    for email, rec in accounts.items():
        code = rec if isinstance(rec, str) else rec.get("code") if isinstance(rec, dict) else None
        if isinstance(code, str) and code:
            lines.append(f"{code} = {{ name = {_toml_str(name_from_email(email))}, email = {_toml_str(email)} }}")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(out)
    return len(lines) - 2


class AccountNames:
    """code name → {name, email}, from data/config/account-names.toml; re-read when the file changes."""

    def __init__(self, path: Path):
        self.path, self._mtime, self._names = Path(path), None, {}

    def all(self) -> dict[str, dict]:
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            return {}
        if mtime != self._mtime:
            try:
                data = tomllib.loads(self.path.read_text()).get("accounts") or {}
            except (OSError, ValueError):
                data = {}
            self._names = {code: rec for code, rec in data.items() if isinstance(rec, dict) and rec.get("name")}
            self._mtime = mtime
        return self._names

    def name(self, code: str | None) -> str | None:
        return (self.all().get(code) or {}).get("name") if code else None


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    proxy = Path(os.environ.get("CLAUDE_PROXY_CONFIG") or DEFAULT_PROXY_CONFIG)
    out = Path(args[0]) if args else Path("data/config") / NAMES_FILE
    print(f"wrote {write_account_names(proxy, out)} account names to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
