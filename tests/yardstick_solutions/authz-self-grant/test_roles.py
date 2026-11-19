"""Regression test for the self-grant bug: an admin must not be able to grant themselves a role,
even by spelling their own id with different letter case."""
from __future__ import annotations

import unittest

from roles import RoleError, Roles


class SelfGrantRegression(unittest.TestCase):
    def setUp(self) -> None:
        self.roles = Roles()
        self.roles.set_role("admin", "admin")

    def test_admin_cannot_self_grant_even_with_different_case(self) -> None:
        with self.assertRaises(RoleError):
            self.roles.grant("Admin", "admin", "editor")

    def test_grant_to_someone_else_still_works(self) -> None:
        self.roles.grant("admin", "newuser", "viewer")
        self.assertEqual(self.roles.role_of("newuser"), "viewer")


if __name__ == "__main__":
    unittest.main()
