"""Isolated-profile scope policy for the dashboard server.

Owns the server-side ``--isolated`` authority boundary. This policy used to
live inline in ``hermes_cli/web_server.py``; it is extracted here so the
security boundary has one focused owner instead of another policy island
inside the godfile (#91381 review, #78628 decomposition tracking).

``web_server.py`` re-exports these functions under the legacy underscore names
(``_is_isolated_server``, ``_isolated_scope_dir``, ``_profile_name_for_scope``,
``_scope_topology_for_isolated``, ``_clamp_profile_query_for_isolated``), so:

* ``web_server``'s own route handlers keep calling the same names,
* the extracted ``web_routers`` modules keep resolving them through
  ``web_deps.late(...)`` at call time, and
* ``monkeypatch.setattr(web_server, \"<name>\", ...)`` stays authoritative.

State stays on the FastAPI ``app`` (``app.state.isolated`` /
``app.state.isolated_scope_dir``), written once by ``web_server.start_server``
at launch. The helpers read that state lazily through the live ``web_server``
module — mirroring the ``web_deps.late`` contract — so nothing here freezes
app state or the app object at import time.

Why identity is a resolved *path*, never a profile-name string:
``get_active_profile_name()`` returns the sentinels ``\"custom\"`` / ``\"default\"``
for an unrecognized home or a derivation failure, and those strings are also
valid *real* profile names — a name-based equality check could therefore alias
an isolated server onto a sibling profile and pass ``_assert_profile_in_scope``
(#91330 review, closed by comparing canonical directories instead).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import HTTPException


def _server():
    """Return the live ``hermes_cli.web_server`` module (imported on demand).

    Cycle-safe: ``web_server`` imports this module at its own module bottom,
    long after ``app`` exists, and the functions here only touch ``web_server``
    inside a call — never at import time.
    """
    mod = sys.modules.get("hermes_cli.web_server")
    if mod is None:  # pragma: no cover - only reachable outside a live server
        import hermes_cli.web_server as mod  # type: ignore[no-redef]
    return mod


def is_isolated_server() -> bool:
    """True when this server was launched with ``--isolated``.

    Threaded from the ``--isolated`` flag through :func:`web_server.start_server`
    into ``app.state.isolated``. Defaults to False so a server that never ran
    ``start_server`` (e.g. a bare TestClient without the helper setting it) is
    treated as the unified machine dashboard, which is the safe non-restricting
    default.
    """
    return bool(getattr(_server().app.state, "isolated", False))


def isolated_scope_dir() -> Optional[Path]:
    """The canonical resolved HERMES_HOME this isolated server is scoped to.

    Captured once in :func:`web_server.start_server` at launch as an immutable
    path on ``app.state`` (#91330 review: "capture the canonical isolated launch
    authority once in start_server() and store that immutable identity/path in
    app.state"). Falls back to the process's current ``HERMES_HOME`` when
    ``start_server`` never ran (tests set ``app.state.isolated`` directly);
    ``get_hermes_home()`` is process-stable so the fallback is deterministic.
    """
    stored = getattr(_server().app.state, "isolated_scope_dir", None)
    if stored is not None:
        return Path(stored).resolve()
    if not is_isolated_server():
        return None
    from hermes_constants import get_hermes_home

    return get_hermes_home().resolve()


def profile_name_for_scope(scope_dir: Optional[Path]) -> Optional[str]:
    """Map a resolved HERMES_HOME to its profile name, or ``None``.

    Returns ``"default"`` for the root home, the profile name for a home under
    ``~/.hermes/profiles/<name>``, and ``None`` for any unrecognized/ambiguous
    home. ``None`` is the fail-closed signal: an isolated server with no
    unambiguous principal may not prove any named profile is in scope, so it is
    denied rather than inferred to be one.
    """
    if scope_dir is None:
        return None
    from hermes_cli import profiles as profiles_mod

    scope = Path(scope_dir).resolve()
    if scope == profiles_mod._get_default_hermes_home().resolve():
        return "default"
    profiles_root = profiles_mod._get_profiles_root().resolve()
    try:
        rel = scope.relative_to(profiles_root)
        if len(rel.parts) == 1 and profiles_mod._PROFILE_ID_RE.match(rel.parts[0]):
            return rel.parts[0]
    except ValueError:
        pass
    return None


def scope_topology_for_isolated(topology: Dict[str, Any]) -> Dict[str, Any]:
    """Narrow a machine-wide ``/api/status`` topology to an isolated server.

    On an isolated server the *unparameterized* ``/api/status`` (no ``?profile=``
    at all) still went through the raw machine-wide collection: ``profiles``
    enumerated every sibling profile and ``gateways`` carried each live
    gateway's host ports, and because the empty selector skipped the
    ``profile=<name>`` branch the ``profile_platforms`` map was also folded into
    ``gateway_platforms``. That leaks sibling profile/host metadata and is
    reachable before dashboard auth (``/api/status`` is in ``PUBLIC_API_PATHS``
    #76932-class). A ``?profile=`` request is clamped/403'd by
    :func:`clamp_profile_query_for_isolated` before reaching the handler; this
    helper closes the *no-query* form by scoping the collection to the pinned
    principal so an isolated server never publishes another profile's names,
    ports, or platform state.
    """
    if not is_isolated_server():
        return topology
    scope = isolated_scope_dir()
    pinned = profile_name_for_scope(scope)
    # Fail closed: an isolated server with no unambiguous principal publishes
    # nothing machine-wide rather than an empty/leaky enumeration.
    if pinned is None:
        return {
            "profiles": [],
            "gateway_mode": "none",
            "gateways": [],
            "profile_platforms": {},
        }
    return {
        "profiles": [p for p in topology.get("profiles", []) if p == pinned],
        "gateway_mode": topology.get("gateway_mode"),
        "gateways": [
            g for g in topology.get("gateways", []) if g.get("profile") == pinned
        ],
        "profile_platforms": {
            name: plats
            for name, plats in (topology.get("profile_platforms") or {}).items()
            if name == pinned
        },
    }


def clamp_profile_query_for_isolated(
    profile: Optional[str], *, allow_all: bool = False
) -> Optional[str]:
    """Clamp or reject a ``profile`` query param against the isolated scope.

    The unified machine dashboard is intentionally cross-profile and returns
    ``profile`` unchanged. An isolated server answers only for its pinned
    profile: ``profile=all`` (a dashboard-sidebar convenience meaning "all
    sessions for this backend") is clamped to the pinned profile, an explicit
    sibling selector is rejected with 403 *before any I/O*, and the pinned
    profile passes through. ``None``/``""``/``"current"`` mean the backend's
    own profile and pass through unchanged.

    Identity is the canonical resolved launch path stored on ``app.state`` at
    :func:`web_server.start_server` — never a profile-name string (the
    ``"custom"`` / ``"default"`` sentinels of ``get_active_profile_name()``
    collide with real profile names and would alias a sibling onto the
    isolation boundary).
    """
    if not is_isolated_server():
        return profile
    requested = (profile or "").strip()
    if not requested or requested.lower() == "current":
        return profile
    if allow_all and requested.lower() == "all":
        # "all sessions" on an isolated backend means "this profile's
        # sessions", not "every profile on disk".
        scope = isolated_scope_dir()
        name = profile_name_for_scope(scope)
        if name is None:
            raise HTTPException(
                status_code=403,
                detail="This dashboard is isolated but has no unambiguous scoped profile.",
            )
        return name

    from hermes_cli import profiles as profiles_mod

    try:
        requested_canon = profiles_mod.normalize_profile_name(requested)
        profiles_mod.validate_profile_name(requested_canon)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    scope = isolated_scope_dir()
    requested_dir = profiles_mod.get_profile_dir(requested_canon).resolve()
    if scope is not None and scope == requested_dir:
        return requested_canon
    shown = profile_name_for_scope(scope) if scope is not None else "(unknown)"
    raise HTTPException(
        status_code=403,
        detail=f"This dashboard is isolated to '{shown}'; refusing profile '{requested_canon}'.",
    )