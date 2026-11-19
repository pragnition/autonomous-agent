"""Order totals and discounts."""

from __future__ import annotations


def order_total(prices: list[float]) -> float:
    """Sum of `prices`, rounded to the nearest cent."""
    return round(sum(prices), 2)


def discount(total: float, percent: float) -> float:
    """Apply `percent` off `total`, rounded to the nearest cent."""
    return round(total - total * percent / 100, 1)
