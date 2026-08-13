"""Regression tests for TOCTOU-safe credential file writers in ``hermes_cli.auth``.

Background
==========
The three writers below used to create a temp file via ``Path.write_text`` /
``Path.open('w')`` and only ``chmod``'d it to ``0o600`` afterward. Between
create and chmod the file existed at the process umask (typically ``0o644``),
briefly exposing OAuth tokens to other local users on multi-user hosts. The
fix switches them to ``os.open(O_EXCL, mode=0o600)`` + ``os.fdopen`` +
``fsync`` so the file is atomic at ``0o600`` on creation. Mirrors the fixes
shipped for ``agent/google_oauth.py`` (#19673) and ``tools/mcp_oauth.py``
(#21148).

These tests stay green only while the token file and its parent directory
end up at ``0o600`` / ``0o700`` after every write. POSIX-only — the mode-bit
enforcement does not exist on Windows.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from unittest.mock import ANY, patch

import pytest


pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="POSIX mode bits not enforced on Windows",
)


# ---------------------------------------------------------------------------
# _save_auth_store  (~/.hermes/auth.json — every native OAuth provider)
# ---------------------------------------------------------------------------


def test_save_auth_store_writes_0o600_with_0o700_parent(tmp_path, monkeypatch):
    """``_save_auth_store`` must land ``auth.json`` at 0o600 and parent at 0o700."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    old_umask = os.umask(0o022)  # make the race observable if it regresses
    try:
        from hermes_cli import auth as auth_mod

        auth_store = {
            "version": auth_mod.AUTH_STORE_VERSION,
            "providers": {"openai-codex": {"tokens": {"access_token": "secret-x"}}},
            "active_provider": "openai-codex",
        }
        auth_path = auth_mod._save_auth_store(auth_store)
    finally:
        os.umask(old_umask)

    mode = stat.S_IMODE(auth_path.stat().st_mode)
    parent_mode = stat.S_IMODE(auth_path.parent.stat().st_mode)

    assert mode == 0o600, (
        f"auth.json mode 0o{mode:o} != 0o600 — TOCTOU race regressed"
    )
    assert parent_mode == 0o700, (
        f"auth.json parent dir mode 0o{parent_mode:o} != 0o700 — siblings can traverse"
    )

    # Content survived the rewrite
    data = json.loads(auth_path.read_text())
    assert data["providers"]["openai-codex"]["tokens"]["access_token"] == "secret-x"


def test_save_auth_store_preserves_existing_owner(tmp_path, monkeypatch):
    """Atomic replacement must preserve UID/GID before exposing the new inode."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    auth_path = tmp_path / "auth.json"
    auth_path.write_text('{"version": 1, "providers": {}}', encoding="utf-8")
    existing_stat = auth_path.stat()
    owner = os.stat_result(
        (
            existing_stat.st_mode,
            existing_stat.st_ino,
            existing_stat.st_dev,
            existing_stat.st_nlink,
            1234,
            5678,
            existing_stat.st_size,
            existing_stat.st_atime,
            existing_stat.st_mtime,
            existing_stat.st_ctime,
        )
    )
    events = []

    from hermes_cli import auth as auth_mod

    real_atomic_replace = auth_mod.atomic_replace

    def ordered_fchown(fd, uid, gid):
        events.append(("fchown", uid, gid))

    def ordered_replace(source, target):
        events.append(("replace", source, target))
        return real_atomic_replace(source, target)

    real_stat = type(auth_path).stat

    def owner_stat(path, *args, **kwargs):
        if path == auth_path:
            return owner
        return real_stat(path, *args, **kwargs)

    with (
        patch.object(type(auth_path), "stat", autospec=True, side_effect=owner_stat),
        patch.object(os, "fchown", side_effect=ordered_fchown),
        patch.object(auth_mod, "atomic_replace", side_effect=ordered_replace),
    ):
        auth_mod._save_auth_store(
            {"version": auth_mod.AUTH_STORE_VERSION, "providers": {}}
        )

    assert events[0] == ("fchown", owner.st_uid, owner.st_gid)
    assert events[1][0] == "replace"


def test_save_new_auth_store_inherits_parent_owner(tmp_path, monkeypatch):
    """A first privileged write must use the HERMES_HOME owner's UID/GID."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    parent_stat = tmp_path.stat()
    owner = os.stat_result(
        (
            parent_stat.st_mode,
            parent_stat.st_ino,
            parent_stat.st_dev,
            parent_stat.st_nlink,
            1234,
            5678,
            parent_stat.st_size,
            parent_stat.st_atime,
            parent_stat.st_mtime,
            parent_stat.st_ctime,
        )
    )

    from hermes_cli import auth as auth_mod

    real_stat = type(tmp_path).stat

    def owner_stat(path, *args, **kwargs):
        if path == tmp_path:
            return owner
        return real_stat(path, *args, **kwargs)

    with (
        patch.object(type(tmp_path), "stat", autospec=True, side_effect=owner_stat),
        patch.object(os, "fchown") as fchown,
    ):
        auth_mod._save_auth_store(
            {"version": auth_mod.AUTH_STORE_VERSION, "providers": {}}
        )

    fchown.assert_called_once_with(ANY, owner.st_uid, owner.st_gid)


