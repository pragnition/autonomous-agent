"""Hidden regression tests for the self-grant bug in roles.py."""
from __future__ import annotations

import unittest

from roles import RoleError, Roles


class SelfGrantHidden(unittest.TestCase):
    def setUp(self) -> None:
        self.roles = Roles()
        self.roles.set_role("admin", "admin")

    def test_upper_case_self_grant_is_refused(self) -> None:
        with self.assertRaises(RoleError):
            self.roles.grant("Admin", "admin", "editor")
        self.assertEqual(self.roles.role_of("admin"), "admin")

    def test_normal_grant_still_works(self) -> None:
        self.roles.grant("admin", "newuser", "editor")
        self.assertEqual(self.roles.role_of("newuser"), "editor")

    def test_unknown_role_is_refused(self) -> None:
        with self.assertRaises(RoleError):
            self.roles.grant("admin", "newuser", "superuser")


if __name__ == "__main__":
    unittest.main()
