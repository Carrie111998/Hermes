"""Windows batch-shim argument safety for the agent-browser CLI.

Regression tests for the stray-junk-file incidents of 2026-05-06 and
2026-07-27, where ``browser_console(expression=...)`` created files named
``b.outerHTML)`` / ``b.outerHTML).slice(0`` at the Hermes root.

Root cause: ``shutil.which("agent-browser")`` resolves to ``agent-browser.CMD``
on Windows.  ``CreateProcess`` runs a ``.cmd``/``.bat`` target through
``cmd.exe``, which *re-parses* the command line.  ``subprocess.list2cmdline``
only quotes arguments containing spaces or quotes, so a compact JS one-liner
(no spaces) reaches cmd.exe bare and the ``>`` of an ``=>`` arrow is taken as
an output redirection: the script is truncated (SyntaxError) and a cwd-relative
file is created holding the CLI's JSON error response.

The same defect lets ``&`` in a URL terminate the argument and execute the
remainder, so this is a command-injection surface, not only a litter problem.
"""

import base64
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


# ── eval payloads never traverse a command line verbatim ──────────────


class TestEvalArgsAreBase64Encoded:
    """``eval`` expressions go over the wire base64-encoded, not raw."""

    def test_eval_expression_is_base64_encoded(self):
        from tools.browser_tool import _cmd_safe_browser_args

        js = "Array.from(document.querySelectorAll('button')).map(b=>b.outerHTML).slice(0,20)"
        args = _cmd_safe_browser_args("eval", [js])

        assert args[0] == "-b"
        assert base64.b64decode(args[1]).decode("utf-8") == js

    def test_encoded_eval_args_contain_no_shell_metacharacters(self):
        from tools.browser_tool import _cmd_safe_browser_args

        js = "document.querySelector('a[href*=\"x\"]')?.outerHTML && a>b || c|d ^e %PATH% !v!"
        args = _cmd_safe_browser_args("eval", [js])

        joined = "".join(args)
        for meta in ('>', '<', '&', '|', '^', '%', '!', '"', "'", ' '):
            assert meta not in joined, f"{meta!r} survived base64 encoding"

    def test_non_eval_args_are_untouched(self):
        from tools.browser_tool import _cmd_safe_browser_args

        assert _cmd_safe_browser_args("click", ["@e3"]) == ["@e3"]
        assert _cmd_safe_browser_args("console", ["--clear"]) == ["--clear"]

    def test_eval_with_no_args_is_untouched(self):
        from tools.browser_tool import _cmd_safe_browser_args

        assert _cmd_safe_browser_args("eval", []) == []

    def test_already_encoded_eval_is_not_double_encoded(self):
        from tools.browser_tool import _cmd_safe_browser_args

        pre = ["-b", base64.b64encode(b"document.title").decode("ascii")]
        assert _cmd_safe_browser_args("eval", pre) == pre


# ── batch shims are bypassed so cmd.exe never re-parses argv ──────────


class TestWindowsShimResolution:
    """npm's ``.cmd`` shim is unwrapped to the native executable it calls."""

    def test_npm_cmd_shim_resolves_to_wrapped_executable(self, tmp_path):
        from tools.browser_tool import _resolve_batch_shim

        target_dir = tmp_path / "node_modules" / "agent-browser" / "bin"
        target_dir.mkdir(parents=True)
        target = target_dir / "agent-browser-win32-x64.exe"
        target.write_bytes(b"MZ")

        shim = tmp_path / "agent-browser.CMD"
        shim.write_text(
            '@ECHO off\r\n'
            '"%~dp0node_modules\\agent-browser\\bin\\agent-browser-win32-x64.exe" %*\r\n'
        )

        assert _resolve_batch_shim(str(shim)) == str(target)

    def test_non_batch_path_is_returned_unchanged(self, tmp_path):
        from tools.browser_tool import _resolve_batch_shim

        exe = tmp_path / "agent-browser"
        exe.write_text("#!/bin/sh\n")
        assert _resolve_batch_shim(str(exe)) == str(exe)

    def test_shim_pointing_at_missing_target_falls_back_to_shim(self, tmp_path):
        from tools.browser_tool import _resolve_batch_shim

        shim = tmp_path / "agent-browser.cmd"
        shim.write_text('@ECHO off\r\n"%~dp0node_modules\\nope\\gone.exe" %*\r\n')
        assert _resolve_batch_shim(str(shim)) == str(shim)

    def test_unreadable_shim_falls_back_to_shim(self, tmp_path):
        from tools.browser_tool import _resolve_batch_shim

        missing = tmp_path / "not-there.cmd"
        assert _resolve_batch_shim(str(missing)) == str(missing)


# ── both spawn sites are wired to the helpers ─────────────────────────


