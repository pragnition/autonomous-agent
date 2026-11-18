"""Open (or find) the PR for the current branch, for improve-jensen runs on a runner without `gh`.

    python scripts/open_pr.py --title "fix: ..." --body-file /tmp/body.md [--label improve-jensen]

Uses GH_TOKEN (or GITHUB_TOKEN) and GITHUB_REPOSITORY from the Actions environment, stdlib only.
If a PR from this branch is already open it is reused. Prints the PR URL.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")


def _call(method: str, path: str, token: str, payload: dict | None = None):
    req = urllib.request.Request(
        f"{API}{path}",
        method=method,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read() or b"null")


def open_pr(repo: str, token: str, head: str, base: str, title: str, body: str, labels: list[str]) -> str:
    owner = repo.split("/")[0]
    existing = _call("GET", f"/repos/{repo}/pulls?state=open&head={owner}:{head}", token)
    if existing:
        pr = existing[0]
    else:
        pr = _call("POST", f"/repos/{repo}/pulls", token, {"title": title, "body": body, "head": head, "base": base})
    if labels:
        _call("POST", f"/repos/{repo}/issues/{pr['number']}/labels", token, {"labels": labels})
    return pr["html_url"]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--title", required=True)
    p.add_argument("--body-file", required=True)
    p.add_argument("--base", default="main")
    p.add_argument("--head", help="defaults to the current branch")
    p.add_argument("--label", action="append", default=[])
    a = p.parse_args(argv)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        print("GH_TOKEN/GITHUB_TOKEN and GITHUB_REPOSITORY must be set", file=sys.stderr)
        return 2
    head = a.head or subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True).strip()
    with open(a.body_file) as f:
        body = f.read()
    print(open_pr(repo, token, head, a.base, a.title, body, a.label))
    return 0


if __name__ == "__main__":
    sys.exit(main())
