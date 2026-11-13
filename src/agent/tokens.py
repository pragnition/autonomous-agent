"""The one token estimate used for every budget: characters / 4, plus a 10% margin."""


def estimate_tokens(text: str) -> int:
    # ceil(len / 4 * 1.1) in integer arithmetic: the float version rounds 110.00000000000001 up to 111
    return -(-len(text) * 11 // 40)
