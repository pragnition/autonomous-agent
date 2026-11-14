import pytest
from slack_sdk.errors import SlackApiError

from agent.core.slack_markdown import MARKDOWN_LIMIT, markdown_chunks, post_markdown
from agent_fixtures import FakeSlack


async def test_posts_text_as_a_markdown_block_with_the_text_as_fallback():
    slack = FakeSlack()
    await post_markdown(slack, channel="C1", thread_ts="1.0", text="**GitHub:** ok\n- one\n- two")
    assert slack.posts == [{"channel": "C1", "thread_ts": "1.0", "text": "**GitHub:** ok\n- one\n- two",
                            "blocks": [{"type": "markdown", "text": "**GitHub:** ok\n- one\n- two"}]}]


async def test_falls_back_to_plain_text_when_slack_rejects_the_block():
    slack = FakeSlack()
    original = slack.chat_postMessage

    async def picky(**kw):
        if "blocks" in kw:
            raise SlackApiError("invalid_blocks", {"ok": False, "error": "invalid_blocks"})
        return await original(**kw)

    slack.chat_postMessage = picky
    assert await post_markdown(slack, channel="C1", thread_ts="1.0", text="**hi**") == ["101.0"]
    assert slack.posts == [{"channel": "C1", "thread_ts": "1.0", "text": "**hi**"}]


async def test_other_slack_errors_still_raise():
    slack = FakeSlack()

    async def down(**kw):
        raise SlackApiError("channel_not_found", {"ok": False, "error": "channel_not_found"})

    slack.chat_postMessage = down
    with pytest.raises(SlackApiError):
        await post_markdown(slack, channel="C1", thread_ts="1.0", text="hi")


async def test_long_text_goes_out_as_several_messages():
    slack = FakeSlack()
    text = "\n".join(f"line {i} " + "x" * 90 for i in range(400))
    await post_markdown(slack, channel="C1", thread_ts="1.0", text=text)
    assert len(slack.posts) > 1
    assert all(len(p["blocks"][0]["text"]) <= MARKDOWN_LIMIT for p in slack.posts)
    assert "".join(p["text"] for p in slack.posts) == text


async def test_post_markdown_returns_ts_list():
    slack = FakeSlack()
    sent = await post_markdown(slack, channel="C1", thread_ts=None, text="y" * 13_000)
    assert len(slack.posts) == 2 and sent == ["101.0", "102.0"]


def test_short_text_is_one_chunk():
    assert markdown_chunks("hello") == ["hello"]


def test_a_cut_inside_a_code_fence_closes_and_reopens_it():
    text = "intro\n```\n" + "".join(f"code {i}\n" for i in range(50)) + "```\nafter"
    chunks = markdown_chunks(text, limit=120)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 120
        assert sum(line.startswith("```") for line in chunk.splitlines()) % 2 == 0


def test_a_single_huge_line_is_hard_cut():
    chunks = markdown_chunks("y" * 300, limit=100)
    assert all(len(c) <= 100 for c in chunks) and "".join(chunks) == "y" * 300