class TestChromeFallbackSpawnIsSafe:
    """The Lightpanda→Chrome fallback builds its own argv — it must be safe too."""

    def test_fallback_eval_argv_is_encoded_and_shim_free(self, tmp_path):
        from unittest.mock import patch

        import tools.browser_tool as bt

        target_dir = tmp_path / "node_modules" / "agent-browser" / "bin"
        target_dir.mkdir(parents=True)
        native = target_dir / "agent-browser-win32-x64.exe"
        native.write_bytes(b"MZ")
        shim = tmp_path / "agent-browser.cmd"
        shim.write_text(
            '@ECHO off\r\n'
            '"%~dp0node_modules\\agent-browser\\bin\\agent-browser-win32-x64.exe" %*\r\n'
        )

        js = "Array.from(x).map(b=>b.outerHTML).slice(0,20)"
        seen = []

        class _Proc:
            returncode = 0

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        def _fake_popen(argv, *a, **kw):
            seen.append(list(argv))
            # The real CLI writes JSON to the stdout fd the caller opened;
            # mimic a success so the fallback proceeds past its navigate step.
            out_fd = kw.get("stdout")
            if isinstance(out_fd, int):
                os.write(out_fd, b'{"success":true,"data":{}}')
            return _Proc()

        url_ok = {"success": True, "data": {"result": "https://example.com/"}}

        with patch.object(bt, "_run_browser_command", return_value=url_ok), \
             patch.object(bt, "_find_agent_browser", return_value=str(shim)), \
             patch.object(bt, "_chromium_installed", return_value=True), \
             patch.object(bt.subprocess, "Popen", side_effect=_fake_popen):
            bt._run_chrome_fallback_command("t", "eval", [js], 30)

        assert seen, "expected the fallback path to spawn agent-browser"
        eval_argv = [a for a in seen if "eval" in a]
        assert eval_argv, f"no eval spawn captured, got: {seen}"
        argv = eval_argv[0]

        assert argv[0] == str(native), f"batch shim not unwrapped: {argv[0]}"
        assert js not in argv, "raw JS reached the command line"
        assert "-b" in argv
        idx = argv.index("-b")
        assert base64.b64decode(argv[idx + 1]).decode("utf-8") == js


# ── the actual defect, exercised against the real OS ──────────────────


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe re-parsing is Windows-only")
class TestBatchShimReparseIsFixed:
    """End-to-end: a metacharacter-bearing argv must not reach cmd.exe bare."""

    def _echo_shim(self, directory: Path, name: str) -> Path:
        shim = directory / name
        shim.write_text('@echo off\r\necho ARGV:%*\r\n')
        return shim

    def test_redirect_arrow_creates_stray_file_without_the_fix(self, tmp_path):
        """Characterization: proves the raw shim really is unsafe."""
        shim = self._echo_shim(tmp_path, "raw.cmd")
        js = "Array.from(x).map(b=>b.outerHTML).slice(0,20)"

        subprocess.run(
            [str(shim), "--json", "eval", js],
            cwd=str(tmp_path), capture_output=True, timeout=60,
        )

        assert (tmp_path / "b.outerHTML).slice(0").exists(), (
            "expected the unpatched shim path to create a stray redirect file"
        )

    def test_base64_eval_argv_creates_no_stray_file(self, tmp_path):
        from tools.browser_tool import _cmd_safe_browser_args

        shim = self._echo_shim(tmp_path, "safe.cmd")
        js = "Array.from(x).map(b=>b.outerHTML).slice(0,20)"
        argv = [str(shim), "--json", "eval"] + _cmd_safe_browser_args("eval", [js])

        before = set(os.listdir(tmp_path))
        proc = subprocess.run(
            argv, cwd=str(tmp_path), capture_output=True, timeout=60,
        )
        after = set(os.listdir(tmp_path))

        assert before == after, f"stray files created: {after - before}"
        assert b"ARGV:" in proc.stdout

    def test_ampersand_url_does_not_execute_through_resolved_exe(self, tmp_path):
        """``&`` in a URL must not terminate the argument and run a command."""
        import shutil as _shutil

        from tools.browser_tool import _resolve_batch_shim

        # agent-browser is an npm CLI, so node is already a hard dependency.
        # Copy it in as a stand-in for the real native binary the shim wraps.
        node = _shutil.which("node")
        if not node:
            pytest.skip("node is required to stand in for the native binary")

        target_dir = tmp_path / "node_modules" / "agent-browser" / "bin"
        target_dir.mkdir(parents=True)
        real = target_dir / "agent-browser-win32-x64.exe"
        _shutil.copyfile(node, real)

        shim = tmp_path / "agent-browser.cmd"
        shim.write_text(
            '@ECHO off\r\n'
            '"%~dp0node_modules\\agent-browser\\bin\\agent-browser-win32-x64.exe" %*\r\n'
        )

        resolved = _resolve_batch_shim(str(shim))
        assert resolved == str(real)

        url = "https://example.com/jobs?a=1&b=2"
        proc = subprocess.run(
            [resolved, "-e", "console.log(process.argv[1])", url],
            cwd=str(tmp_path), capture_output=True, timeout=60,
        )
        assert proc.stdout.decode().strip() == url
