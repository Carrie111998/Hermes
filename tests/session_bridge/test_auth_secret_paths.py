"""The bridge auth secrets must resolve identically in every process.

The bearer token and the origin-marker HMAC key authenticate every consumer
of the one loopback service on :7484, so their on-disk locations are
machine-global.  ``get_hermes_home()`` resolves profile-scoped when
``HERMES_HOME`` (or the serve ``--config-home`` context override) names
``<root>/profiles/<name>`` — the same split-brain that forked the
characterization store on 2026-08-25.  The copies under
``profiles/main/session-bridge/`` had already diverged by 2026-08-26: the
root token was rotated on 2026-08-19 while the profile copy kept the
pre-rotation bytes, so which secret a process presented depended on its
environment.  These tests pin the repaired contract: one token file and one
marker-key file, anchored at the Hermes root, for the server and every
client alike.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from session_bridge import broker_client
from session_bridge.mcp_server import resolve_bearer_token, resolve_marker_key
from session_bridge.secret_paths import (
    auth_secret_root,
    default_marker_key_file,
    default_token_file,
)
from tests.session_bridge.test_mcp_server import _restrict_secret_file

_TOKEN = "u" * 64
_MARKER_KEY = b"m" * 32


def test_secret_paths_ignore_profile_scoped_hermes_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "hermes-root"
    profile = root / "profiles" / "main"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))

    assert auth_secret_root() == root / "session-bridge"
    assert default_token_file() == root / "session-bridge" / "token"
    assert default_marker_key_file() == root / "session-bridge" / "marker-key"


def test_secret_paths_respect_custom_deployment_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "custom-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    assert default_token_file() == home / "session-bridge" / "token"
    assert default_marker_key_file() == home / "session-bridge" / "marker-key"


def test_context_override_does_not_redirect_the_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``serve --config-home`` scoping must not fork the secret locations."""

    home = tmp_path / "hermes-root"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    token = set_hermes_home_override(tmp_path / "elsewhere")
    try:
        resolved_token = default_token_file()
        resolved_marker = default_marker_key_file()
    finally:
        reset_hermes_home_override(token)

    assert resolved_token == home / "session-bridge" / "token"
    assert resolved_marker == home / "session-bridge" / "marker-key"


def test_resolve_bearer_token_default_path_is_root_anchored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 2026-08-19 rotation trap: a profile-scoped process must still
    read the rotated root token, never a stale profile copy."""

    root = tmp_path / "hermes-root"
    profile = root / "profiles" / "main"
    (root / "session-bridge").mkdir(parents=True)
    (profile / "session-bridge").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))

    root_token = root / "session-bridge" / "token"
    root_token.write_text(_TOKEN, encoding="utf-8")
    _restrict_secret_file(root_token)
    # Deliberately unrestricted: the resolver must never open it, and if it
    # wrongly did, the content assertion below (or the restriction check)
    # fails the test without a second slow icacls round-trip.
    stale = profile / "session-bridge" / "token"
    stale.write_text("s" * 64, encoding="utf-8")

    assert resolve_bearer_token(environ={}) == _TOKEN.encode()


def test_resolve_marker_key_default_path_is_root_anchored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "hermes-root"
    profile = root / "profiles" / "main"
    (root / "session-bridge").mkdir(parents=True)
    (profile / "session-bridge").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))

    root_key = root / "session-bridge" / "marker-key"
    root_key.write_bytes(_MARKER_KEY)
    _restrict_secret_file(root_key)
    # Unrestricted on purpose — see the token test above.
    stale = profile / "session-bridge" / "marker-key"
    stale.write_bytes(b"x" * 32)

    assert resolve_marker_key() == _MARKER_KEY


def test_broker_client_reads_the_same_token_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The broker fallback must present the token the server validates."""

    root = tmp_path / "hermes-root"
    profile = root / "profiles" / "main"
    (root / "session-bridge").mkdir(parents=True)
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    (root / "session-bridge" / "token").write_text(
        "  " + _TOKEN + "\n", encoding="utf-8"
    )

    assert broker_client._read_bearer_token() == _TOKEN
