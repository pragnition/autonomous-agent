"""The state folder: where improve-jensen keeps its windows, metrics, evidence, plan and deploy flag on the host.

Everything the hourly watch remembers between runs lives under one root directory (`State.default()` is
`~/.local/state/improve-jensen`, overridden by env `IMPROVE_JENSEN_STATE`), as a handful of small files:
`state.json` (the window boundary, known clones, and Jensen's notes' own, separate cursor --
`notes_committed_end`/`notes_pending_end`, two-phase exactly like the main window but never held back by it or
holding it back: see `hold_window`'s own warning about why -- and whether the last notes read failed),
`metrics.jsonl` (one line per turn, midturn or PR, stamped with when it happened and read back by `count`),
`pending-metrics.jsonl`/`pending-clones.json` (this window's
not-yet-final measurement, written by `stage`), `evidence.jsonl` (one line per `note`), `deploys.jsonl` (one line
per `log_deploy`), `deploy-pending` (present means a deploy is pending -- its own file, not a `state.json` key,
since `deploy.py` runs as a separate process from the hourly watch and a shared read-modify-write could race
against this watch's own `commit()`), `plan.json` (written by Claude, read and validated by `plan`, removed by
`clear_plan` once a new hour is handed to Claude), and `digest-<end>.md` files. Every whole-file write replaces the
previous value atomically (`*.tmp` then `os.replace`), so a process killed mid-write never leaves a torn file for
the next hourly run to trip over.

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
_DEPLOY_PENDING_FILE = "deploy-pending"
_PLAN_FILE = "plan.json"
_DIGEST_PREFIX = "digest-"
_DIGEST_SUFFIX = ".md"
_DIGEST_FMT = "%Y%m%dT%H%MZ"
_RUNS_DIR = "runs"
_RUN_REVIEWS_SUFFIX = "-reviews.json"
_ASSESS_PLAN_FILE = "assess-plan.json"
_PROJECT_ACTIVE_FILE = "project-active.json"

# Numeric per-turn/per-midturn signals `count` reports as n/median/p90/max, and which record `kind` they live on.
_TURN_NUMERIC = {"context_first", "context_peak", "inline_code_chars"}
_MIDTURN_NUMERIC = {"midturn_wait_s"}
# Counted signals `count` reports as total/turns. All live on `kind == "turn"` records.
_LIST_VALUED = {"tool_errors", "missing_tools"}
_COUNTED = _LIST_VALUED | {
    "lost_background", "still_running", "turn_failed", "no_delegation", "big_start_small_ask", "reclone",
    "session_restarted", "account_moved",
}

_MAX_PLAN_REVIEWS = 10
# `validate_proposals`: at most this many proposed changes survive, and each `change` is cut to this many
# characters (ending in `…` when it is).
_MAX_PROPOSALS = 5
_PROPOSAL_CHANGE_MAX = 140
# A proposal's optional `credit` (whose session a learned change came from, for Slack only) is cut to this many.
_PROPOSAL_CREDIT_MAX = 60
_LEARNED = "learned"

# improve-jensen's own traffic: the turns it queues into Jensen (`scripts/ask_jensen.py` marks them with this source) and
# anything in the fresh `V-review-*` conversations it opens. Kept in the metrics and shown in the digest, but
# never counted as evidence about Jensen -- improve-jensen must not grade its own reviews and live checks as
# Jensen's work.
IMPROVE_JENSEN_SOURCE = "improve-jensen"
_REVIEW_CONVERSATION_PREFIX = "V-review-"


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


def is_improve_jensens_own(record: dict) -> bool:
    """Whether a turn or midturn record is improve-jensen's own traffic (see `IMPROVE_JENSEN_SOURCE`). A PR record
    never is: a PR Jensen opens while working on a review's findings is still Jensen's shipped work."""
    if record.get("kind") not in ("turn", "midturn"):
        return False
    return (record.get("source") == IMPROVE_JENSEN_SOURCE
            or str(record.get("conversation") or "").startswith(_REVIEW_CONVERSATION_PREFIX))


def _collapsed(value: object, limit: int) -> str | None:
    """`value` with its whitespace collapsed (runs folded to single spaces, then stripped) and cut to `limit`
    characters, ending in `…` when it had to be; `None` when it isn't a string or nothing is left."""
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    if not value:
        return None
    if len(value) > limit:
        value = value[:limit - 1].rstrip() + "…"
    return value


