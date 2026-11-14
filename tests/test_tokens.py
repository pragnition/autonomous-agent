from agent import __version__
from agent.tokens import estimate_tokens


def test_version():
    assert __version__ == "0.1.0"


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 2  # ceil(1 * 1.1)
    assert estimate_tokens("x" * 400) == 110
