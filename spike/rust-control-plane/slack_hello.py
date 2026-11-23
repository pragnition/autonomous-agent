#!/usr/bin/env python3
"""Spike: open a second Socket Mode connection with the existing app token, wait for `hello`, close.

Reads SLACK_APP_TOKEN from the environment and never prints it or the socket URL. It acknowledges nothing, so any
event Slack routes here in these seconds is retried to core's own connection.
"""
import asyncio
import json
import os
import urllib.request

import aiohttp


async def main() -> None:
    req = urllib.request.Request("https://slack.com/api/apps.connections.open", method="POST", data=b"",
                                 headers={"Authorization": f"Bearer {os.environ['SLACK_APP_TOKEN']}"})
    opened = json.load(urllib.request.urlopen(req, timeout=10))
    if not opened.get("ok"):
        print("apps.connections.open failed:", opened.get("error"))
        return
    async with aiohttp.ClientSession() as session, session.ws_connect(opened["url"], timeout=10) as ws:
        hello = json.loads((await asyncio.wait_for(ws.receive(), timeout=5)).data)
        print("type:", hello.get("type"), "num_connections:", hello.get("num_connections"))


asyncio.run(main())
