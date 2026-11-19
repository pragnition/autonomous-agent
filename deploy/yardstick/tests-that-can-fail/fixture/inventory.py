"""Track on-hand stock for known items."""

from __future__ import annotations

STOCK = {
    "widget": 10,
    "gadget": 4,
    "gizmo": 0,
}


def available(item: str) -> int:
    """Return on-hand stock for `item`. Raises KeyError if `item` isn't tracked."""
    return STOCK[item]
