import json

import pytest

from agent.memory.consolidate import (ConsolidationError, apply_consolidation, consolidation_prompt,
                                      remember_explicit)
from agent.memory.store import MemoryStore, NoteInput
from agent.memory.tools import BUDGET_SPENT, MemoryToolbox
from agent.tokens import estimate_tokens

P = "projects/p"


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory.db")


def add(store, summary, scope=P, body="Some body text."):
    return store.upsert(NoteInput(scope=scope, kind="fact", summary=summary, body=body))


def box(store, budget=1000, scopes=(P, "team")):
    return MemoryToolbox(store, list(scopes), budget, run_id="R1", actor="builder", secret_values=["s3cr3t-value-123"])


def test_recall_only_in_scope(store):
    mine = add(store, "Gateway caps uploads")
    add(store, "Gateway in another project", scope="projects/q")
    hits = box(store).recall("gateway", limit=50)
    assert [h["id"] for h in hits] == [mine.id]
    assert set(hits[0]) == {"id", "summary", "scope", "updated_at"}


def test_read_memory_out_of_scope_not_found(store):
    other = add(store, "Secret plan", scope="channels/C9")
    assert box(store).read_memory(other.id) == "not found"
    assert box(store).read_memory("missing") == "not found"


def test_read_memory_in_scope(store):
    n = add(store, "Gateway caps uploads", body="Limit is 50 MB.")
    assert "Limit is 50 MB." in box(store).read_memory(n.id)


def test_budget_exhaustion_message(store):
    n = add(store, "Gateway caps uploads", body="word " * 150)
    b = box(store, budget=60)
    assert b.read_memory(n.id) == BUDGET_SPENT
    assert BUDGET_SPENT == "memory budget for this run is spent"


def test_tokens_used_accumulates(store):
    add(store, "Gateway caps uploads")
    b = box(store)
    b.recall("gateway")
    assert b.tokens_used > 0


def test_remember_goes_to_inbox_scrubbed(store):
    assert box(store).remember("DB_URL must be set; password s3cr3t-value-123", about="project") == "noted"
    [entry] = store.inbox_for("R1")
    assert entry["actor"] == "builder" and entry["about"] == "project"
    assert "s3cr3t" not in entry["text"]


OUTPUT = {
    "episode": {"summary": "Added copy button to citations", "body": "Built and opened PR #12."},
    "upserts": [
        {"scope": P, "kind": "fact", "summary": "Frontend tests need NODE_ENV=test", "body": "Otherwise they hang."},
        {"scope": "people/alice", "kind": "preference", "summary": "Alice likes small PRs", "body": "Inferred."},
        {"scope": "team", "kind": "lore", "summary": "Fridays are deploy-free", "body": "Inferred."},
    ],
}


def apply(store, output=OUTPUT, untrusted=False, secrets=()):
    return apply_consolidation(store, output, project="p", task_id="T-0001", run_id="R2", sources=["T-0001"],
                               untrusted=untrusted, allowed_scopes=[P, "team", "people/alice"],
                               secret_values=list(secrets))


def test_apply_writes_episode_and_facts(store):
    notes = apply(store)
    assert [(n.kind, n.scope) for n in notes] == [("episode", P), ("fact", P)]
    assert notes[0].sources == ["T-0001"]


def test_apply_drops_inferred_people_and_team_notes(store):
    apply(store)
    assert store.candidates(["people/alice", "team"]) == []


def test_apply_untrusted_forces_external(store):
    assert {n.trust for n in apply(store, untrusted=True)} == {"external"}


def test_apply_scrubs_secrets(store):
    out = json.loads(json.dumps(OUTPUT))
    out["upserts"][0]["body"] = "Use token ghp_" + "x" * 30 + " and pw hunter2-hunter2"
    notes = apply(store, out, secrets=["hunter2-hunter2"])
    assert "ghp_" not in notes[1].body and "hunter2" not in notes[1].body


def test_apply_follows_supersedes(store):
    old = add(store, "Frontend tests need NODE_ENV=dev")
    out = {"episode": OUTPUT["episode"], "upserts": [dict(OUTPUT["upserts"][0], supersedes=old.id)]}
    apply(store, out)
    assert store.get(old.id).status == "superseded"


def test_apply_invalid_output_raises(store):
    with pytest.raises(ConsolidationError):
        apply(store, {"upserts": []})
    with pytest.raises(ConsolidationError):
        apply(store, {"episode": {"summary": "x"}, "upserts": "nope"})


def test_consolidation_prompt_capped():
    msgs = [f"message {i} " + "blah " * 400 for i in range(40)]
    prompt = consolidation_prompt(task_summary="did it", outcome="pr", human_messages=msgs, review_comments=[],
                                  inbox=["note"], existing_index=["- [a] fact"])
    assert estimate_tokens(prompt) <= 8000
    assert "message 39 " in prompt and "message 0 " not in prompt


def test_remember_explicit_people_preference(store):
    n = remember_explicit(store, scope="people/alice", text="I prefer small PRs with one change each",
                          actor="alice")
    assert (n.kind, n.trust, n.scope) == ("preference", "team", "people/alice")
    assert n.summary == "I prefer small PRs with one change each"
    assert remember_explicit(store, scope=P, text="Deploys happen on Tuesdays", actor="alice").kind == "fact"


def test_apply_hashes_citations_against_repo(store, tmp_path):
    from agent.memory.citations import CitationChecker
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("one\ntwo\nthree\n")
    out = {"episode": OUTPUT["episode"], "upserts": [dict(OUTPUT["upserts"][0], citations=[
        {"path": "a.py", "lines": "1-2"}, {"path": "missing.py", "lines": "1-1"}, {"lines": "1-1"}])]}
    notes = apply_consolidation(store, out, project="p", task_id="T-1", run_id="R", sources=[], untrusted=False,
                                allowed_scopes=[P], secret_values=[], repo_root=repo)
    assert notes[1].citations == [{"path": "a.py", "lines": "1-2",
                                   "sha256": CitationChecker.hash_lines(repo / "a.py", 1, 2)}]
    no_repo = apply_consolidation(store, {"episode": {"summary": "e2", "body": "b"}, "upserts": [dict(
        OUTPUT["upserts"][0], summary="Other fact", citations=[{"path": "a.py", "lines": "1-2"}])]},
        project="p", task_id="T-2", run_id="R", sources=[], untrusted=False, allowed_scopes=[P], secret_values=[])
    assert no_repo[1].citations == []