def validate_proposals(value: object) -> list[dict]:
    """A raw `propose` value (from `plan.json` or `assess-plan.json`) validated into `[{"issue": int, "change":
    str, "kind"?: "learned", "credit"?: str}, ...]`: anything that isn't a list falls back to `[]`. An entry
    survives only when its `issue` is a positive int (a bool doesn't count, even though `bool` is an `int`
    subclass) and its `change` is a non-empty string once its whitespace is collapsed (runs of whitespace folded
    to single spaces, then stripped); `change` is then cut to `_PROPOSAL_CHANGE_MAX` characters, ending in `…`
    when it had to be. `kind` is kept only when it's exactly `"learned"`, and `credit` only when it's a non-empty
    string the same way, cut to `_PROPOSAL_CREDIT_MAX`; either key is left out otherwise, never costing the entry.
    The first entry for a given `issue` wins over any later one for the same issue, and the result is cut to the
    first `_MAX_PROPOSALS` that remain."""
    if not isinstance(value, list):
        return []
    seen: set[int] = set()
    out: list[dict] = []
    for entry in value:
        if len(out) >= _MAX_PROPOSALS:
            break
        if not isinstance(entry, dict):
            continue
        issue = entry.get("issue")
        if isinstance(issue, bool) or not isinstance(issue, int) or issue <= 0 or issue in seen:
            continue
        change = _collapsed(entry.get("change"), _PROPOSAL_CHANGE_MAX)
        if change is None:
            continue
        seen.add(issue)
        proposal = {"issue": issue, "change": change}
        if entry.get("kind") == _LEARNED:
            proposal["kind"] = _LEARNED
        credit = _collapsed(entry.get("credit"), _PROPOSAL_CREDIT_MAX)
        if credit is not None:
            proposal["credit"] = credit
        out.append(proposal)
    return out


def _latest_per_pr(records: list[dict]) -> list[dict]:
    """One record per PR URL: the last one in `records` order (committed metrics, then staged), which is the most
    recent measurement of it. A PR gets a record in every window it's updated in."""
    latest: dict[object, dict] = {}
    for record in records:
        if record.get("kind") == "pr":
            latest.pop(record.get("url"), None)
            latest[record.get("url")] = record
    return list(latest.values())


def _count_pr(records: list[dict], label: str) -> str:
    prs = _latest_per_pr(records)
    merged = [r for r in prs if r.get("open_to_merge_s") is not None]
    merged_without_passing_ci = sum(1 for r in merged if r.get("ci") not in ("passed", "unknown"))
    ci_unknown = sum(1 for r in prs if r.get("ci") == "unknown")
    _, median_lines, _, _ = _stat([r["lines"] for r in prs if r.get("lines") is not None])
    _, median_open_to_merge_s, _, _ = _stat([r["open_to_merge_s"] for r in merged])
    coauthor_trailers = sum(r.get("coauthor_trailers") or 0 for r in prs)
    return (f"pr since {label}: prs={len(prs)} merged_without_passing_ci={merged_without_passing_ci} "
            f"ci_unknown={ci_unknown} median_lines={median_lines} median_open_to_merge_s={median_open_to_merge_s} "
            f"coauthor_trailers={coauthor_trailers}")


