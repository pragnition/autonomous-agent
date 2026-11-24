"""People's names for the dashboard's Accounts page.

The account proxy only ever shows code names, by design. Its own config lists each account by its holder's work
email, so this script, run on the host by `just account-names`, writes code -> name and email to
data/config/account-names.toml, which agentd reads. agentd never sees the proxy's config or its token directory.
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


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    proxy = Path(os.environ.get("CLAUDE_PROXY_CONFIG") or DEFAULT_PROXY_CONFIG)
    out = Path(args[0]) if args else Path("data/config") / NAMES_FILE
    print(f"wrote {write_account_names(proxy, out)} account names to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
