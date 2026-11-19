"""Grant and look up roles for a small in-memory directory of users.

User ids are matched without regard to case everywhere a role is looked up or assigned.
"""

from __future__ import annotations

KNOWN_ROLES = {"viewer", "editor", "admin"}


class RoleError(Exception):
    """Raised when a role grant is refused."""


class Roles:
    def __init__(self) -> None:
        self._roles: dict[str, str] = {}

    def set_role(self, user_id: str, role: str) -> None:
        """Set a user's role directly, with no checks. Used to seed who starts out as an admin."""
        self._roles[user_id.lower()] = role.lower()

    def role_of(self, user_id: str) -> str | None:
        return self._roles.get(user_id.lower())

    def grant(self, actor_id: str, target_id: str, role: str) -> None:
        """`actor_id` grants `role` to `target_id`.

        Only an admin may grant a role, nobody may grant themselves a role, and `role` must be
        one of `KNOWN_ROLES`. Ids are compared case-insensitively everywhere, including here, so
        an admin can't dodge the self-grant check by using a different case for their own id.
        """
        if role.lower() not in KNOWN_ROLES:
            raise RoleError(f"unknown role: {role!r}")
        if self.role_of(actor_id) != "admin":
            raise RoleError(f"{actor_id!r} is not an admin")
        if actor_id.lower() == target_id.lower():
            raise RoleError("cannot grant a role to yourself")
        self._roles[target_id.lower()] = role.lower()
