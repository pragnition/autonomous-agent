"""Base tests for orders.py."""
from __future__ import annotations

import unittest

from orders import calc_total, discount


class OrdersTests(unittest.TestCase):
    def test_calc_total_sums_and_rounds_to_cents(self) -> None:
        self.assertEqual(calc_total([10.0, 5.25, 0.10]), 15.35)

    def test_discount_applies_percent_off(self) -> None:
        self.assertEqual(discount(100.0, 20), 80.0)

    def test_discount_zero_percent_is_unchanged(self) -> None:
        self.assertEqual(discount(50.0, 0), 50.0)


if __name__ == "__main__":
    unittest.main()
