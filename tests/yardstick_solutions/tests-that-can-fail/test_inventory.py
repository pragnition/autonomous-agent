"""Tests for inventory.reserve."""
from __future__ import annotations

import unittest

from inventory import STOCK, available, reserve


class ReserveTests(unittest.TestCase):
    def setUp(self) -> None:
        STOCK["widget"] = 10
        STOCK["gadget"] = 4
        STOCK["gizmo"] = 0

    def test_reserve_lowers_stock(self) -> None:
        self.assertEqual(reserve("widget", 3), 7)
        self.assertEqual(available("widget"), 7)

    def test_reserve_cannot_go_below_zero(self) -> None:
        with self.assertRaises(ValueError):
            reserve("gadget", 5)
        self.assertEqual(available("gadget"), 4)

    def test_reserve_rejects_unknown_item(self) -> None:
        with self.assertRaises(ValueError):
            reserve("unknown-item", 1)

    def test_reserve_rejects_negative_qty(self) -> None:
        with self.assertRaises(ValueError):
            reserve("widget", -1)


if __name__ == "__main__":
    unittest.main()
