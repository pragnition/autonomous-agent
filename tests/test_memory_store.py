from datetime import UTC, datetime, timedelta

import pytest

from agent.memory.store import InvalidNote, MemoryStore, NoteInput, Tombstoned, TrustViolation

P = "projects/chilliclaw"


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory.db")


def note(summary="Uploads over 50 MB are rejected by the gateway", scope=P, **kw):
    return NoteInput(scope=scope, kind=kw.pop("kind", "fact"), summary=summary,
                     body=kw.pop("body", "The API gateway rejects large uploads before the app sees them."), **kw)


def test_upsert_creates_version_and_is_searchable(store):
    n = store.upsert(note(), run_id="R1")
    assert n.id == "uploads-over-50-mb-are-rejected-by-the-gateway"
    assert n.status == "active" and n.trust == "team"
    assert [v["op"] for v in store.versions(n.id)] == ["created"]
    assert [x.id for x in store.search("gateway uploads", [P])] == [n.id]


def test_ids_are_unique(store):
    a = store.upsert(note())
    b = store.upsert(note())
    assert b.id == a.id + "-2"


def test_update_adds_version(store):
    n = store.upsert(note())
    n2 = store.upsert(note(id=n.id, body="Gateway limit is 50 MB; the app allows 100 MB."), actor="ann")
    assert n2.body.startswith("Gateway limit")
    assert [v["version"] for v in store.versions(n.id)] == [1, 2]
    assert store.versions(n.id)[1]["op"] == "modified"


def test_forget_hides_and_tombstones(store):
    n = store.upsert(note())
    store.forget(n.id, actor="ann")
    assert store.get(n.id).status == "forgotten"
    assert store.search("gateway", [P], active_only=False) == []
    assert store.candidates([P]) == []


def test_upsert_tombstoned_id_raises(store):
    n = store.upsert(note())
    store.forget(n.id, actor="ann")
    with pytest.raises(Tombstoned):
        store.upsert(note(id=n.id))
    with pytest.raises(Tombstoned):  # same content under a new id is blocked too
        store.upsert(note())


def test_purge_removes_versions(store):
    n = store.upsert(note())
    store.purge(n.id)
    assert store.get(n.id) is None
    assert store.versions(n.id) == []


def test_search_respects_scopes(store):
    store.upsert(note(scope="channels/C1", summary="Gateway decision in channel one"))
    assert store.search("gateway", ["channels/C2", P]) == []
    assert len(store.search("gateway", ["channels/C1"])) == 1


def test_search_tolerates_fts_syntax(store):
    store.upsert(note())
    assert store.search('gateway "AND -( *', [P])


def test_external_cannot_supersede_team(store):
    old = store.upsert(note())
    with pytest.raises(TrustViolation):
        store.supersede(old.id, note(summary="Gateway limit is 10 MB", trust="external"))
    with pytest.raises(TrustViolation):
        store.upsert(note(id=old.id, trust="external"))


def test_supersede_links_and_hides_old(store):
    old = store.upsert(note())
    new = store.supersede(old.id, note(summary="Gateway limit raised to 100 MB"), actor="ann")
    o = store.get(old.id)
    assert (o.status, o.superseded_by) == ("superseded", new.id)
    assert [c.id for c in store.candidates([P])] == [new.id]


def test_demote_unused_excludes_from_candidates_but_search_finds(store):
    n = store.upsert(note())
    assert store.demote_unused(datetime.now(UTC) + timedelta(days=31)) == 1
    assert store.candidates([P]) == []
    assert store.search("gateway", [P]) == []
    assert [x.id for x in store.search("gateway", [P], active_only=False)] == [n.id]


def test_touch_and_mark_stale(store):
    n = store.upsert(note())
    store.touch([n.id])
    assert store.get(n.id).use_count == 1
    store.mark_stale(n.id)
    assert store.get(n.id).status == "stale"


def test_inbox(store):
    store.add_inbox("R1", "builder", None, "tests need DB_URL set")
    assert [i["text"] for i in store.inbox_for("R1")] == ["tests need DB_URL set"]


def test_validation_rejects_long_summary_and_bad_scope(store):
    with pytest.raises(InvalidNote):
        store.upsert(note(summary="x" * 121))
    with pytest.raises(InvalidNote):
        store.upsert(note(scope="random/thing"))
    with pytest.raises(InvalidNote):
        store.upsert(note(body="word " * 201))
    with pytest.raises(InvalidNote):
        store.upsert(note(kind="rumour"))
