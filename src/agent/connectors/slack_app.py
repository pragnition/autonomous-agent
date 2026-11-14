"""Slack wiring: a Bolt app in Socket Mode (outbound only). Messages go to the intake."""

from __future__ import annotations

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp


def build_app(bot_token: str, intake) -> AsyncApp:
    app = AsyncApp(token=bot_token)

    @app.event("app_mention")
    async def on_mention(event):
        await intake.handle_slack({**event, "type": "app_mention"})

    @app.event("message")
    async def on_message(event):
        await intake.handle_slack({**event, "type": "message"})

    return app


async def run_socket_mode(app: AsyncApp, app_token: str) -> None:
    await AsyncSocketModeHandler(app, app_token).start_async()
