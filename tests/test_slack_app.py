from agent.connectors.slack_app import build_app


def test_the_bots_own_messages_reach_the_intake():
    """Bolt drops the bot's own events by default; core needs them to note the threads the agent starts itself."""
    names = {type(m).__name__ for m in build_app("xoxb-test", None)._async_middleware_list}
    assert not any("IgnoringSelfEvents" in n for n in names)
