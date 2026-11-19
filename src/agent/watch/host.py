"""The host side of improve-jensen: everything it runs against the live stack's containers, Compose and `gh`.

Stdlib only — `Host` takes its `run` callable (defaulting to `subprocess.run`) as a constructor argument so tests
can swap in a fake that records calls instead of touching Docker or the network. `host.run` stays public (not
`_run`): later commands call it directly for a plain `docker inspect` or `docker exec` that doesn't fit one of the
named helpers here.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

from agent.secrets import load_secrets

# Where core_collect.py / core_ask.py actually live, so a caller can hand core_python() a bare file name.
_WATCH_DIR = Path(__file__).parent

CORE_CONTAINER = "agent-core"
RUNNER_CONTAINER = "agent-runner"

# docker exec's own complaints about the container, as opposed to the command inside it failing (a missing
# transcript file): seen verbatim in its stderr, lowercased for a case-insensitive match.
_CONTAINER_UNREACHABLE = ("no such container", "is not running")

# The runner token as `agent-runner` sees it: compose mounts `<live>/data/runner` at `/run/agent`, read-only.
_RUNNER_TOKEN_IN_RUNNER = "/run/agent/runner-token"


class Host:
    """The live install at `live` (`~/autonomous-agent` by default), driven through `run` (`subprocess.run` by
    default; tests inject a fake)."""

    def __init__(self, run: Callable[..., subprocess.CompletedProcess] = subprocess.run, *, live: Path):
        self.run = run
        self.live = Path(live)

    @classmethod
    def default(cls) -> Host:
        env = os.environ.get("IMPROVE_JENSEN_LIVE")
        live = Path(env) if env else Path.home() / "autonomous-agent"
        return cls(live=live)

    def core_python(self, module_file: str | Path, args: list[str]) -> str:
        """Run `module_file`'s text inside `agent-core` (`docker exec -i agent-core python - <args>`), the file
        piped on stdin so core never needs to carry or import it. A bare name (no directory) resolves relative to
        `src/agent/watch/`, where `core_collect.py` and `core_ask.py` live; an absolute path is used as given."""
        path = Path(module_file)
        if not path.is_absolute():
            path = _WATCH_DIR / path
        result = self.run(["docker", "exec", "-i", CORE_CONTAINER, "python", "-", *args],
                           input=path.read_text(), capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"docker exec returned {result.returncode}: {result.stderr}")
        return result.stdout

    def runner_python(self, module_file: str | Path, args: list[str]) -> str:
        """Run `module_file`'s text inside `agent-runner` (`docker exec -i agent-runner python3 - <args>`), the
        file piped on stdin so the runner never needs to carry or import it. Same contract as `core_python`
        (a bare name resolves relative to `src/agent/watch/`, where `runner_feedback.py` lives), except it runs
        `python3` rather than `python`: the runner's own interpreter is 3.12, invoked by that name there."""
        path = Path(module_file)
        if not path.is_absolute():
            path = _WATCH_DIR / path
        result = self.run(["docker", "exec", "-i", RUNNER_CONTAINER, "python3", "-", *args],
                           input=path.read_text(), capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"docker exec returned {result.returncode}: {result.stderr}")
        return result.stdout

    def transcripts(self, ids: list[str], dest: Path) -> dict[str, Path]:
        """Copy each id's runner transcript out of `agent-runner` into `dest/<id>.jsonl`. An id with no transcript
        file (the run never got that far, or it's already been pruned) is silently skipped; `agent-runner` itself
        being unreachable (stopped, removed) raises `RuntimeError` instead, since that's not "no transcript yet",
        it's "every id in this batch would look missing." Written as raw bytes — never decoded — so a transcript
        caught mid-write with a split multibyte character can't turn a copy into a crash."""
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        out: dict[str, Path] = {}
        for item_id in ids:
            result = self.run(["docker", "exec", RUNNER_CONTAINER, "cat", f"/var/agent/transcripts/{item_id}.jsonl"],
                               capture_output=True)
            if result.returncode != 0:
                stderr = (result.stderr or b"").decode(errors="replace")
                if any(phrase in stderr.lower() for phrase in _CONTAINER_UNREACHABLE):
                    raise RuntimeError(f"docker exec {RUNNER_CONTAINER} returned {result.returncode}: {stderr}")
                continue
            path = dest / f"{item_id}.jsonl"
            path.write_bytes(result.stdout)
            out[item_id] = path
        return out

    def jensen_gh(self, args: list[str], *, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
        """Run `gh <args>` as Jensen, with `GH_TOKEN` (from `<live>/data/secrets/agent.env`) passed only through
        `env=` — it must never appear in `args`, where a process listing or a log would show it. A missing or
        empty token raises rather than running `gh` at all: an unset `GH_TOKEN` makes `gh` fall through to
        `GITHUB_TOKEN` or whatever account is logged into this host, silently running as the wrong identity
        instead of failing — so `GITHUB_TOKEN` is dropped from the child's environment too. `capture=False` lets
        stdout/stderr stream straight to the terminal, for a `gh` passthrough a person is watching live."""
        path = self.live / "data" / "secrets" / "agent.env"
        token = load_secrets(path).get("GH_TOKEN", "")
        if not token:
            raise RuntimeError(f"GH_TOKEN missing from {path}")
        env = {**os.environ, "GH_TOKEN": token}
        env.pop("GITHUB_TOKEN", None)
        kwargs: dict = {"env": env, "check": check}
        if capture:
            kwargs.update(capture_output=True, text=True)
        return self.run(["gh", *args], **kwargs)

    def secret_values(self) -> list[str]:
        """Every secret value the live install holds, for `agent.scrub.scrub` to redact out of anything
        improve-jensen posts in public: each value in `<live>/data/secrets/*.env` (core's, Jensen's, the
        supervisors'), in `<live>/data/secrets/projects/*.env` (each product's own), and the whole of
        `<live>/data/runner/runner-token`. Any of them can turn up in a transcript. An empty list when none of
        those exist yet. The `.env` files are parsed with `agent.secrets.load_secrets`, the same stdlib-only parser
        core itself uses, so a quoted or `export`-prefixed value here is handled the same way it is everywhere
        else — a quoted `GH_TOKEN` doesn't silently break `jensen_gh`, and a value returned here never carries its
        surrounding quotes, which would make `scrub` miss the bare token inside a transcript or digest.

        Never raises: the hourly measure calls this first thing, and a crash there would wedge every hour after.
        Each source is read on its own; one that can't be read is skipped with a line on stderr naming only the
        source and the exception's type. The runner token is written 0600 for the agent container's own uid, which
        the watch on the host may not be, so a token file that can't be read here is read as its owner instead,
        through `docker exec agent-runner cat /run/agent/runner-token`."""
        secrets = self.live / "data" / "secrets"
        values: list[str] = []
        for folder in (secrets, secrets / "projects"):
            try:
                paths = sorted(folder.glob("*.env")) if folder.is_dir() else []
            except Exception as exc:
                self._skip_secret_source(folder, exc)
                continue
            for path in paths:
                try:
                    values.extend(load_secrets(path).values())
                except Exception as exc:
                    self._skip_secret_source(path, exc)
        token = self._runner_token()
        if token:
            values.append(token)
        return [value for value in values if value]

    def _runner_token(self) -> str | None:
        path = self.live / "data" / "runner" / "runner-token"
        try:
            return path.read_text().strip() if path.is_file() else None
        except Exception as exc:
            unreadable = exc
        try:
            result = self.run(["docker", "exec", RUNNER_CONTAINER, "cat", _RUNNER_TOKEN_IN_RUNNER],
                              capture_output=True, text=True)
            if result.returncode == 0 and (result.stdout or "").strip():
                return result.stdout.strip()
        except Exception:
            pass
        self._skip_secret_source(path, unreadable, also="reading it inside agent-runner failed too")
        return None

    def _skip_secret_source(self, path: Path, exc: BaseException, also: str = "") -> None:
        try:
            source = path.relative_to(self.live)
        except ValueError:
            source = path.name
        extra = f"; {also}" if also else ""
        print(f"warning: skipped {source} in the secret scrub ({type(exc).__name__}{extra})", file=sys.stderr)

    def compose(self, args: list[str]) -> subprocess.CompletedProcess:
        """Run `docker compose <args>` from the live install's own directory, the way a person would by hand."""
        return self.run(["docker", "compose", *args], cwd=self.live)
