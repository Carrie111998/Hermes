"""Regression tests for #48510 — Windows codex.cmd resolution.

`npm i -g @openai/codex` on Windows installs `codex`, `codex.cmd` and
`codex.ps1` but no bare `codex.exe`. `subprocess.run`/`Popen` with a list
argument call `CreateProcess`, which does NOT consult `PATHEXT`, so a bare
`"codex"` raises FileNotFoundError even though the CLI is installed.

These are behaviour contracts, not snapshots: they assert the relationship
"whatever the default resolves to must be launchable", and that an explicit
caller-supplied path is never rewritten.
"""

import subprocess
import sys

import pytest

from agent.transports import codex_app_server as cas


class TestResolveCodexBin:
    def test_explicit_path_is_never_rewritten(self):
        """A caller who passes a real path must get it back verbatim."""
        explicit = r"C:\custom\codex.exe" if sys.platform == "win32" else "/opt/codex"
        assert cas.resolve_codex_bin(explicit) == explicit

    def test_default_resolves_when_only_cmd_shim_exists(self, tmp_path, monkeypatch):
        """The .cmd-only install (the #48510 repro) must resolve, not fall
        back to the bare name that CreateProcess cannot launch."""
        node_dir = tmp_path / "node"
        node_dir.mkdir()
        # Stock Windows npm layout: shims only, no bare .exe.
        (node_dir / "codex").write_text("#!/bin/sh\n")
        (node_dir / "codex.cmd").write_text("@echo off\n")
        (node_dir / "codex.ps1").write_text("# ps shim\n")

        shim = node_dir / "codex.cmd"
        monkeypatch.setattr(
            "hermes_constants.find_hermes_node_executable",
            lambda name: str(shim) if name == "codex" else None,
        )

        resolved = cas.resolve_codex_bin()
        assert resolved == str(shim)
        # The contract that actually matters: never hand CreateProcess a bare
        # name when a concrete path was discoverable.
        assert resolved != cas.DEFAULT_CODEX_BIN

    def test_ps1_is_never_selected(self, tmp_path, monkeypatch):
        """PowerShell blocks .ps1 under the default execution policy, so the
        resolver must not choose it (hermes_constants excludes it by design)."""
        import hermes_constants as hc

        candidates = hc._candidate_node_command_names("codex")
        assert not any(c.lower().endswith(".ps1") for c in candidates), candidates
        if sys.platform == "win32":
            assert candidates[0].lower().endswith(".cmd")

    def test_falls_back_to_input_when_nothing_resolves(self, monkeypatch):
        """No discoverable binary → return the original so the caller still
        gets the actionable 'not found, install with npm i -g' message."""
        monkeypatch.setattr(
            "hermes_constants.find_hermes_node_executable", lambda name: None
        )
        assert cas.resolve_codex_bin() == cas.DEFAULT_CODEX_BIN

    def test_resolver_failure_does_not_block_spawn(self, monkeypatch):
        """A broken resolver must degrade to the old behaviour, not raise."""

        def boom(name):
            raise RuntimeError("resolver exploded")

        monkeypatch.setattr("hermes_constants.find_hermes_node_executable", boom)
        assert cas.resolve_codex_bin() == cas.DEFAULT_CODEX_BIN


class TestCheckCodexBinaryUsesResolver:
    def test_default_call_spawns_resolved_path_not_bare_name(self, monkeypatch):
        """check_codex_binary() must hand subprocess the resolved path."""
        monkeypatch.setattr(
            "hermes_constants.find_hermes_node_executable",
            lambda name: r"C:\hermes\node\codex.cmd",
        )
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["argv0"] = cmd[0]
            return subprocess.CompletedProcess(cmd, 0, "codex-cli 0.145.0\n", "")

        monkeypatch.setattr(cas.subprocess, "run", fake_run)

        ok, ver = cas.check_codex_binary()
        assert ok, ver
        assert seen["argv0"] == r"C:\hermes\node\codex.cmd"

    def test_explicit_bin_is_passed_through_unchanged(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_constants.find_hermes_node_executable",
            lambda name: r"C:\should\not\be\used.cmd",
        )
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["argv0"] = cmd[0]
            return subprocess.CompletedProcess(cmd, 0, "codex-cli 0.145.0\n", "")

        monkeypatch.setattr(cas.subprocess, "run", fake_run)

        ok, _ = cas.check_codex_binary(codex_bin="/usr/local/bin/codex")
        assert ok
        assert seen["argv0"] == "/usr/local/bin/codex"

    def test_missing_binary_still_reports_actionable_error(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_constants.find_hermes_node_executable", lambda name: None
        )

        def fake_run(cmd, **kwargs):
            raise FileNotFoundError(2, "not found")

        monkeypatch.setattr(cas.subprocess, "run", fake_run)

        ok, msg = cas.check_codex_binary()
        assert not ok
        assert "npm i -g @openai/codex" in msg


class TestSessionUsesResolver:
    def test_session_default_bin_is_resolved(self, monkeypatch):
        """CodexAppServerSession must resolve at construction time so the
        spawn in ensure_started() never sees a bare name."""
        from agent.transports import codex_app_server_session as cass

        monkeypatch.setattr(
            "hermes_constants.find_hermes_node_executable",
            lambda name: r"C:\hermes\node\codex.cmd",
        )
        sess = cass.CodexAppServerSession(cwd=".")
        assert sess._codex_bin == r"C:\hermes\node\codex.cmd"

    def test_session_explicit_bin_wins(self, monkeypatch):
        from agent.transports import codex_app_server_session as cass

        monkeypatch.setattr(
            "hermes_constants.find_hermes_node_executable",
            lambda name: r"C:\should\not\be\used.cmd",
        )
        sess = cass.CodexAppServerSession(cwd=".", codex_bin="/opt/codex/bin/codex")
        assert sess._codex_bin == "/opt/codex/bin/codex"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PATHEXT behaviour")
class TestWindowsCreateProcessContract:
    def test_bare_cmd_name_is_unlaunchable_by_createprocess(self, tmp_path, monkeypatch):
        """Documents the root cause: shutil.which finds a .cmd that
        subprocess cannot launch by bare name. If CPython ever changes this,
        the test fails loudly and the workaround can be revisited."""
        import shutil

        d = tmp_path / "shimdir"
        d.mkdir()
        (d / "hermestestshim.cmd").write_text("@echo off\r\necho ok\r\n")
        monkeypatch.setenv("PATH", str(d), prepend=False)

        assert shutil.which("hermestestshim") is not None
        with pytest.raises(FileNotFoundError):
            subprocess.run(
                ["hermestestshim"], capture_output=True, timeout=10,
                stdin=subprocess.DEVNULL,
            )
