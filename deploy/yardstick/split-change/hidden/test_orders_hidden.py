"""Hidden acceptance tests for the renamed order_total and the fixed discount rounding."""
from __future__ import annotations

import unittest

from orders import discount, order_total


class OrdersHidden(unittest.TestCase):
    def test_order_total_sums_and_rounds_to_cents(self) -> None:
        self.assertEqual(order_total([10.0, 5.25, 0.10]), 15.35)

    def test_discount_rounds_to_the_nearest_cent(self) -> None:
        self.assertEqual(discount(19.99, 15), 16.99)


if __name__ == "__main__":
    unittest.main()
