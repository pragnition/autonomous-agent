import json

from agent.runner.handoff import MAX_DIGEST_CHARS, handoff_note

SID = "11111111-1111-4111-8111-111111111111"


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def runner_msg(*blocks, sid=SID, parent=None):
    return {"type": "AssistantMessage", "content": list(blocks), "model": "m", "parent_tool_use_id": parent,
            "session_id": sid}


def cli_msg(*blocks, sidechain=False):
    return {"type": "assistant", "isSidechain": sidechain, "sessionId": SID,
            "message": {"role": "assistant", "content": list(blocks)}}


def test_the_note_carries_a_digest_of_the_runner_transcript(tmp_path):
    transcript = write_jsonl(tmp_path / "R1.jsonl", [
        {"type": "UserMessage", "content": "the prompt, never in the digest"},
        runner_msg({"text": "msg-one"}),
        runner_msg({"id": "t1", "name": "Edit", "input": {"file_path": "/s/app.py", "old_string": "x"}}),
        runner_msg({"thinking": "private", "signature": "s"}, {"text": "msg-two"}),
        runner_msg({"id": "t2", "name": "Write", "input": {"file_path": "/s/notes.md", "content": "y"}}),
        runner_msg({"text": "a subagent's"}, parent="t9"),
        runner_msg({"text": "msg-three"}),
        runner_msg({"id": "t3", "name": "Bash", "input": {"command": "uname -a"}}),
        runner_msg({"text": "msg-four"}),
        runner_msg({"text": "another session's"}, sid="22222222-2222-4222-8222-222222222222"),
    ])
    note = handoff_note(SID, tmp_path / "claude", transcript)
    assert SID in note and str(transcript) in note
    assert "[1] msg-two\n\n[2] msg-three\n\n[3] msg-four" in note  # the last three, oldest first
    assert "msg-one" not in note and "private" not in note and "subagent" not in note and "another session" not in note
    assert "never in the digest" not in note
    assert "Files it edited or wrote: /s/app.py, /s/notes.md" in note and "uname" not in note


def test_the_digest_comes_from_the_cli_session_log_on_a_later_turn(tmp_path):
    home = tmp_path / "claude"
    log = write_jsonl(home / "projects" / "-s" / f"{SID}.jsonl", [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        cli_msg({"type": "text", "text": "done with the export"},
                {"type": "tool_use", "id": "t", "name": "MultiEdit", "input": {"file_path": "/s/export.py"}}),
        cli_msg({"type": "text", "text": "a sidechain's"}, sidechain=True),
    ])
    note = handoff_note(SID, home)
    assert str(log) in note and "[1] done with the export" in note and "sidechain" not in note
    assert "Files it edited or wrote: /s/export.py" in note


def test_the_note_never_carries_the_full_record(tmp_path):
    big = [runner_msg({"text": f"message {i} " + "x" * 5000}) for i in range(50)]
    transcript = write_jsonl(tmp_path / "R1.jsonl", big)
    assert transcript.stat().st_size > 50 * MAX_DIGEST_CHARS
    note = handoff_note(SID, tmp_path / "claude", transcript)
    assert len(note) < MAX_DIGEST_CHARS + 2000  # the digest, its cap, and the note's own words
    assert "message 49 " in note and "message 46 " not in note and "x" * 2000 not in note


def test_the_note_says_not_to_load_the_record_or_resume(tmp_path):
    note = handoff_note(SID, None)
    assert "don't read or load its record whole" in note
    assert "/resume" in note and "--resume" in note and "grep or tail" in note
    assert "read_thread" in note and "git status" in note
    assert "Read it" not in note
    assert "no digest" in note  # nothing on record: says so instead of making one up
