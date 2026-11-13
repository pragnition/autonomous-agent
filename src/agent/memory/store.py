"""memory.db: the agent's long-term memory (spec §6.15).

Notes, an immutable version row per change, an FTS5 index kept in the same transaction, the inbox of raw notes
from agents, and tombstones that stop forgotten notes from being recreated.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from agent.db import utcnow

KINDS = {"fact", "lesson", "preference", "episode", "lore"}
TRUST = {"team", "external"}
SCOPE_RE = re.compile(r"^(team|projects/[a-z0-9-]+|people/[a-z0-9_-]+|channels/[A-Za-z0-9_-]+)$")
RECALLABLE = ("active", "demoted", "stale")
MAX_SUMMARY = 120
MAX_BODY_WORDS = 200

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    body TEXT NOT NULL,
    entities TEXT NOT NULL DEFAULT '[]',
    links TEXT NOT NULL DEFAULT '[]',
    sources TEXT NOT NULL DEFAULT '[]',
    trust TEXT NOT NULL,
    citations TEXT NOT NULL DEFAULT '[]',
    valid_from TEXT,
    superseded_by TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    last_used TEXT,
    use_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS notes_scope ON notes(scope, status);
CREATE TABLE IF NOT EXISTS note_versions (
    note_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    snapshot TEXT NOT NULL,
    op TEXT NOT NULL,
    run_id TEXT,
    actor TEXT,
    at TEXT NOT NULL,
    PRIMARY KEY (note_id, version)
);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(id UNINDEXED, summary, body);
CREATE TABLE IF NOT EXISTS inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    actor TEXT,
    about TEXT,
    text TEXT NOT NULL,
    at TEXT NOT NULL,
    processed_at TEXT
);
CREATE TABLE IF NOT EXISTS tombstones (
    id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tombstones_hash ON tombstones(content_hash);
"""

_JSON_FIELDS = ("entities", "links", "sources", "citations")


class MemoryError(Exception):
    pass


class InvalidNote(MemoryError):
    pass


class Tombstoned(MemoryError):
    pass


class TrustViolation(MemoryError):
    pass


@dataclass
class NoteInput:
    scope: str
    kind: str
    summary: str
    body: str
    id: str | None = None
    entities: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    trust: str = "team"
    citations: list[dict] = field(default_factory=list)
    valid_from: str | None = None


@dataclass
class Note:
    id: str
    scope: str
    kind: str
    summary: str
    body: str
    entities: list[str]
    links: list[str]
    sources: list[str]
    trust: str
    citations: list[dict]
    valid_from: str | None
    superseded_by: str | None
    status: str
    last_used: str | None
    use_count: int
    created_at: str
    updated_at: str


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60].strip("-")
    return slug or "note"


def _content_hash(scope: str, summary: str) -> str:
    return hashlib.sha256(f"{scope}\n{' '.join(summary.lower().split())}".encode()).hexdigest()


def _fts_query(text: str) -> str:
    words = re.findall(r"\w+", text.lower())
    return " OR ".join(f'"{w}"' for w in words)


def _validate(n: NoteInput) -> None:
    problems = []
    if not SCOPE_RE.match(n.scope or ""):
        problems.append(f"bad scope {n.scope!r}")
    if n.kind not in KINDS:
        problems.append(f"bad kind {n.kind!r}")
    if n.trust not in TRUST:
        problems.append(f"bad trust {n.trust!r}")
    if not n.summary or len(n.summary) > MAX_SUMMARY:
        problems.append(f"summary must be 1-{MAX_SUMMARY} characters")
    if len(n.body.split()) > MAX_BODY_WORDS:
        problems.append(f"body must be at most {MAX_BODY_WORDS} words")
    if problems:
        raise InvalidNote("; ".join(problems))


