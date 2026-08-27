"""Regression tests for the CuaDriver.app signature + bundle resolution gate.

Issue #96328 — macOS ``computer_use`` rejected the current notarised Cua
Driver (0.22.1) and the standard ``~/.local/bin/cua-driver`` symlink.

Two independent failures were bundled into one fix:

1. ``_resolve_cua_driver_app_path`` parsed the unresolved command string.
   The standard installer places ``~/.local/bin/cua-driver`` as a symlink
   into ``/Applications/CuaDriver.app/Contents/MacOS/cua-driver``; without
   resolving symlinks first the marker scan never sees
   ``.app/Contents/MacOS/`` and Hermes reports the app is missing.

2. ``_validate_cua_driver_app_signature`` only accepted the legacy Team
   ID ``4YEC26S9KF``. cua-driver 0.22.x is now signed as
   ``Developer ID Application: Cua AI, Inc. (YCK386LBJ7)`` with the same
   bundle identifier ``com.trycua.driver``. The scalar team check
   rejected every legitimate current release.

These tests assert both axes: realpath-first resolution against a temp
``CuaDriver.app`` fixture, and an exact-match allowlist that accepts
both legacy ``4YEC26S9KF`` and current ``YCK386LBJ7`` Team IDs while
keeping the exact-bundle-id and unsigned-build opt-in behaviour intact.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_cuadriver_app(tmp_path: Path, *, name: str = "CuaDriver.app") -> Path:
    """Materialize a minimal but executable CuaDriver.app under ``tmp_path``.

    Mirrors the on-disk layout the official installer uses so
    ``_resolve_cua_driver_app_path`` (which checks for an executable
    ``Contents/MacOS/cua-driver`` inside the bundle) accepts it on every
    host. Builds the inner binary path with forward slashes so the
    function's literal ``.app/Contents/MacOS/`` marker matches on Windows
    runners too — the function is macOS-only at runtime, but the tests
    need to verify its contract portably.
    """
    app = tmp_path / name
    inner = "/Contents/MacOS/cua-driver"
    # forward-slash joined — this is a macOS bundle layout, not a host-path
    binary = Path(str(app).replace("\\", "/") + inner)
    binary.parent.mkdir(parents=True, exist_ok=True)
    # A #! sh script with the executable bit set is portable across CI hosts
    # (Linux, macOS, Windows under WSL/POSIX shims) and is what the helper
    # actually probes.
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    return app


def _codesign_proc(returncode: int = 0, stderr: str = ""):
    """Build a stand-in for ``codesign -dv`` CompletedProcess."""
    return subprocess.CompletedProcess(["codesign"], returncode, stdout="", stderr=stderr)


def _patch_codesign(monkeypatch, proc):
    """Pin ``shutil.which`` and ``subprocess.run`` to the faked codesign."""
    from tools.computer_use import cua_backend as cb

    monkeypatch.setattr(cb.shutil, "which", lambda name: "/usr/bin/codesign")
    monkeypatch.setattr(cb.subprocess, "run", lambda *a, **kw: proc)


# ---------------------------------------------------------------------------
# Symlink resolution — the user-facing reproduction from #96328
# ---------------------------------------------------------------------------


class TestResolveCuaDriverAppPath:
    def test_resolves_when_driver_path_is_a_symlink_into_app(self, tmp_path, monkeypatch):
        """The standard installer places a shim symlink at
        ``~/.local/bin/cua-driver`` pointing into the carrying app. The
        resolver must follow that symlink before scanning for the bundle
        marker (#96328)."""
        from tools.computer_use import cua_backend as cb

        app = _make_cuadriver_app(tmp_path)
        shim_dir = tmp_path / "shims"
        shim_dir.mkdir()
        shim = shim_dir / "cua-driver"
        # Use forward slashes for the symlink target — the marker scan is
        # macOS-only at runtime, so the test mimics the macOS form.
        target = str(app).replace("\\", "/") + "/Contents/MacOS/cua-driver"
        try:
            os.symlink(target, str(shim))
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this host")

        assert cb._resolve_cua_driver_app_path(str(shim)) == str(app)

    def test_resolves_when_driver_path_is_already_resolved(self, tmp_path):
        """Pre-resolved absolute paths still resolve — no double-realpath
        regression. ``os.path.realpath`` is idempotent on already-resolved
        paths, so the resolver must continue to return the bundle."""
        from tools.computer_use import cua_backend as cb

        app = _make_cuadriver_app(tmp_path)
        binary_path = str(app).replace("\\", "/") + "/Contents/MacOS/cua-driver"
        # Materialize the file so the realpath target exists for the resolver's
        # isfile check (the helper does the write; this path reuses it).
        binary = Path(binary_path)
        assert binary.is_file()

        assert cb._resolve_cua_driver_app_path(binary_path) == str(app)

    def test_returns_none_for_path_outside_any_app_bundle(self, tmp_path):
        """A driver binary OUTSIDE any .app bundle must still resolve to
        None — the post-fix resolver never falls back to /Applications or
        side-loads a DIFFERENT install than the one the driver resolved."""
        from tools.computer_use import cua_backend as cb

        bare = tmp_path / "cua-driver"
        bare.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        bare.chmod(0o755)

        assert cb._resolve_cua_driver_app_path(str(bare)) is None

    def test_returns_none_when_shim_does_not_point_into_an_app(self, tmp_path):
        """A symlink whose target is NOT inside a CuaDriver.app must not
        be coerced into a bogus bundle — the prior /Applications fallback
        did exactly that, and the post-fix resolver refuses it."""
        from tools.computer_use import cua_backend as cb

        bare = tmp_path / "cua-driver"
        bare.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        bare.chmod(0o755)
        shim_dir = tmp_path / "shims"
        shim_dir.mkdir()
        shim = shim_dir / "cua-driver"
        try:
            os.symlink(str(bare), str(shim))
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this host")

        assert cb._resolve_cua_driver_app_path(str(shim)) is None


# ---------------------------------------------------------------------------
# Team ID allowlist — accept current notarised cua-driver 0.22.x
# ---------------------------------------------------------------------------


class TestValidateCuaDriverAppSignature:
    def test_trusted_team_allowlist_contains_legacy_and_current(self):
        """Both the legacy developer Apple ID and the current Cua AI, Inc.
        team ID must be on the trusted allowlist. The allowlist is a
        frozenset (immutable) and explicit, so an accidental narrowing
        back to a scalar — the bug that landed #96328 — fails here
        before it ever ships (#96328).

        Defensive against pre-fix state: the legacy scalar ``_CUA_DRIVER_TEAM_ID``
        exists pre-fix, the new frozenset exists post-fix. The test asserts
        the SEMANTIC contract (legacy AND current both trusted, frozenset
        shape) rather than the exact attribute name, so pre-fix collection
        succeeds and the assertions cleanly demonstrate the bug.
        """
        from tools.computer_use import cua_backend as cb

        allowlist = getattr(cb, "_CUA_DRIVER_TRUSTED_TEAM_IDS", None)
        if allowlist is None:
            # Pre-fix state: the legacy scalar accepts only the developer's
            # team ID. Verify the post-fix invariant anyway — this branch
            # is what the bug looks like.
            legacy = getattr(cb, "_CUA_DRIVER_TEAM_ID", None)
            assert legacy == "4YEC26S9KF", (
                "trusted-team constant missing AND legacy scalar drifted; "
                "neither pre-fix nor post-fix invariant holds"
            )
            assert legacy != "YCK386LBJ7", (
                "pre-fix bug: legacy scalar rejects the current Cua AI team ID"
            )
            # Fall through to demonstrate the post-fix invariant would hold.
            allowlist = frozenset({legacy})

        assert isinstance(allowlist, frozenset), (
            "trusted-team allowlist must be a frozenset, not a scalar; "
            "a scalar can only hold one Team ID and silently rejects the other"
        )
        assert "4YEC26S9KF" in allowlist
        assert "YCK386LBJ7" in allowlist

    def test_bundle_id_constant_unchanged(self):
        """Bundle identifier remains an exact-match literal so a suffixed
        identifier (``com.trycua.driver.evil``) still fails closed. This is
        the load-bearing invariant the allowlist must not relax."""
        from tools.computer_use import cua_backend as cb

        assert cb._CUA_DRIVER_BUNDLE_ID == "com.trycua.driver"

    def test_accepts_current_cua_ai_team_id(self, monkeypatch):
        """cua-driver 0.22.x is notarised as
        ``Developer ID Application: Cua AI, Inc. (YCK386LBJ7)`` with the
        canonical ``com.trycua.driver`` identifier. Hermes must accept it
        — the legacy-only check rejected every current release (#96328)."""
        from tools.computer_use import cua_backend as cb

        _patch_codesign(
            monkeypatch,
            _codesign_proc(
                stderr=(
                    "Identifier=com.trycua.driver\n"
                    "TeamIdentifier=YCK386LBJ7\n"
                    "Format=app bundle with Mach-O thin\n"
                ),
            ),
        )
        cb._validate_cua_driver_app_signature("/Applications/CuaDriver.app")  # no raise

    def test_accepts_legacy_team_id(self, monkeypatch):
        """The legacy developer's Apple ID must still pass — old
        notarised builds keep working through the upgrade."""
        from tools.computer_use import cua_backend as cb

        _patch_codesign(
            monkeypatch,
            _codesign_proc(
                stderr=(
                    "Identifier=com.trycua.driver\n"
                    "TeamIdentifier=4YEC26S9KF\n"
                ),
            ),
        )
        cb._validate_cua_driver_app_signature("/Applications/CuaDriver.app")  # no raise

    def test_rejects_unknown_team_id(self, monkeypatch):
        """A signed-by-someone-else bundle is an impostor, not a variant.
        The allowlist is exact-match, so any team outside it fails closed."""
        from tools.computer_use import cua_backend as cb

        _patch_codesign(
            monkeypatch,
            _codesign_proc(
                stderr=(
                    "Identifier=com.trycua.driver\n"
                    "TeamIdentifier=EVIL000000\n"
                ),
            ),
        )
        with pytest.raises(RuntimeError, match="team"):
            cb._validate_cua_driver_app_signature("/Applications/CuaDriver.app")

    def test_rejects_suffixed_identifier_even_with_trusted_team(self, monkeypatch):
        """Bundle identifier is an exact-match literal, separate from the
        team allowlist — a suffixed identifier (``com.trycua.driver.evil``)
        on a trusted team still fails closed."""
        from tools.computer_use import cua_backend as cb

        _patch_codesign(
            monkeypatch,
            _codesign_proc(
                stderr=(
                    "Identifier=com.trycua.driver.evil\n"
                    "TeamIdentifier=YCK386LBJ7\n"
                ),
            ),
        )
        with pytest.raises(RuntimeError, match="identifier"):
            cb._validate_cua_driver_app_signature("/Applications/CuaDriver.app")

    def test_unsigned_rejected_by_default(self, monkeypatch):
        """``TeamIdentifier=not set`` (ad-hoc / unsigned dev build) must
        fail closed by default — the escape hatch is an explicit config
        opt-in, never silent."""
        from tools.computer_use import cua_backend as cb

        _patch_codesign(
            monkeypatch,
            _codesign_proc(
                stderr=(
                    "Identifier=com.trycua.driver\n"
                    "TeamIdentifier=not set\n"
                ),
            ),
        )
        monkeypatch.setattr(cb, "_computer_use_cfg", lambda: {})
        with pytest.raises(RuntimeError, match="team"):
            cb._validate_cua_driver_app_signature("/Applications/CuaDriver.app")

    def test_unsigned_allowed_by_config_opt_in(self, monkeypatch):
        """``computer_use.allow_unsigned_driver: true`` is the documented
        escape hatch for local driver development. The validator must
        respect it without weakening the exact-bundle-id requirement."""
        from tools.computer_use import cua_backend as cb

        _patch_codesign(
            monkeypatch,
            _codesign_proc(
                stderr=(
                    "Identifier=com.trycua.driver\n"
                    "TeamIdentifier=not set\n"
                ),
            ),
        )
        monkeypatch.setattr(cb, "_computer_use_cfg", lambda: {"allow_unsigned_driver": True})
        cb._validate_cua_driver_app_signature("/Applications/CuaDriver.app")  # no raise

    def test_unsigned_with_wrong_identifier_still_rejected(self, monkeypatch):
        """Opt-in for unsigned builds must NOT relax the exact-bundle-id
        requirement — a wrong identifier with TeamIdentifier=not set
        still fails closed."""
        from tools.computer_use import cua_backend as cb

        _patch_codesign(
            monkeypatch,
            _codesign_proc(
                stderr=(
                    "Identifier=com.trycua.driver.evil\n"
                    "TeamIdentifier=not set\n"
                ),
            ),
        )
        monkeypatch.setattr(cb, "_computer_use_cfg", lambda: {"allow_unsigned_driver": True})
        with pytest.raises(RuntimeError, match="identifier"):
            cb._validate_cua_driver_app_signature("/Applications/CuaDriver.app")

    def test_codesign_unavailable_raises_runtime_error(self, monkeypatch):
        """When codesign itself is missing the wrapper must refuse to
        launch the bundle, never silently skip the gate."""
        from tools.computer_use import cua_backend as cb

        monkeypatch.setattr(cb.shutil, "which", lambda name: None)
        with pytest.raises(RuntimeError, match="codesign"):
            cb._validate_cua_driver_app_signature("/Applications/CuaDriver.app")

    def test_unsigned_bundle_rejected(self, monkeypatch):
        """codesign -dv exit-non-zero means the bundle is not code-signed
        at all; raise the same kind of error regardless of team id."""
        from tools.computer_use import cua_backend as cb

        _patch_codesign(
            monkeypatch,
            _codesign_proc(
                returncode=1,
                stderr="code object is not signed at all",
            ),
        )
        with pytest.raises(RuntimeError, match="not code-signed"):
            cb._validate_cua_driver_app_signature("/Applications/CuaDriver.app")


# ---------------------------------------------------------------------------
# End-to-end: symlink shim + current notarised bundle -> launch command
# ---------------------------------------------------------------------------


class TestEmbeddedDaemonSpawnWithSymlinkAndCurrentSigning:
    def test_spawn_command_built_from_shim_with_current_team(
        self, tmp_path, monkeypatch
    ):
        """End-to-end reproduction of #96328: ``~/.local/bin/cua-driver`` is
        a symlink into the CuaDriver.app that cua-driver 0.22.x installs
        signed by Cua AI, Inc. (YCK386LBJ7). Hermes must resolve the shim,
        validate the bundle, and build the launch command — not fail
        closed with "CuaDriver.app is required" or "signed by team". """
        from tools.computer_use import cua_backend as cb

        app = _make_cuadriver_app(tmp_path)
        shim_dir = tmp_path / "shims"
        shim_dir.mkdir()
        shim = shim_dir / "cua-driver"
        target = str(app).replace("\\", "/") + "/Contents/MacOS/cua-driver"
        try:
            os.symlink(target, str(shim))
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this host")

        _patch_codesign(
            monkeypatch,
            _codesign_proc(
                stderr=(
                    "Identifier=com.trycua.driver\n"
                    "TeamIdentifier=YCK386LBJ7\n"
                ),
            ),
        )

        validated = []
        monkeypatch.setattr(
            cb,
            "_validate_cua_driver_app_signature",
            lambda app_path: validated.append(app_path),
        )

        command = cb._embedded_daemon_spawn_command(
            str(shim),
            ["serve", "--embedded", "--socket", "/tmp/private.sock"],
            platform="darwin",
        )

        assert validated == [str(app)]
        assert command == [
            "/usr/bin/open",
            "-n",
            "-g",
            "-a",
            str(app),
            "--args",
            "serve",
            "--embedded",
            "--socket",
            "/tmp/private.sock",
        ]


# ---------------------------------------------------------------------------
# Off-macOS fast path is untouched — non-darwin never goes through the gate
# ---------------------------------------------------------------------------


def test_non_macos_embedded_daemon_skips_signature_gate(monkeypatch):
    """On Linux/Windows the private daemon launches the cua-driver binary
    directly, not via ``/usr/bin/open`` + an .app bundle, so the
    signature gate must NOT fire — pinning that contract here so the
    allowlist work never accidentally widens the gate to non-darwin
    hosts (which would block every Linux/Windows install)."""
    from tools.computer_use import cua_backend as cb

    invoked = []
    monkeypatch.setattr(
        cb,
        "_validate_cua_driver_app_signature",
        lambda app_path: invoked.append(app_path),
    )

    command = cb._embedded_daemon_spawn_command(
        "/usr/local/bin/cua-driver",
        ["serve", "--embedded", "--socket", "/tmp/private.sock"],
        platform="linux",
    )

    assert command == [
        "/usr/local/bin/cua-driver",
        "serve",
        "--embedded",
        "--socket",
        "/tmp/private.sock",
    ]
    assert invoked == []