def test_save_auth_store_cleans_up_when_fchown_fails(tmp_path, monkeypatch):
    """A failed ownership transfer must leave no replacement or temp file."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    auth_path = tmp_path / "auth.json"
    original = '{"version": 1, "providers": {"original": {}}}'
    auth_path.write_text(original, encoding="utf-8")

    from hermes_cli import auth as auth_mod

    with patch.object(os, "fchown", side_effect=PermissionError("denied")):
        with pytest.raises(PermissionError, match="denied"):
            auth_mod._save_auth_store(
                {"version": auth_mod.AUTH_STORE_VERSION, "providers": {}}
            )

    assert auth_path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob("auth.json.tmp.*")) == []


# ---------------------------------------------------------------------------
# _save_qwen_cli_tokens  (Qwen CLI OAuth tokens)
# ---------------------------------------------------------------------------


def test_save_qwen_cli_tokens_writes_0o600_with_0o700_parent(tmp_path, monkeypatch):
    """``_save_qwen_cli_tokens`` must land the token file at 0o600 and parent at 0o700."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # The Qwen CLI auth path lives under $HOME/.qwen by default — isolate it.
    monkeypatch.setenv("HOME", str(tmp_path))
    old_umask = os.umask(0o022)
    try:
        from hermes_cli import auth as auth_mod

        tokens = {
            "access_token": "qwen-secret",
            "refresh_token": "qwen-refresh",
            "token_type": "Bearer",
            "expiry_date": 123,
        }
        auth_path = auth_mod._save_qwen_cli_tokens(tokens)
    finally:
        os.umask(old_umask)

    mode = stat.S_IMODE(auth_path.stat().st_mode)
    parent_mode = stat.S_IMODE(auth_path.parent.stat().st_mode)

    assert mode == 0o600, (
        f"Qwen token file mode 0o{mode:o} != 0o600 — TOCTOU race regressed"
    )
    assert parent_mode == 0o700, (
        f"Qwen token parent dir mode 0o{parent_mode:o} != 0o700"
    )

    data = json.loads(auth_path.read_text())
    assert data["access_token"] == "qwen-secret"


# ---------------------------------------------------------------------------
# Nous shared-credential store write (inside _write_shared_nous_state)
# ---------------------------------------------------------------------------


def test_shared_nous_store_writes_0o600_with_0o700_parent(tmp_path, monkeypatch):
    """The Nous shared-credential store must land at 0o600 / parent 0o700."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # _nous_shared_store_path() refuses to touch the real shared store during
    # pytest runs; redirect it into tmp_path explicitly. Use a distinct
    # subdirectory name (``shared_override``) so the guard's "real user
    # home" reference — which currently tracks HERMES_HOME via
    # get_default_hermes_root() — can't collide with our override and
    # falsely claim we're writing to the real user's shared store.
    monkeypatch.setenv("HERMES_SHARED_AUTH_DIR", str(tmp_path / "shared_override"))
    old_umask = os.umask(0o022)
    try:
        from hermes_cli import auth as auth_mod

        state = {
            "access_token": "nous-access-xxx",
            "refresh_token": "nous-refresh-xxx",
            "token_type": "Bearer",
            "scope": "openid profile",
            "client_id": "test-client",
            "obtained_at": "2026-01-01T00:00:00Z",
            "expires_at": "2026-01-01T01:00:00Z",
        }
        auth_mod._write_shared_nous_state(state)
        path = auth_mod._nous_shared_store_path()
    finally:
        os.umask(old_umask)

    assert path.exists(), "shared Nous store was not written"
    mode = stat.S_IMODE(path.stat().st_mode)
    parent_mode = stat.S_IMODE(path.parent.stat().st_mode)

    assert mode == 0o600, (
        f"Nous shared store mode 0o{mode:o} != 0o600 — TOCTOU race regressed"
    )
    assert parent_mode == 0o700, (
        f"Nous shared store parent dir mode 0o{parent_mode:o} != 0o700"
    )

    data = json.loads(path.read_text())
    assert data["refresh_token"] == "nous-refresh-xxx"


# ---------------------------------------------------------------------------
# Atomicity: verify ``os.open`` is called with an explicit 0o600 mode.
# ---------------------------------------------------------------------------


def test_save_auth_store_uses_os_open_with_0o600_mode(tmp_path, monkeypatch):
    """Regression: the writer must call ``os.open`` with an explicit restricted
    mode so the file is created at 0o600 atomically — closing the TOCTOU
    window the previous ``Path.open('w')`` left open (fd inherited process
    umask and was briefly 0o644 before post-write chmod)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    observed_opens: list[tuple[str, int, int]] = []
    real_os_open = os.open

    def spying_os_open(path, flags, mode=0o777, *args, **kwargs):
        observed_opens.append((str(path), flags, mode))
        return real_os_open(path, flags, mode, *args, **kwargs)

    with patch.object(os, "open", spying_os_open):
        from hermes_cli import auth as auth_mod

        auth_mod._save_auth_store(
            {"version": auth_mod.AUTH_STORE_VERSION, "providers": {}}
        )

    auth_tmp_opens = [
        (p, fl, m) for (p, fl, m) in observed_opens if "auth.json.tmp" in p
    ]
    assert auth_tmp_opens, (
        f"os.open was never called for the auth.json temp file; "
        f"observed={observed_opens!r}"
    )
    for path, flags, mode in auth_tmp_opens:
        assert flags & os.O_CREAT, f"auth.json temp open missing O_CREAT: path={path}"
        assert flags & os.O_EXCL, (
            f"auth.json temp open missing O_EXCL — TOCTOU-safe pattern regressed: "
            f"path={path}, flags={flags}"
        )
        # Must be exactly S_IRUSR | S_IWUSR (0o600) — no group/other bits.
        expected = stat.S_IRUSR | stat.S_IWUSR
        assert mode == expected, (
            f"auth.json temp open mode 0o{mode:o} != 0o{expected:o} — "
            f"umask would apply and potentially expose tokens"
        )
