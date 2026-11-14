"""Posting text the agent wrote to Slack. The agent writes standard Markdown, but a message's `text` field is Slack's
mrkdwn, where `**bold**`, `- ` lists, `## headings` and `[text](url)` links show up as literal characters. A
`markdown` block renders standard Markdown; `text` stays as the notification fallback."""

from __future__ import annotations

import logging

from slack_sdk.errors import SlackApiError

log = logging.getLogger(__name__)
MARKDOWN_LIMIT = 12_000  # Slack's cap on all markdown blocks in one message
_FENCE = "```"


def markdown_chunks(text: str, limit: int = MARKDOWN_LIMIT) -> list[str]:
    """Split text into pieces of at most `limit` characters on line breaks. A cut inside a code fence closes the
    fence and reopens it in the next piece; a line longer than a piece is cut where it must."""
    room = limit - 2 * (len(_FENCE) + 1)  # space to close a fence here and reopen it in the next piece
    pieces = [line[i:i + room] for line in text.splitlines(keepends=True) for i in range(0, len(line), room)]
    chunks, current, in_fence = [], "", False
    for piece in pieces:
        if current and len(current) + len(piece) > room:
            chunks.append(current.rstrip("\n") + "\n" + _FENCE if in_fence else current)
            current = _FENCE + "\n" if in_fence else ""
        current += piece
        if piece.lstrip().startswith(_FENCE):
            in_fence = not in_fence
    return chunks + [current] if current else chunks


async def post_markdown(slack, *, channel: str, thread_ts: str | None, text: str) -> list[str]:
    """Post `text` as one or more messages; returns each posted message's ts, in order."""
    posted = []
    for chunk in markdown_chunks(text):
        try:
            resp = await slack.chat_postMessage(channel=channel, thread_ts=thread_ts, text=chunk,
                                                blocks=[{"type": "markdown", "text": chunk}])
        except SlackApiError as e:
            if e.response.get("error") not in ("invalid_blocks", "invalid_blocks_format"):
                raise
            log.warning("Slack rejected the markdown block (%s); posting plain text", e.response.get("error"))
            resp = await slack.chat_postMessage(channel=channel, thread_ts=thread_ts, text=chunk)
        posted.append(resp["ts"])
    return posted
