"""Canonical machine-global locations of the session-bridge auth secrets.

The bearer token and the origin-marker HMAC key authenticate every consumer
of the ONE loopback service on :7484, so every process must resolve the same
files regardless of profile scoping.  ``get_hermes_home()`` resolves
profile-scoped when ``HERMES_HOME`` (or the serve ``--config-home`` context
override) names ``<root>/profiles/<name>``; that forked the on-disk secrets
the same way it forked the characterization store on 2026-08-25, and by
2026-08-26 the copies had diverged for real -- the root token was rotated on
2026-08-19 while ``profiles/main/session-bridge/token`` kept the
pre-rotation bytes.  Anchoring on :func:`get_default_hermes_root` -- the same
repair as :func:`session_bridge.characterize.characterization_store_root`
and ``events.paths`` -- keeps custom deployment homes (tests, Docker)
hermetic while mapping any profile-scoped home back to its root.

Import-safe -- no dependencies beyond ``hermes_constants``.
"""

from __future__ import annotations

from pathlib import Path

from hermes_constants import get_default_hermes_root


def auth_secret_root() -> Path:
    """Directory holding the bridge's auth secrets, anchored at the root."""

    return get_default_hermes_root() / "session-bridge"


def default_token_file() -> Path:
    """The one bearer-token file the server and every client must share."""

    return auth_secret_root() / "token"


def default_marker_key_file() -> Path:
    """The one origin-marker HMAC key file; a fork here splits validation."""

    return auth_secret_root() / "marker-key"


def default_retired_marker_key_dir() -> Path:
    """Directory of retired origin-marker keys kept for validation only.

    A rotation moves the previous ``marker-key`` bytes into this directory
    (newest-first sortable filenames) so reservations and native markers
    minted before the rotation keep verifying; signing never uses them.
    """

    return auth_secret_root() / "marker-key-retired"
