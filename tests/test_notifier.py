from agent import db, tasks
from agent.core.notifier import SlackNotifier
from agent.tasks import Status
from agent_fixtures import FakeSlack, add_conversation


def recorded(conn) -> list[tuple]:
    return [tuple(r) for r in conn.execute("SELECT channel, ts, conversation_id FROM slack_messages ORDER BY ts")]


def new_task(conn, title="t"):
    t = tasks.create_task(conn, project="sample", title=title, description="", kind="bug", source="slack",
                          source_ref=None, requester="ann", owner="ann", slack_channel="D-1")
    return tasks.transition(conn, t.id, Status.working, actor="x")


async def test_status_and_question_are_recorded_against_the_tasks_conversation(tmp_path):
    conn = db.connect(tmp_path / "agent.db")
    db.create_schema(conn)
    notifier = SlackNotifier(FakeSlack(), conn)
    task = new_task(conn)
    add_conversation(conn, channel="D-1", thread_ts=None, task_id=task.id, is_primary=0)
    primary = add_conversation(conn, channel="D-1", thread_ts=None, task_id=task.id)
    await notifier.task_changed(task)              # posts the status message at the top level: 101.0
    task = tasks.get_task(conn, task.id)
    await notifier.ask(task, "which env?")         # asks in the status message's thread: 102.0
    await notifier.task_changed(tasks.transition(conn, task.id, Status.needs_input, actor="x"))  # edits 101.0
    assert recorded(conn) == [("D-1", "101.0", primary), ("D-1", "102.0", primary)]


async def test_a_task_without_a_conversation_records_nothing(tmp_path):
    conn = db.connect(tmp_path / "agent.db")
    db.create_schema(conn)
    notifier = SlackNotifier(FakeSlack(), conn)
    task = new_task(conn)
    await notifier.task_changed(task)
    await notifier.ask(tasks.get_task(conn, task.id), "which env?")
    assert recorded(conn) == []


async def test_tick_already_on_the_thread_root_is_fine(tmp_path):
    conn = db.connect(tmp_path / "agent.db")
    db.create_schema(conn)
    slack = FakeSlack()

    async def already(**kw):  # a finished chat turn left the same tick on the message
        raise RuntimeError("The server responded with: {'ok': False, 'error': 'already_reacted'}")

    slack.reactions_add = already
    task = new_task(conn)
    tasks.set_fields(conn, task.id, slack_thread_ts="t1")
    task = tasks.transition(conn, task.id, Status.done, actor="x")
    await SlackNotifier(slack, conn).task_changed(task)
    assert db.get_setting(conn, f"reaction:{task.id}") == "white_check_mark"
