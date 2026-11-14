"""Consolidation: turning a finished task or thread into an episode note and updated facts."""

from __future__ import annotations


from pathlib import Path

from agent.memory.citations import CitationChecker
from agent.memory.scrub import scrub
from agent.memory.store import MAX_SUMMARY, MemoryError, MemoryStore, Note, NoteInput
from agent.tokens import estimate_tokens

MAX_INPUT_TOKENS = 8000
CONSOLIDATION_SCHEMA = {
    "type": "object",
    "required": ["episode", "upserts"],
    "properties": {
        "episode": {"type": "object", "required": ["summary", "body"],
                    "properties": {"summary": {"type": "string"}, "body": {"type": "string"}}},
        "upserts": {"type": "array", "items": {
            "type": "object", "required": ["scope", "kind", "summary", "body"],
            "properties": {"scope": {"type": "string"}, "kind": {"type": "string"}, "summary": {"type": "string"},
                           "body": {"type": "string"}, "entities": {"type": "array", "items": {"type": "string"}},
                           "links": {"type": "array", "items": {"type": "string"}},
                           "citations": {"type": "array", "items": {"type": "object"}},
                           "supersedes": {"type": "string"}}}},
    },
}


class ConsolidationError(Exception):
    pass


def clip_summary(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_SUMMARY else text[:MAX_SUMMARY - 3].rstrip() + "..."


def clip_body(text: str, max_words: int = 200) -> str:
    words = text.split()
    return text.strip() if len(words) <= max_words else " ".join(words[:max_words]) + " ..."


def _section(title: str, items: list[str]) -> str:
    return f"## {title}\n" + ("\n".join(f"- {i}" for i in items) if items else "(none)")


def consolidation_prompt(*, task_summary: str, outcome: str, human_messages: list[str], review_comments: list[str],
                         inbox: list[str], existing_index: list[str]) -> str:
    human, reviews = list(human_messages), list(review_comments)

    def render() -> str:
        return "\n\n".join([
            "Consolidate what was learned into memory. Reply with JSON matching the schema you were given.",
            f"## Task\n{task_summary}\n\nOutcome: {outcome}",
            _section("Notes jotted during the work", inbox),
            _section("What people said (oldest first)", human),
            _section("PR review comments", reviews),
            _section("Notes already in memory (do not duplicate; use supersedes to replace one)", existing_index),
        ])

    text = render()
    while estimate_tokens(text) > MAX_INPUT_TOKENS and (human or reviews):
        (human or reviews).pop(0)
        text = render()
    if estimate_tokens(text) > MAX_INPUT_TOKENS:
        text = text[: MAX_INPUT_TOKENS * 40 // 11 - 20] + "\n[truncated]"
    return text


def _validate(output: dict) -> None:
    episode = output.get("episode") if isinstance(output, dict) else None
    upserts = output.get("upserts") if isinstance(output, dict) else None
    if not isinstance(episode, dict) or not all(isinstance(episode.get(k), str) for k in ("summary", "body")):
        raise ConsolidationError("output needs an episode with a string summary and body")
    if not isinstance(upserts, list) or not all(
            isinstance(u, dict) and all(isinstance(u.get(k), str) for k in ("scope", "kind", "summary", "body"))
            for u in upserts):
        raise ConsolidationError("upserts must be a list of objects with scope, kind, summary and body")


def apply_consolidation(store: MemoryStore, output: dict, *, project: str, task_id: str | None, run_id: str | None,
                        sources: list[str], untrusted: bool, allowed_scopes: list[str],
                        secret_values: list[str], repo_root: Path | None = None,
                        episode_scope: str | None = None) -> list[Note]:
    _validate(output)
    trust = "external" if untrusted else "team"

    def clean(text: str) -> str:
        return scrub(text, secret_values)

    written = [store.upsert(NoteInput(
        scope=episode_scope or f"projects/{project}", kind="episode", summary=clip_summary(clean(output["episode"]["summary"])),
        body=clip_body(clean(output["episode"]["body"])), sources=list(sources), trust=trust), run_id=run_id)]

    for u in output["upserts"]:
        scope = u["scope"]
        # notes about people and the team are never inferred from a session, so they are dropped
        if scope not in allowed_scopes or scope == "team" or scope.startswith("people/"):
            continue
        note = NoteInput(scope=scope, kind=u["kind"], summary=clip_summary(clean(u["summary"])),
                         body=clip_body(clean(u["body"])), entities=list(u.get("entities") or []),
                         links=list(u.get("links") or []), sources=list(sources), trust=trust,
                         citations=_hashed(u.get("citations") or [], repo_root))
        old = store.get(u["supersedes"]) if u.get("supersedes") else None
        try:
            if old is not None and old.scope in allowed_scopes:
                written.append(store.supersede(old.id, note, run_id=run_id))
            else:
                written.append(store.upsert(note, run_id=run_id))
        except MemoryError:
            continue  # invalid or trust-violating upserts are skipped; the rest still land
    return written


def _hashed(citations: list, repo_root: Path | None) -> list[dict]:
    """The model names a path and lines; the hash is ours. Citations that can't be hashed are dropped, since an
    unhashed citation would mark its fact stale on first use."""
    out = []
    root = Path(repo_root).resolve() if repo_root else None
    for c in citations:
        if not root or not isinstance(c, dict) or "path" not in c or "lines" not in c:
            continue
        path = (root / str(c["path"])).resolve()
        try:
            start, _, end = str(c["lines"]).partition("-")
            if root not in path.parents or not path.is_file():
                continue
            out.append({"path": str(c["path"]), "lines": str(c["lines"]),
                        "sha256": CitationChecker.hash_lines(path, int(start), int(end or start))})
        except (ValueError, OSError, UnicodeDecodeError):
            continue
    return out


def remember_explicit(store: MemoryStore, *, scope: str, text: str, actor: str) -> Note:
    kind = "preference" if scope.startswith("people/") else "fact"
    return store.upsert(NoteInput(scope=scope, kind=kind, summary=clip_summary(text), body=clip_body(text),
                                  sources=[f"said-by:{actor}"], trust="team"), actor=actor)
