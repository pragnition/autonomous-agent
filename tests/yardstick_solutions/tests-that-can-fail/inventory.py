"""Track on-hand stock for known items."""

from __future__ import annotations

STOCK = {
    "widget": 10,
    "gadget": 4,
    "gizmo": 0,
}


def available(item: str) -> int:
    """Return on-hand stock for `item`. Raises ValueError if `item` isn't tracked."""
    if item not in STOCK:
        raise ValueError(f"unknown item: {item!r}")
    return STOCK[item]


def reserve(item: str, qty: int) -> int:
    """Reserve `qty` units of `item`, lowering its stock, and return what's left.

    Raises ValueError if `item` isn't tracked, `qty` is negative, or the reservation would take
    stock below zero.
    """
    if item not in STOCK:
        raise ValueError(f"unknown item: {item!r}")
    if qty < 0:
        raise ValueError("qty must not be negative")
    if qty > STOCK[item]:
        raise ValueError(f"only {STOCK[item]} of {item!r} left")
    STOCK[item] -= qty
    return STOCK[item]
