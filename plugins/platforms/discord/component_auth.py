"""Discord component authorization seam (feature I4).

Pure-logic guards for Discord interaction components:

* :class:`ComponentAuthPolicy` decides whether an actor may drive a
  component (allowlist, owner, or privileged roles) and whether the
  component's originating view is stale.
* :func:`reused_custom_id` detects reuse of a custom id across the
  session, which is a classic replay vector for button components.

All checks fail closed: anything that is not explicitly allowed is denied.
"""

from __future__ import annotations

import time
from typing import Iterable, Optional, Set, Union

UserId = Union[str, int]
RoleId = Union[str, int]


class ComponentAuthError(ValueError):
    """Raised when the component authorization seam receives invalid input."""


def _canonical(ids: Optional[Iterable[Union[str, int]]]) -> Set[str]:
    """Normalize ids to strings so str/int variants compare equal."""
    return {str(i) for i in (ids or ())}


class ComponentAuthPolicy:
    """Authorization policy for Discord component interactions.

    An actor is authorized when any of these hold:

    1. their user id is in the configured ``allowlist``,
    2. they are the ``owner_id`` of the component interaction, or
    3. any role they hold (passed via ``allowed_role_ids``) is in the
       policy's ``admin_role_ids``.

    Everything else is denied (fail closed).
    """

    def __init__(
        self,
        allowlist: Optional[Set[UserId]] = None,
        admin_role_ids: Optional[Set[RoleId]] = None,
    ) -> None:
        self.allowlist: Set[str] = _canonical(allowlist)
        self.admin_role_ids: Set[str] = _canonical(admin_role_ids)

    def authorize(
        self,
        actor_user_id: UserId,
        *,
        owner_id: Optional[UserId] = None,
        allowed_role_ids: Optional[Set[RoleId]] = None,
    ) -> bool:
        """Return True if ``actor_user_id`` may act on the component.

        ``allowed_role_ids`` carries the roles the actor actually holds;
        authorization requires one of them to be a configured admin role.
        """
        actor = str(actor_user_id)
        if actor in self.allowlist:
            return True
        if owner_id is not None and actor == str(owner_id):
            return True
        actor_roles = _canonical(allowed_role_ids)
        if actor_roles & self.admin_role_ids:
            return True
        return False

    def is_expired(self, created_at_ts: float, *, max_age_seconds: float = 900) -> bool:
        """Return True when the component view is stale.

        A view created at ``created_at_ts`` is expired once more than
        ``max_age_seconds`` have elapsed. ``created_at_ts`` must be a
        non-negative number, otherwise :class:`ComponentAuthError` is raised.
        """
        if isinstance(created_at_ts, bool) or not isinstance(created_at_ts, (int, float)):
            raise ComponentAuthError(
                f"created_at_ts must be a non-negative number, got {created_at_ts!r}"
            )
        if created_at_ts < 0:
            raise ComponentAuthError(
                f"created_at_ts must be non-negative, got {created_at_ts!r}"
            )
        return time.time() - created_at_ts > max_age_seconds


def reused_custom_id(custom_id: str, seen_ids: Set[str]) -> bool:
    """Return True if ``custom_id`` was already seen (reuse/replay).

    New ids are recorded in ``seen_ids`` and return False. An empty
    ``custom_id`` raises :class:`ComponentAuthError`.
    """
    if not isinstance(custom_id, str) or not custom_id:
        raise ComponentAuthError("custom_id must be a non-empty string")
    if custom_id in seen_ids:
        return True
    seen_ids.add(custom_id)
    return False
