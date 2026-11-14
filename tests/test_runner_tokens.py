import os
from datetime import UTC, datetime, timedelta

import pytest

from agent import db
from agent.core.runner_tokens import read_runner_token, sync_runner_token
from agent.core.sessions import mint_token, resolve_token


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.create_schema(c)
    return c


@pytest.fixture
def path(tmp_path):
    return tmp_path / "runner" / "runner-token"


def test_writes_a_runner_token(conn, path):
    token = sync_runner_token(conn, path)
    assert path.read_text() == token + "\n" and read_runner_token(path) == token
    assert resolve_token(conn, token).kind == "runner"


def test_keeps_a_valid_token(conn, path):
    assert sync_runner_token(conn, path) == sync_runner_token(conn, path)


def test_replaces_revoked_or_expiring(conn, path):
    first = sync_runner_token(conn, path)
    conn.execute("UPDATE tokens SET revoked = 1")
    second = sync_runner_token(conn, path)
    assert second != first and resolve_token(conn, second).kind == "runner"
    later = sync_runner_token(conn, path, now=datetime.now(UTC) + timedelta(days=55))
    assert later != second


def test_replaces_a_token_of_another_kind(conn, path):
    path.parent.mkdir(parents=True)
    session = mint_token(conn, "session", work_item_id="w1", ttl_s=3600)
    path.write_text(session)
    token = sync_runner_token(conn, path)
    assert token != session and resolve_token(conn, token).kind == "runner"


def test_file_is_private(conn, path):
    sync_runner_token(conn, path)
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_unknown_token_is_replaced(conn, path):
    path.parent.mkdir(parents=True)
    path.write_text("not a token")
    assert resolve_token(conn, sync_runner_token(conn, path)).kind == "runner"


def test_read_runner_token_missing_or_empty_file(tmp_path):
    path = tmp_path / "runner-token"
    assert read_runner_token(path) == ""
    path.write_text("\n")
    assert read_runner_token(path) == ""
    path.write_text("t\n")
    assert read_runner_token(path) == "t"