def summarize(records: list[dict], signal: str, label: str) -> str:
    """The human-readable evidence line for `signal` over an already-chosen list of `records`, labelled `label`
    (normally an ISO timestamp). `State.count` builds that list itself (committed plus staged metrics since a
    `since` boundary); a digest's own totals section instead summarizes exactly the records one `measure` run
    just produced, never a disk read over a time range -- a window measured again before it was committed would
    otherwise pick up its own earlier, abandoned staging as well.

    improve-jensen's own turns and midturns (`is_improve_jensens_own`) are left out of every signal.

    Numeric signals (`context_first`, `context_peak`, `inline_code_chars` from `kind == "turn"` records;
    `midturn_wait_s` from `kind == "midturn"` records) print `n`, `median`, `p90` and `max` (see `_stat`).
    Counted signals (`lost_background`, `still_running`, `turn_failed`, `no_delegation`, `big_start_small_ask`,
    `reclone`, `session_restarted`, `account_moved`, `tool_errors`, `missing_tools`, all from `kind == "turn"` records) print `total` (the sum -- for
    `tool_errors`/`missing_tools`, the sum of each record's list length) and `turns` (records greater than zero,
    or with a non-empty list). `pr` (from `kind == "pr"` records, only the newest record of each PR URL) prints
    `prs`, `merged_without_passing_ci` (`open_to_merge_s` is not null and `ci` is neither `passed` nor
    `unknown`), `ci_unknown` (PRs whose CI couldn't be read -- never counted as a failure), `median_lines`,
    `median_open_to_merge_s` and `coauthor_trailers` (the sum of each PR's `coauthor_trailers`, a record missing
    it counted as 0). An unknown signal raises `ValueError`.
    """
    records = [r for r in records if not is_improve_jensens_own(r)]
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

    def hold_window(self) -> None:
        """Keep the window being measured open: the next `commit()` leaves `committed_end` where it is, so the
        next window starts from the same place. For an hour whose finished turns couldn't be read at all (core
        unreachable) -- committing it would skip them for good.

        Never use this for a source that doesn't zero out `items` the way core being unreachable does: holding
        the *main* window back while turns/PRs core *did* hand over this hour still get staged and committed
        would mean the next window re-fetches (and, since `metrics.jsonl` only ever appends, re-commits) those
        same turns, compounding every hour the other source stays down. Jensen's notes have their own,
        independent cursor (`notes_committed_end`/`stage_notes_end`) for exactly this reason."""
        data = self._read_state()
        data["pending_end"] = data.get("committed_end")
        self._write_state(data)

    def notes_committed_end(self) -> datetime | None:
        """The end of the last window Jensen's notes were successfully read through, or `None` if they never
        have been. This is notes' own cursor, separate from the main window's `committed_end`: a failed notes
        read must not hold the *main* window back (see `hold_window`'s warning), so notes need a boundary of
        their own that only moves forward on a successful read, and that can lag behind the main window for as
        long as notes keep failing."""
        committed = self._read_state().get("notes_committed_end")
        return _parse(committed) if committed else None

    def stage_notes_end(self, end: datetime) -> None:
        """Record `end` as this window's notes cursor, staged until the next `commit()`. Call this only after a
        successful notes read -- a failed one must leave `notes_pending_end` exactly as it was (whatever an
        earlier successful attempt staged, or nothing), so the next attempt still starts from the last point
        notes were actually read, covering the whole gap in between once it succeeds again."""
        data = self._read_state()
        data["notes_pending_end"] = _iso(end)
        self._write_state(data)

    def notes_failing(self) -> bool:
        """Whether the notes read failed in the last committed window, so a failure now is no longer news."""
        return self._read_state().get("notes_failing") is True

    def stage_notes_failing(self, failing: bool) -> None:
        """Record whether this window's notes read failed, staged until the next `commit()` the same way the notes
        cursor is: a window measured again before it is committed still sees the streak as it stood before it."""
        data = self._read_state()
        data["notes_failing_pending"] = failing
        self._write_state(data)

    def commit(self) -> None:
        """Make the last window's end the new starting point (`committed_end = pending_end`), and fold whatever
        is staged for it into the permanent record: staged metrics are appended to `metrics.jsonl`, staged
        clones merge into `known_clones`, and both staging files are cleared. A window with nothing staged (no
        `stage()` call ever happened, or a previous `commit()` already cleared it) only moves the window
        boundary -- there is nothing to fold in, and that's fine.

        The notes cursor moves the same way, but only when there's something staged for it: `notes_committed_end
        = notes_pending_end` when a `stage_notes_end` call landed one (a successful notes read happened at some
        point since the last commit), then `notes_pending_end` is cleared either way -- so a run whose notes kept
        failing right up to this commit leaves `notes_committed_end` exactly where it was, lagging behind the
        main window until notes finally succeed. Whether this window's notes read failed (`stage_notes_failing`)
        becomes `notes_failing()` the same way."""
        data = self._read_state()
        data["committed_end"] = data.get("pending_end")
        if data.get("notes_pending_end"):
            data["notes_committed_end"] = data["notes_pending_end"]
        data.pop("notes_pending_end", None)
        if "notes_failing_pending" in data:
            data["notes_failing"] = data.pop("notes_failing_pending") is True
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
        """Append one evidence line for `issue` and return that issue's running total, this note included. The
        total counts distinct refs: the same turn or PR noted twice (a re-measured window, or a review and the watch
        both seeing it) is still one occurrence."""
        path = self.root / _EVIDENCE_FILE
        _append_jsonl(path, {"ts": _iso(now), "issue": issue, "ref": ref, "line": line})
        return len({r.get("ref") for r in _read_jsonl(path) if r.get("issue") == issue})

    # -- digests --

    def write_digest(self, end: datetime, text: str) -> Path:
        name = f"{_DIGEST_PREFIX}{end.astimezone(UTC).strftime(_DIGEST_FMT)}{_DIGEST_SUFFIX}"
        path = self.root / name
        _atomic_write(path, text)
        return path

    def digest_newer_than(self, ts: datetime) -> bool:
        """Whether any digest was written after `ts`, going by the file's own mtime (not the end time baked into
        its name -- a window measured late can write a digest well after the hour it's named for)."""
        cutoff = ts.timestamp()
        for path in self.root.glob(f"{_DIGEST_PREFIX}*{_DIGEST_SUFFIX}"):
            try:
                if path.stat().st_mtime > cutoff:
                    return True
            except OSError:
                continue
        return False

    def prune(self, now: datetime, days: int = 30) -> None:
        """Drop digests older than `days`, metric lines whose `ts` is older than `days`, and `runs/*-reviews.json`
        files older than `days` (a run id isn't a timestamp, so age there is the file's own mtime)."""
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

        runs_dir = self.root / _RUNS_DIR
        if runs_dir.exists():
            for path in runs_dir.glob(f"*{_RUN_REVIEWS_SUFFIX}"):
                try:
                    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
                except OSError:
                    continue
                if mtime < cutoff:
                    path.unlink()

    # -- plan --

    def clear_plan(self) -> None:
        """Remove `plan.json`. `measure` calls this whenever it hands Claude a new hour, so a run that succeeds
        without writing a plan never re-dispatches the previous hour's reviews and proposed changes."""
        (self.root / _PLAN_FILE).unlink(missing_ok=True)

    def plan(self, run_id: str | None = None) -> dict:
        """The validated `plan.json`: `{"reviews": [...], "propose": [Proposal]}`.

        A missing file or invalid JSON falls back to `{"reviews": [], "propose": []}`. Otherwise: each review is
        normalised to exactly `{"url": str, "conversation": str | None}` -- a review without a string `url` is
        dropped entirely, and its own `conversation` is kept only when it's a string (never a plan-level field
        shared across every review -- each review names its own conversation, or none). A URL listed twice is kept
        once, and a URL already in another run's `runs/*-reviews.json` is dropped (that PR was already reviewed;
        `run_id`'s own file doesn't count, so the same run attempted again plans the same reviews). The list is
        cut to the first 10 that remain. `propose` is validated by the module-level `validate_proposals`.
        """
        fallback = {"reviews": [], "propose": []}
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

        skip = self._reviewed_urls(except_run=run_id)
        reviews = []
        for entry in (data.get("reviews") or []):
            if not isinstance(entry, dict) or not isinstance(entry.get("url"), str) or entry["url"] in skip:
                continue
            skip.add(entry["url"])
            conversation = entry.get("conversation")
            reviews.append({"url": entry["url"],
                            "conversation": conversation if isinstance(conversation, str) else None})

        return {"reviews": reviews[:_MAX_PLAN_REVIEWS], "propose": validate_proposals(data.get("propose"))}

    # -- run reviews (so a scheduled review job never names a PR URL in the workflow's own prompt/logs) --

    def _run_reviews_path(self, run_id: str) -> Path:
        return self.root / _RUNS_DIR / f"{run_id}{_RUN_REVIEWS_SUFFIX}"

    def write_run_reviews(self, run_id: str, reviews: list[dict]) -> Path:
        """Write this run's validated review list (see `plan()`) to `runs/<run_id>-reviews.json`. A later
        `review-target --run <run_id> --index <i>` call -- made by Claude itself, inside the review job, never
        printed into a prompt a public Actions log would show -- looks a review's URL and conversation back up
        from this file."""
        path = self._run_reviews_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, json.dumps(reviews))
        return path

    def _reviewed_urls(self, except_run: str | None) -> set[str]:
        """Every review URL in `runs/*-reviews.json`, leaving out `except_run`'s own file."""
        urls: set[str] = set()
        runs_dir = self.root / _RUNS_DIR
        if not runs_dir.is_dir():
            return urls
        own = self._run_reviews_path(except_run).name if except_run is not None else None
        for path in runs_dir.glob(f"*{_RUN_REVIEWS_SUFFIX}"):
            if path.name == own:
                continue
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, list):
                urls.update(item["url"] for item in data if isinstance(item, dict) and isinstance(item.get("url"), str))
        return urls

    def read_run_review(self, run_id: str, index: int) -> dict | None:
        """The review at `index` in `runs/<run_id>-reviews.json`, or `None` if the file is missing, unreadable,
        not a list, or `index` is out of range."""
        try:
            data = json.loads(self._run_reviews_path(run_id).read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, list) or not (0 <= index < len(data)):
            return None
        item = data[index]
        return item if isinstance(item, dict) else None

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
        """The pending flag lives in its own file (`deploy-pending`, present means pending), not a key in
        `state.json` -- `deploy.py` runs as a separate process from the hourly watch, and `commit()` rewrites
        `state.json` from that other process; two read-modify-writes to the same file from different processes
        could otherwise race and drop one or the other's change. A bare file's existence has no such race: the
        write here is a single filesystem call (`touch`/`unlink`), never a read-modify-write of shared content."""
        path = self.root / _DEPLOY_PENDING_FILE
        if flag:
            path.touch()
        else:
            path.unlink(missing_ok=True)

    def deploy_pending(self) -> bool:
        return (self.root / _DEPLOY_PENDING_FILE).exists()

    def log_deploy(self, record: dict) -> None:
        _append_jsonl(self.root / _DEPLOYS_FILE, record)

    def last_deploy(self) -> dict | None:
        """The newest `deploys.jsonl` record, or `None` with none yet."""
        records = _read_jsonl(self.root / _DEPLOYS_FILE)
        return records[-1] if records else None

    def deploys_between(self, start: datetime, end: datetime) -> list[dict]:
        """The `deploys.jsonl` records whose `ts` is in `(start, end]`, oldest first; one without a parseable `ts`
        is left out."""
        out = []
        for record in _read_jsonl(self.root / _DEPLOYS_FILE):
            try:
                ts = _parse(record["ts"])
            except (KeyError, TypeError, ValueError):
                continue
            if start < ts <= end:
                out.append(record)
        return out

    # -- assessment (what Jensen needs) --

    def last_assess(self) -> datetime | None:
        """When the assessment last ran to completion, or `None` if it never has."""
        value = self._read_state().get("last_assess")
        return _parse(value) if value else None

    def set_last_assess(self, ts: datetime) -> None:
        data = self._read_state()
        data["last_assess"] = _iso(ts)
        self._write_state(data)

    def assess_plan(self) -> dict:
        """The validated `assess-plan.json`: `{"propose": [Proposal]}`. A missing file, invalid JSON, or a value
        that isn't an object all fall back to `{"propose": []}`; `propose` itself is validated by the
        module-level `validate_proposals`."""
        path = self.root / _ASSESS_PLAN_FILE
        try:
            raw = path.read_text()
        except OSError:
            return {"propose": []}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"propose": []}
        if not isinstance(data, dict):
            return {"propose": []}
        return {"propose": validate_proposals(data.get("propose"))}

    def clear_assess_plan(self) -> None:
        """Remove `assess-plan.json` -- `propose --from assess` calls this right after reading it, successful or
        not, so this run's proposed changes are spent either way and never re-read by a later run."""
        (self.root / _ASSESS_PLAN_FILE).unlink(missing_ok=True)

    # -- project tracking (deterministic: a file, not a search over issue comments) --

    def project_mark(self, issue: int, status: str, now: datetime) -> None:
        """`status == "active"` writes `project-active.json` (`{issue, started}`), replacing whatever was
        there; `status == "done"` removes it. Any other `status` raises `ValueError` -- the CLI's own
        `--status` choices already keep this from happening through normal use."""
        if status == "active":
            _atomic_write(self.root / _PROJECT_ACTIVE_FILE, json.dumps({"issue": issue, "started": _iso(now)}))
        elif status == "done":
            (self.root / _PROJECT_ACTIVE_FILE).unlink(missing_ok=True)
        else:
            raise ValueError(f"bad project status: {status!r}")

    def project_active(self) -> dict | None:
        """The active project (`project-active.json`), or `None` if there isn't one or it can't be read."""
        path = self.root / _PROJECT_ACTIVE_FILE
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None
