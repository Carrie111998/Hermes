"""Regression guard: CuaDriver.app bundle resolution must follow symlinks.

Bug (2026-08-26, Sam's Mac): `computer_use` failed every call with
"CuaDriver.app is required for private computer-use sessions on macOS" while
`hermes computer-use doctor` reported EVERY check green and the cua-driver
daemon was healthy and directly drivable via `cua-driver call ...`.

Root cause: `_resolve_cua_driver_app_path()` matched the literal substring
".app/Contents/MacOS/" against the RAW resolved driver path. The canonical
install puts a symlink on PATH:

    ~/.local/bin/cua-driver -> /Applications/CuaDriver.app/Contents/MacOS/cua-driver

`shutil.which()` returns the SYMLINK path, which contains no bundle marker, so
the match returned -1 and the caller failed closed on a perfectly valid
install. `doctor` never calls this function (it spawns the binary directly and
lets the OS follow the symlink), which is exactly why doctor stayed green while
the MCP tool was hard-down — a green diagnostic on a broken feature.

These tests pin BOTH halves of the contract: symlinks must resolve, AND the
no-fallback security guarantee must survive (a driver that does not live in a
bundle must still return None rather than reaching for /Applications).
"""

import os

import pytest

from tools.computer_use import cua_backend


def _make_fake_bundle(root, name="CuaDriver.app"):
    """Create a minimal executable CuaDriver.app bundle under *root*."""
    app = root / name
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    binary = macos / "cua-driver"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    return app, binary


class TestResolveCuaDriverAppPathSymlinks:
    def test_resolves_bundle_through_a_symlink_on_path(self, tmp_path):
        """The exact shape of the real bug: PATH entry is a symlink."""
        app, binary = _make_fake_bundle(tmp_path)

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        link = bin_dir / "cua-driver"
        link.symlink_to(binary)

        # Precondition: the raw symlink path really does lack the marker, so
        # this test is exercising the bug and not a tautology.
        assert ".app/Contents/MacOS/" not in str(link)

        assert cua_backend._resolve_cua_driver_app_path(str(link)) == str(app)

    def test_resolves_bundle_from_a_direct_path(self, tmp_path):
        """Non-symlink installs must keep working unchanged."""
        app, binary = _make_fake_bundle(tmp_path)
        assert cua_backend._resolve_cua_driver_app_path(str(binary)) == str(app)

    def test_resolves_through_a_chain_of_symlinks(self, tmp_path):
        """realpath collapses multi-hop links (brew-style relink chains)."""
        app, binary = _make_fake_bundle(tmp_path)

        first = tmp_path / "first"
        first.symlink_to(binary)
        second = tmp_path / "second"
        second.symlink_to(first)

        assert cua_backend._resolve_cua_driver_app_path(str(second)) == str(app)


class TestResolveCuaDriverAppPathFailsClosed:
    """The symlink fix must NOT weaken the deliberate no-fallback posture."""

    def test_returns_none_for_binary_outside_any_bundle(self, tmp_path):
        loose = tmp_path / "cua-driver"
        loose.write_text("#!/bin/sh\nexit 0\n")
        loose.chmod(0o755)

        assert cua_backend._resolve_cua_driver_app_path(str(loose)) is None

    def test_returns_none_when_symlink_target_is_outside_a_bundle(self, tmp_path):
        """A symlink must not manufacture a bundle that isn't there."""
        loose = tmp_path / "real-binary"
        loose.write_text("#!/bin/sh\nexit 0\n")
        loose.chmod(0o755)

        link = tmp_path / "cua-driver"
        link.symlink_to(loose)

        assert cua_backend._resolve_cua_driver_app_path(str(link)) is None

    def test_returns_none_when_bundle_executable_is_missing(self, tmp_path):
        """Marker present in the path but no real executable inside."""
        app = tmp_path / "CuaDriver.app"
        (app / "Contents" / "MacOS").mkdir(parents=True)
        phantom = app / "Contents" / "MacOS" / "cua-driver"

        assert cua_backend._resolve_cua_driver_app_path(str(phantom)) is None

    def test_returns_none_for_a_broken_symlink(self, tmp_path):
        link = tmp_path / "cua-driver"
        link.symlink_to(tmp_path / "does-not-exist")

        assert cua_backend._resolve_cua_driver_app_path(str(link)) is None

    def test_does_not_fall_back_to_applications(self, tmp_path):
        """Never reach for a /Applications copy the resolution chain didn't validate."""
        loose = tmp_path / "cua-driver"
        loose.write_text("#!/bin/sh\nexit 0\n")
        loose.chmod(0o755)

        result = cua_backend._resolve_cua_driver_app_path(str(loose))
        assert result is None
        assert result != "/Applications/CuaDriver.app"


class TestLiveInstallResolves:
    """Guard the real machine state, not just synthetic fixtures."""

    @pytest.mark.skipif(
        not os.path.exists("/Applications/CuaDriver.app"),
        reason="CuaDriver.app is not installed on this machine",
    )
    def test_installed_driver_on_path_resolves_to_a_bundle(self):
        import shutil

        driver = shutil.which("cua-driver")
        if not driver:
            pytest.skip("cua-driver is not on PATH")

        resolved = cua_backend._resolve_cua_driver_app_path(driver)
        assert resolved is not None, (
            f"cua-driver at {driver!r} did not resolve to a CuaDriver.app bundle — "
            "this is the exact failure that took computer_use down."
        )
        assert resolved.endswith(".app")
        assert os.path.isdir(resolved)
