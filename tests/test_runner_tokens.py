import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from agent import db
from agent.core.runner_tokens import queue_names, sync_runner_tokens
from agent.core.sessions import resolve_token

NAMES = ["code-sample", "general"]


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.migrate(c)
    return c


@pytest.fixture
def path(tmp_path):
    return tmp_path / "runner" / "runner-tokens.json"


def test_queue_names():
    assert queue_names({"sample": object(), "alpha": object()}) == ["code-alpha", "code-sample", "general"]


def test_writes_a_token_per_queue(conn, path):
    tokens = sync_runner_tokens(conn, path, NAMES)
    assert json.loads(path.read_text()) == tokens and sorted(tokens) == NAMES
    for name, tok in tokens.items():
        info = resolve_token(conn, tok)
        assert (info.kind, info.sandbox) == ("sandbox", name)


def test_keeps_valid_tokens(conn, path):
    assert sync_runner_tokens(conn, path, NAMES) == sync_runner_tokens(conn, path, NAMES)


def test_replaces_revoked_or_expiring(conn, path):
    first = sync_runner_tokens(conn, path, NAMES)
    conn.execute("UPDATE tokens SET revoked = 1 WHERE sandbox = 'general'")
    second = sync_runner_tokens(conn, path, NAMES)
    assert second["code-sample"] == first["code-sample"] and second["general"] != first["general"]
    assert resolve_token(conn, second["general"]).sandbox == "general"
    later = sync_runner_tokens(conn, path, NAMES, now=datetime.now(UTC) + timedelta(days=55))
    assert all(later[n] != second[n] for n in NAMES)


def test_drops_unknown_names(conn, path):
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"code-gone": "x"}))
    sync_runner_tokens(conn, path, ["general"])
    assert list(json.loads(path.read_text())) == ["general"]


def test_file_is_private(conn, path):
    sync_runner_tokens(conn, path, NAMES)
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_unreadable_file_is_rebuilt(conn, path):
    path.parent.mkdir(parents=True)
    path.write_text("not json")
    assert sorted(sync_runner_tokens(conn, path, NAMES)) == NAMES
