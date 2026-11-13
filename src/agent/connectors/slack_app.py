"""Slack wiring: a Bolt app in Socket Mode (outbound only). Messages go to the intake; button clicks go to the
confirmation handler (spec v2 §7: only major infrastructure changes wait for an admin's click)."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

ACTION_IDS = re.compile(r"^cli_(confirm|cancel):")


def make_action_handler(on_action: Callable[[str, str], Awaitable[str]]):
    async def handler(ack, body, client):
        await ack()
        user = body["user"]["id"]
        text = await on_action(body["actions"][0]["action_id"], user)
        channel = (body.get("channel") or {}).get("id")
        if channel:
            await client.chat_postEphemeral(channel=channel, user=user, text=text)
    return handler


def build_app(bot_token: str, intake, on_action: Callable[[str, str], Awaitable[str]] | None = None) -> AsyncApp:
    """`intake` has `handle(event)` (v1 ChatService) or `handle_slack(event)` (v2 Conversations)."""
    app = AsyncApp(token=bot_token)
    handle = getattr(intake, "handle_slack", None) or intake.handle

    @app.event("app_mention")
    async def on_mention(event, ack=None):
        await handle({**event, "type": "app_mention"})

    @app.event("message")
    async def on_message(event, ack=None):
        await handle({**event, "type": "message"})

    if on_action is not None:
        app.action(ACTION_IDS)(make_action_handler(on_action))
    return app


async def run_socket_mode(app: AsyncApp, app_token: str) -> None:
    await AsyncSocketModeHandler(app, app_token).start_async()
