"""Startup checks for profile-scoped worker state."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home, get_default_hermes_root


def profile_scope_snapshot(profile_name: str | None = None) -> dict[str, str]:
    """Return the effective profile, home, and state DB paths.

    The values are resolved together so prompt identity and persistence cannot
    silently use different profile scopes.
    """
    home = Path(get_hermes_home()).resolve()
    root = Path(get_default_hermes_root()).resolve()
    if profile_name is None:
        try:
            relative = home.relative_to(root / "profiles")
            profile_name = relative.parts[0] if relative.parts else "default"
        except ValueError:
            profile_name = "default"
    profile_name = str(profile_name or "default").strip() or "default"
    expected_home = root / "profiles" / profile_name if profile_name != "default" else root
    return {
        "profile": profile_name,
        "home": str(home),
        "state_db": str(home / "state.db"),
        "expected_home": str(expected_home.resolve()),
    }


def assert_profile_scope(
    profile_name: str | None = None, state_db: str | Path | None = None
) -> dict[str, str]:
    """Fail fast when profile identity, home, and state DB scope disagree."""
    snapshot = profile_scope_snapshot(profile_name)
    if Path(snapshot["home"]) != Path(snapshot["expected_home"]):
        raise RuntimeError(
            "Profile scope mismatch: "
            f"profile={snapshot['profile']!r} home={snapshot['home']!r} "
            f"expected_home={snapshot['expected_home']!r} "
            f"state_db={snapshot['state_db']!r}"
        )
    if state_db is not None and Path(state_db).resolve() != Path(snapshot["state_db"]):
        raise RuntimeError(
            "Profile state DB mismatch: "
            f"profile={snapshot['profile']!r} expected={snapshot['state_db']!r} "
            f"actual={Path(state_db).resolve()!r}"
        )
    return snapshot