class MemoryStore:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)

    # reading

    def _note(self, row: sqlite3.Row | None) -> Note | None:
        if row is None:
            return None
        data = dict(row)
        for f in _JSON_FIELDS:
            data[f] = json.loads(data[f])
        return Note(**data)

    def get(self, id: str) -> Note | None:
        return self._note(self.conn.execute("SELECT * FROM notes WHERE id = ?", (id,)).fetchone())

    def versions(self, id: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM note_versions WHERE note_id = ? ORDER BY version", (id,))
        return [dict(r) for r in rows]

    def search(self, query: str, scopes: list[str], limit: int = 5, *, active_only: bool = True) -> list[Note]:
        if not scopes:
            return []
        statuses = ("active",) if active_only else RECALLABLE
        scope_ph, status_ph = ",".join("?" * len(scopes)), ",".join("?" * len(statuses))
        fts = _fts_query(query)
        if fts:
            sql = (f"SELECT n.* FROM notes_fts f JOIN notes n ON n.id = f.id WHERE notes_fts MATCH ? "
                   f"AND n.scope IN ({scope_ph}) AND n.status IN ({status_ph}) ORDER BY bm25(notes_fts) LIMIT ?")
            args = [fts, *scopes, *statuses, limit]
        else:
            sql = (f"SELECT * FROM notes WHERE scope IN ({scope_ph}) AND status IN ({status_ph}) "
                   "ORDER BY updated_at DESC LIMIT ?")
            args = [*scopes, *statuses, limit]
        return [self._note(r) for r in self.conn.execute(sql, args)]

    def candidates(self, scopes: list[str]) -> list[Note]:
        if not scopes:
            return []
        ph = ",".join("?" * len(scopes))
        rows = self.conn.execute(f"SELECT * FROM notes WHERE scope IN ({ph}) AND status = 'active' "
                                 "ORDER BY updated_at DESC, use_count DESC", scopes)
        return [self._note(r) for r in rows]

    # writing

    def _snapshot(self, id: str, op: str, run_id: str | None, actor: str | None) -> None:
        note = self.get(id)
        row = self.conn.execute("SELECT COALESCE(MAX(version), 0) + 1 AS v FROM note_versions WHERE note_id = ?",
                                (id,)).fetchone()
        self.conn.execute("INSERT INTO note_versions(note_id, version, snapshot, op, run_id, actor, at) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (id, row["v"], json.dumps(asdict(note)), op, run_id, actor, utcnow()))

    def _reindex(self, id: str, summary: str | None = None, body: str | None = None) -> None:
        self.conn.execute("DELETE FROM notes_fts WHERE id = ?", (id,))
        if summary is not None:
            self.conn.execute("INSERT INTO notes_fts(id, summary, body) VALUES (?, ?, ?)", (id, summary, body))

    def _unique_id(self, base: str) -> str:
        candidate, n = base, 1
        while self.conn.execute("SELECT 1 FROM notes WHERE id = ? UNION SELECT 1 FROM tombstones WHERE id = ?",
                                (candidate, candidate)).fetchone():
            n += 1
            candidate = f"{base}-{n}"
        return candidate

    def _check_tombstones(self, n: NoteInput) -> None:
        hit = self.conn.execute("SELECT id FROM tombstones WHERE id = ? OR content_hash = ?",
                                (n.id or "", _content_hash(n.scope, n.summary))).fetchone()
        if hit:
            raise Tombstoned(f"note {hit['id']} was forgotten and must not be recreated")

    def upsert(self, note: NoteInput, *, run_id: str | None = None, actor: str | None = None) -> Note:
        _validate(note)
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            self._check_tombstones(note)
            existing = self.get(note.id) if note.id else None
            now = utcnow()
            values = (note.scope, note.kind, note.summary, note.body, json.dumps(note.entities),
                      json.dumps(note.links), json.dumps(note.sources), note.trust, json.dumps(note.citations),
                      note.valid_from or now[:10])
            if existing:
                if existing.trust == "team" and note.trust == "external":
                    raise TrustViolation(f"external text cannot change team note {existing.id}")
                self.conn.execute("UPDATE notes SET scope=?, kind=?, summary=?, body=?, entities=?, links=?, "
                                  "sources=?, trust=?, citations=?, valid_from=?, status='active', updated_at=? "
                                  "WHERE id=?", (*values, now, existing.id))
                id, op = existing.id, "modified"
            else:
                id = note.id or self._unique_id(_slug(note.summary))
                self.conn.execute("INSERT INTO notes(scope, kind, summary, body, entities, links, sources, trust, "
                                  "citations, valid_from, id, created_at, updated_at) "
                                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (*values, id, now, now))
                op = "created"
            self._reindex(id, note.summary, note.body)
            self._snapshot(id, op, run_id, actor)
        return self.get(id)

    def supersede(self, old_id: str, new: NoteInput, *, run_id: str | None = None,
                  actor: str | None = None) -> Note:
        old = self.get(old_id)
        if old is None:
            raise MemoryError(f"no note {old_id}")
        if old.trust == "team" and new.trust == "external":
            raise TrustViolation(f"external text cannot supersede team note {old_id}")
        created = self.upsert(new, run_id=run_id, actor=actor)
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute("UPDATE notes SET status='superseded', superseded_by=?, updated_at=? WHERE id=?",
                              (created.id, utcnow(), old_id))
            self._reindex(old_id)
            self._snapshot(old_id, "superseded", run_id, actor)
        return created

    def _set_status(self, id: str, status: str, op: str, actor: str | None = None) -> None:
        self.conn.execute("UPDATE notes SET status=?, updated_at=? WHERE id=?", (status, utcnow(), id))
        self._snapshot(id, op, None, actor)

    def forget(self, id: str, *, actor: str) -> None:
        note = self.get(id)
        if note is None:
            return
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            self._set_status(id, "forgotten", "forgotten", actor)
            self._reindex(id)
            self.conn.execute("INSERT OR REPLACE INTO tombstones(id, content_hash, at) VALUES (?, ?, ?)",
                              (id, _content_hash(note.scope, note.summary), utcnow()))

    def purge(self, id: str) -> None:
        note = self.get(id)
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            if note is not None:
                self.conn.execute("INSERT OR IGNORE INTO tombstones(id, content_hash, at) VALUES (?, ?, ?)",
                                  (id, _content_hash(note.scope, note.summary), utcnow()))
            self.conn.execute("DELETE FROM notes WHERE id = ?", (id,))
            self.conn.execute("DELETE FROM note_versions WHERE note_id = ?", (id,))
            self._reindex(id)

    def touch(self, ids: list[str]) -> None:
        now = utcnow()
        self.conn.executemany("UPDATE notes SET last_used=?, use_count=use_count+1 WHERE id=?",
                              [(now, i) for i in ids])

    def mark_stale(self, id: str) -> None:
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            self._set_status(id, "stale", "modified")

    def demote_unused(self, now: datetime, days: int = 30) -> int:
        cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM notes WHERE status='active' AND COALESCE(last_used, created_at) < ?", (cutoff,))]
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            for id in ids:
                self._set_status(id, "demoted", "modified")
        return len(ids)

    # inbox

    def add_inbox(self, run_id: str | None, actor: str | None, about: str | None, text: str) -> None:
        self.conn.execute("INSERT INTO inbox(run_id, actor, about, text, at) VALUES (?, ?, ?, ?, ?)",
                          (run_id, actor, about, text, utcnow()))

    def inbox_for(self, run_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM inbox WHERE run_id = ? ORDER BY id", (run_id,))]
