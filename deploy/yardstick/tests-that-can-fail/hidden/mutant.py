"""Track on-hand stock for known items (mutant: reserve never checks against going negative)."""

from __future__ import annotations

STOCK = {
    "widget": 10,
    "gadget": 4,
    "gizmo": 0,
}


def available(item: str) -> int:
    """Return on-hand stock for `item`. Raises KeyError if `item` isn't tracked."""
    return STOCK[item]


def reserve(item: str, qty: int) -> int:
    """Reserve `qty` units of `item` and return what's left (mutant: no bounds checking)."""
    if item not in STOCK:
        raise ValueError(f"unknown item: {item!r}")
    STOCK[item] -= qty
    return STOCK[item]
