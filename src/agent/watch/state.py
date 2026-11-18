"""The state folder: where improve-jensen keeps its windows, metrics, evidence, plan and deploy flag on the host.

Everything the hourly watch remembers between runs lives under one root directory (`State.default()` is
`~/.local/state/improve-jensen`, overridden by env `IMPROVE_JENSEN_STATE`), as a handful of small files:
`state.json` (the window boundary, known clones and the deploy-pending flag), `metrics.jsonl` (one line per turn,
midturn or PR, read back by `count`), `pending-metrics.jsonl`/`pending-clones.json` (this window's not-yet-final
measurement, written by `stage`), `evidence.jsonl` (one line per `note`), `deploys.jsonl` (one line per
`log_deploy`), `plan.json` (written by Claude, read and validated by `plan`), and `digest-<end>.md` files. Every
whole-file write replaces the previous value atomically (`*.tmp` then `os.replace`), so a process killed mid-write
never leaves a torn file for the next hourly run to trip over.

Measuring a window and committing it are two separate steps, because the digest a measurement produces is meant
to be acted on (by Claude, against that digest) before the window is final -- and that action can fail. `stage`
records this window's metrics and newly-seen clones where `read_metrics`/`count` can already see them (so
evidence gathered mid-action still reflects this window), but `known_clones()` and the permanent `metrics.jsonl`
stay untouched until `commit()` folds the staged data in for good. If the action on a digest fails and the same
window gets measured again before anything is committed, `stage` simply overwrites what an earlier attempt
staged rather than piling another copy on top, and a turn's own clone can't be mistaken for a reclone of itself
just because an earlier, abandoned attempt at the same window already "saw" it.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

FMT = "%Y-%m-%dT%H:%M:%SZ"

_STATE_FILE = "state.json"
_METRICS_FILE = "metrics.jsonl"
_PENDING_METRICS_FILE = "pending-metrics.jsonl"
_PENDING_CLONES_FILE = "pending-clones.json"
_EVIDENCE_FILE = "evidence.jsonl"
_DEPLOYS_FILE = "deploys.jsonl"
_PLAN_FILE = "plan.json"
_DIGEST_PREFIX = "digest-"
_DIGEST_SUFFIX = ".md"
_DIGEST_FMT = "%Y%m%dT%H%MZ"

# Numeric per-turn/per-midturn signals `count` reports as n/median/p90/max, and which record `kind` they live on.
_TURN_NUMERIC = {"context_first", "context_peak", "inline_code_chars"}
_MIDTURN_NUMERIC = {"midturn_wait_s"}
# Counted signals `count` reports as total/turns. All live on `kind == "turn"` records.
_LIST_VALUED = {"tool_errors", "missing_tools"}
_COUNTED = _LIST_VALUED | {
    "lost_background", "still_running", "turn_failed", "no_delegation", "big_start_small_ask", "reclone",
}

_MAX_PLAN_REVIEWS = 10


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime(FMT)


def _parse(value: str) -> datetime:
    return datetime.strptime(value, FMT).replace(tzinfo=UTC)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _append_jsonl(path: Path, record: dict) -> None:
    with path.open("a") as fh:
        fh.write(json.dumps(record) + "\n")


def _stat(values: list) -> tuple[int, object, object, object]:
    """(n, median, p90, max) over `values`, or zeroes when there are none.

    `median` is the middle value of the sorted list; for an even count it is the *lower* of the two middle values
    (never an average), so it is always one of the recorded values and consistent with `p90`'s nearest-rank method.
    `p90` is nearest-rank: the value at sorted position `ceil(0.9 * n)` (1-indexed) — the smallest value that is
    greater than or equal to at least 90% of the sample.
    """
    if not values:
        return 0, 0, 0, 0
    ordered = sorted(values)
    n = len(ordered)
    median = ordered[(n - 1) // 2]
    p90 = ordered[math.ceil(0.9 * n) - 1]
    return n, median, p90, ordered[-1]


def _count_numeric(signal: str, records: list[dict], kind: str, label: str) -> str:
    values = [r[signal] for r in records if r.get("kind") == kind and signal in r]
    n, median, p90, maximum = _stat(values)
    return f"{signal} since {label}: n={n} median={median} p90={p90} max={maximum}"


def _count_counted(signal: str, records: list[dict], label: str) -> str:
    turn_records = [r for r in records if r.get("kind") == "turn" and signal in r]
    if signal in _LIST_VALUED:
        total = sum(len(r[signal]) for r in turn_records)
        turns = sum(1 for r in turn_records if len(r[signal]) > 0)
    else:
        total = sum(r[signal] for r in turn_records)
        turns = sum(1 for r in turn_records if r[signal] > 0)
    return f"{signal} since {label}: total={total} turns={turns}"


def _count_pr(records: list[dict], label: str) -> str:
    prs = [r for r in records if r.get("kind") == "pr"]
    merged_without_passing_ci = sum(1 for r in prs
                                    if r.get("open_to_merge_s") is not None and r.get("ci") != "passed")
    _, median_lines, _, _ = _stat([r["lines"] for r in prs if r.get("lines") is not None])
    _, median_open_to_merge_s, _, _ = _stat([r["open_to_merge_s"] for r in prs
                                             if r.get("open_to_merge_s") is not None])
    return (f"pr since {label}: prs={len(prs)} merged_without_passing_ci={merged_without_passing_ci} "
            f"median_lines={median_lines} median_open_to_merge_s={median_open_to_merge_s}")


def summarize(records: list[dict], signal: str, label: str) -> str:
    """The human-readable evidence line for `signal` over an already-chosen list of `records`, labelled `label`
    (normally an ISO timestamp). `State.count` builds that list itself (committed plus staged metrics since a
    `since` boundary); a digest's own totals section instead summarizes exactly the records one `measure` run
    just produced, never a disk read over a time range -- the same window measured twice would otherwise double
    up on what a read-since-`start` picks up, since every record's `ts` is the measurement's own clock, not the
    event's.

    Numeric signals (`context_first`, `context_peak`, `inline_code_chars` from `kind == "turn"` records;
    `midturn_wait_s` from `kind == "midturn"` records) print `n`, `median`, `p90` and `max` (see `_stat`).
    Counted signals (`lost_background`, `still_running`, `turn_failed`, `no_delegation`, `big_start_small_ask`,
    `reclone`, `tool_errors`, `missing_tools`, all from `kind == "turn"` records) print `total` (the sum -- for
    `tool_errors`/`missing_tools`, the sum of each record's list length) and `turns` (records greater than zero,
    or with a non-empty list). `pr` (from `kind == "pr"` records) prints `prs`, `merged_without_passing_ci`
    (`open_to_merge_s` is not null and `ci != "passed"`), `median_lines` and `median_open_to_merge_s`.
    An unknown signal raises `ValueError`.
    """
    if signal == "pr":
        return _count_pr(records, label)
    if signal in _TURN_NUMERIC:
        return _count_numeric(signal, records, "turn", label)
    if signal in _MIDTURN_NUMERIC:
        return _count_numeric(signal, records, "midturn", label)
    if signal in _COUNTED:
        return _count_counted(signal, records, label)
    raise ValueError(f"unknown signal: {signal!r}")


class State:
    """improve-jensen's on-host state folder, rooted at `root` (created if missing)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def default(cls) -> State:
        env = os.environ.get("IMPROVE_JENSEN_STATE")
        root = Path(env) if env else Path.home() / ".local" / "state" / "improve-jensen"
        return cls(root)

    # -- state.json --

    def _state_path(self) -> Path:
        return self.root / _STATE_FILE

    def _read_state(self) -> dict:
        path = self._state_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_state(self, data: dict) -> None:
        _atomic_write(self._state_path(), json.dumps(data))

    # -- window / commit --

    def window(self, now: datetime) -> tuple[datetime, datetime]:
        """The window to measure: start is the last committed end, or `now - 1h` with no state yet; end is `now`.
        `now` is recorded as `pending_end`, so a later `commit()` can make it the new `committed_end`."""
        data = self._read_state()
        committed = data.get("committed_end")
        start = _parse(committed) if committed else now - timedelta(hours=1)
        data["pending_end"] = _iso(now)
        self._write_state(data)
        return start, now

    def commit(self) -> None:
        """Make the last window's end the new starting point (`committed_end = pending_end`), and fold whatever
        is staged for it into the permanent record: staged metrics are appended to `metrics.jsonl`, staged
        clones merge into `known_clones`, and both staging files are cleared. A window with nothing staged (no
        `stage()` call ever happened, or a previous `commit()` already cleared it) only moves the window
        boundary -- there is nothing to fold in, and that's fine."""
        data = self._read_state()
        data["committed_end"] = data.get("pending_end")
        self._write_state(data)

        staged_metrics = self._read_pending_metrics()
        if staged_metrics:
            self.append_metrics(staged_metrics)
        staged_clones = self._read_pending_clones()
        if staged_clones:
            self.add_clones(staged_clones)
        for name in (_PENDING_METRICS_FILE, _PENDING_CLONES_FILE):
            path = self.root / name
            if path.exists():
                path.unlink()

    # -- metrics --

    def append_metrics(self, records: list[dict]) -> None:
        if not records:
            return
        path = self.root / _METRICS_FILE
        with path.open("a") as fh:
            for record in records:
                fh.write(json.dumps(record) + "\n")

    def stage(self, records: list[dict], clones: Iterable[str]) -> None:
        """Overwrite this window's staged metrics and newly-seen clones (atomic writes), replacing whatever an
        earlier, not-yet-committed attempt at this same window staged. Staged records are visible to
        `read_metrics`/`count` right away; staged clones are not visible to `known_clones()` at all until
        `commit()` merges them in. Call this every time `measure` has work to report, even with an empty
        `records`/`clones` -- the overwrite is what clears out a stale staging from a previous attempt."""
        _atomic_write(self.root / _PENDING_METRICS_FILE, "".join(json.dumps(r) + "\n" for r in records))
        _atomic_write(self.root / _PENDING_CLONES_FILE, json.dumps(sorted(set(clones))))

    def _read_pending_metrics(self) -> list[dict]:
        return _read_jsonl(self.root / _PENDING_METRICS_FILE)

    def _read_pending_clones(self) -> set[str]:
        path = self.root / _PENDING_CLONES_FILE
        if not path.exists():
            return set()
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return set()
        return set(data) if isinstance(data, list) else set()

    def read_metrics(self, since: datetime) -> list[dict]:
        records = _read_jsonl(self.root / _METRICS_FILE) + self._read_pending_metrics()
        return [r for r in records if r.get("ts") and _parse(r["ts"]) >= since]

    # -- evidence --

    def count(self, signal: str, since: datetime) -> str:
        """The human-readable evidence line for `signal` over committed-plus-staged metrics since `since`. See
        `summarize` for what each signal prints; an unknown signal raises `ValueError`."""
        return summarize(self.read_metrics(since), signal, _iso(since))

    def note(self, issue: int, ref: str, line: str, now: datetime) -> int:
        """Append one evidence line for `issue` and return that issue's running total, this note included."""
        path = self.root / _EVIDENCE_FILE
        _append_jsonl(path, {"ts": _iso(now), "issue": issue, "ref": ref, "line": line})
        return sum(1 for r in _read_jsonl(path) if r.get("issue") == issue)

    # -- digests --

    def write_digest(self, end: datetime, text: str) -> Path:
        name = f"{_DIGEST_PREFIX}{end.astimezone(UTC).strftime(_DIGEST_FMT)}{_DIGEST_SUFFIX}"
        path = self.root / name
        _atomic_write(path, text)
        return path

    def prune(self, now: datetime, days: int = 30) -> None:
        """Drop digests older than `days`, and metric lines whose `ts` is older than `days`."""
        cutoff = now - timedelta(days=days)
        for path in self.root.glob(f"{_DIGEST_PREFIX}*{_DIGEST_SUFFIX}"):
            stamp = path.name[len(_DIGEST_PREFIX):-len(_DIGEST_SUFFIX)]
            try:
                end = datetime.strptime(stamp, _DIGEST_FMT).replace(tzinfo=UTC)
            except ValueError:
                continue
            if end < cutoff:
                path.unlink()

        metrics_path = self.root / _METRICS_FILE
        if metrics_path.exists():
            kept = [r for r in _read_jsonl(metrics_path) if r.get("ts") and _parse(r["ts"]) >= cutoff]
            _atomic_write(metrics_path, "".join(json.dumps(r) + "\n" for r in kept))

    # -- plan --

    def plan(self) -> dict:
        """The validated `plan.json`: `{"reviews": [...], "fix": int | None, "conversation": str | None}`.

        A missing file or invalid JSON falls back to `{"reviews": [], "fix": None}` (no `conversation` key at all —
        there was nothing to read a conversation out of). Otherwise: reviews that aren't a dict with a string `url`
        are dropped, and the list is cut to the first 10 that remain; `fix` is kept only when it's a positive int
        (a bool doesn't count, even though `bool` is a `int` subclass); `conversation` is kept only when it's a
        string.
        """
        fallback = {"reviews": [], "fix": None}
        path = self.root / _PLAN_FILE
        try:
            raw = path.read_text()
        except OSError:
            return fallback
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return fallback
        if not isinstance(data, dict):
            return fallback

        reviews = [r for r in (data.get("reviews") or []) if isinstance(r, dict) and isinstance(r.get("url"), str)]
        fix = data.get("fix")
        if isinstance(fix, bool) or not isinstance(fix, int) or fix <= 0:
            fix = None
        conversation = data.get("conversation")
        if not isinstance(conversation, str):
            conversation = None
        return {"reviews": reviews[:_MAX_PLAN_REVIEWS], "fix": fix, "conversation": conversation}

    # -- known clones --

    def known_clones(self) -> set[str]:
        return set(self._read_state().get("known_clones", []))

    def add_clones(self, repos: Iterable[str]) -> None:
        data = self._read_state()
        clones = set(data.get("known_clones", []))
        clones.update(repos)
        data["known_clones"] = sorted(clones)
        self._write_state(data)

    # -- deploy --

    def set_deploy_pending(self, flag: bool) -> None:
        data = self._read_state()
        data["deploy_pending"] = bool(flag)
        self._write_state(data)

    def deploy_pending(self) -> bool:
        return bool(self._read_state().get("deploy_pending", False))

    def log_deploy(self, record: dict) -> None:
        _append_jsonl(self.root / _DEPLOYS_FILE, record)
