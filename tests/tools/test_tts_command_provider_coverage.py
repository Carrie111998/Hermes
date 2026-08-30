"""Command-provider TTS cluster coverage for tools/tts_tool.py.

This module exercises the *command-provider* code path on top of what
``tests/tools/test_tts_command_providers.py`` already covers. It focuses on
the branches that file leaves uncovered:

* ``_is_command_provider_config`` with a non-dict / blank-command config;
* ``_resolve_command_provider_config`` with an empty provider name;
* the ``_dispatch_to_plugin_provider`` command-provider short-circuit (a
  same-name ``type: command`` block wins over a plugin);
* ``_get_command_tts_timeout`` invalid / non-positive fallbacks;
* ``_is_command_tts_voice_compatible`` string parse;
* ``_shell_quote_context`` bare-backslash and ``_quote_command_tts_placeholder``
  single-quote branches;
* ``_render_command_tts_template`` ``$``-guard and double-brace aliases;
* ``_command_provider_env_passthrough`` tuple / non-list handling;
* ``_terminate_command_tts_process_tree`` already-exited and live-child paths;
* ``_has_any_command_tts_provider`` default-config load;
* ``_configured_command_tts_output_path`` extension swap.

Execution-facing tests run the *real* subprocess path (success, command-not-found,
non-zero exit, timeout, empty output) rather than mocking ``subprocess.run`` /
``Popen``: the real command execution is precisely what this cluster owns.
Every command is a tiny portable Python one-liner (never ``ffmpeg``) and returns
well under two seconds.
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tools import tts_tool
from tools.tts_tool import (
    DEFAULT_COMMAND_TTS_OUTPUT_FORMAT,
    DEFAULT_COMMAND_TTS_TIMEOUT_SECONDS,
    _command_provider_env_passthrough,
    _configured_command_tts_output_path,
    _dispatch_to_plugin_provider,
    _generate_command_tts,
    _get_command_tts_timeout,
    _get_provider_section,
    _has_any_command_tts_provider,
    _is_command_provider_config,
    _is_command_tts_voice_compatible,
    _iter_command_providers,
    _quote_command_tts_placeholder,
    _render_command_tts_template,
    _resolve_command_provider_config,
    _run_command_tts,
    _shell_quote_context,
    _terminate_command_tts_process_tree,
)

PY = sys.executable
SKIP_NT = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only shell semantics"
)


# ---------------------------------------------------------------------------
# _is_command_provider_config
# ---------------------------------------------------------------------------


class TestIsCommandProviderConfig:
    def test_non_dict_config_is_false(self):
        """A non-dict value (list) never counts as a command provider."""

        assert _is_command_provider_config(["command", "echo hi"]) is False

    def test_blank_command_is_false(self):
        assert _is_command_provider_config(
            {"type": "command", "command": "   "}
        ) is False

    def test_command_value_mismatch_is_false(self):
        assert _is_command_provider_config({"type": "command", "command": 123}) is False


# ---------------------------------------------------------------------------
# _resolve_command_provider_config
# ---------------------------------------------------------------------------


class TestResolveCommandProviderConfig:
    def test_empty_provider_name_returns_none(self):
        assert _resolve_command_provider_config("", {"providers": {}}) is None

    def test_command_provider_resolves_to_config(self):
        cfg = {"providers": {"piper-cli": {"type": "command", "command": "x"}}}
        assert _resolve_command_provider_config("piper-cli", cfg) == {
            "type": "command",
            "command": "x",
        }


# ---------------------------------------------------------------------------
# _dispatch_to_plugin_provider — command-provider wins over a same-name plugin
# ---------------------------------------------------------------------------


class TestDispatchToPluginProviderCommandWins:
    def test_command_provider_short_circuits_plugin_lookup(self):
        """A same-name ``type: command`` block must bail before any plugin
        registry lookup (PR #17843 invariant)."""
        cfg = {"providers": {"overlap": {"type": "command", "command": "echo hi"}}}
        assert _dispatch_to_plugin_provider("overlap", "/tmp/o.mp3", "overlap", cfg) is None

    def test_builtin_name_short_circuits(self):
        assert _dispatch_to_plugin_provider("openai", "/tmp/o.mp3", "openai", {}) is None

    def test_empty_provider_short_circuits(self):
        assert _dispatch_to_plugin_provider("", "/tmp/o.mp3", "", {}) is None


# ---------------------------------------------------------------------------
# _iter_command_providers
# ---------------------------------------------------------------------------


class TestIterCommandProviders:
    def test_non_dict_config_yields_nothing(self):
        assert list(_iter_command_providers(["not", "a", "dict"])) == []


class TestGetProviderSection:
    def test_non_dict_returns_empty(self):
        assert _get_provider_section(None, "providers") == {}

    def test_missing_section_returns_empty(self):
        assert _get_provider_section({"tts": {}}, "providers") == {}


# ---------------------------------------------------------------------------
# config getters
# ---------------------------------------------------------------------------


class TestCommandConfigGetters:
    def test_timeout_invalid_value_falls_back(self):
        assert _get_command_tts_timeout({"timeout": "soon"}) == float(
            DEFAULT_COMMAND_TTS_TIMEOUT_SECONDS
        )

    def test_timeout_zero_falls_back(self):
        assert _get_command_tts_timeout({"timeout": 0}) == float(
            DEFAULT_COMMAND_TTS_TIMEOUT_SECONDS
        )

    def test_timeout_negative_falls_back(self):
        assert _get_command_tts_timeout({"timeout": -7}) == float(
            DEFAULT_COMMAND_TTS_TIMEOUT_SECONDS
        )

    def test_timeout_seconds_alias(self):
        assert _get_command_tts_timeout({"timeout_seconds": 42}) == 42.0

    def test_voice_compatible_truthy_string(self):
        assert _is_command_tts_voice_compatible({"voice_compatible": "true"}) is True

    def test_voice_compatible_falsy_string(self):
        assert _is_command_tts_voice_compatible({"voice_compatible": "off"}) is False

    def test_output_format_prefers_path_suffix(self):
        cfg = {"output_format": "ogg"}
        assert tts_tool._get_command_tts_output_format(cfg, "/tmp/clip.wav") == "wav"

    def test_output_format_invalid_falls_back_to_default(self):
        assert tts_tool._get_command_tts_output_format(
            {"format": "bogus", "output_format": "exe"}
        ) == DEFAULT_COMMAND_TTS_OUTPUT_FORMAT


# ---------------------------------------------------------------------------
# _shell_quote_context / _quote_command_tts_placeholder
# ---------------------------------------------------------------------------


class TestShellQuoteContextEdgeCases:
    def test_bare_backslash_does_not_open_quote(self):
        """A bare backslash in the template is skipped, not treated as a quote."""
        # "a\b" — the backslash sits in bare context; scanning past it returns None.
        assert _shell_quote_context("a\\b", 3) is None

    def test_mid_string_bare_context(self):
        assert _shell_quote_context("echo hello world", 6) is None


class TestQuoteCommandTtsPlaceholderEdgeCases:
    def test_single_quote_context(self):
        # Value containing a single quote, inserted inside a single-quoted
        # region, is escaped with the classic '\'' idiom.
        assert _quote_command_tts_placeholder("bob's", "'") == "bob'\\''s"

    def test_double_quote_context_escapes_metacharacters(self):
        rendered = _quote_command_tts_placeholder('a$b"c', '"')
        assert rendered == 'a\\$b\\"c'

    def test_bare_context_quotes_for_posix(self):
        if sys.platform != "win32":
            assert _quote_command_tts_placeholder("/tmp/a b", None) == "'/tmp/a b'"


# ---------------------------------------------------------------------------
# _render_command_tts_template edge cases
# ---------------------------------------------------------------------------


class TestRenderCommandTtsTemplateEdgeCases:
    def test_dollar_prefix_blocks_substitution(self):
        """``\\${x}`` is written for a shell variable and must not be treated as
        a placeholder."""
        rendered = _render_command_tts_template("echo ${x}", {"x": "VAL"})
        assert rendered == "echo ${x}"

    def test_double_brace_aliases_placeholder(self):
        rendered = _render_command_tts_template("echo {{x}}", {"x": "VAL"})
        assert "VAL" in rendered

    def test_unknown_double_braces_stay_literal(self):
        rendered = _render_command_tts_template("echo {{not_a_key}}", {"x": "VAL"})
        assert "{not_a_key}" in rendered


# ---------------------------------------------------------------------------
# _command_provider_env_passthrough
# ---------------------------------------------------------------------------


class TestCommandProviderEnvPassthroughEdgeCases:
    def test_tuple_allowlist_strips_whitespace(self):
        assert _command_provider_env_passthrough(
            {"env_passthrough": ("A_KEY", " B_KEY ")}
        ) == ["A_KEY", "B_KEY"]

    def test_non_list_allowlist_returns_empty(self):
        assert _command_provider_env_passthrough({"env_passthrough": "A_KEY"}) == []


# ---------------------------------------------------------------------------
# _run_command_tts — REAL subprocess execution (no mocks)
# ---------------------------------------------------------------------------


class TestRunCommandTtsRealExecution:
    def test_success_captures_stdout(self):
        result = _run_command_tts(
            f'"{PY}" -c "print(\'hello world\')"', timeout=5,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "hello world"

    def test_command_not_found_raises_cleanly(self):
        """The shell returns 127; the error surfaces as CalledProcessError,
        never a bare traceback."""
        with pytest.raises(subprocess.CalledProcessError) as excinfo:
            _run_command_tts("definitely_not_a_real_command_xyz_123", timeout=5)
        assert excinfo.value.returncode != 0

    def test_nonzero_exit_surfaces_returncode_and_stderr(self):
        cmd = f'"{PY}" -c "import sys; sys.stderr.write(\'boom\'); sys.exit(3)"'
        with pytest.raises(subprocess.CalledProcessError) as excinfo:
            _run_command_tts(cmd, timeout=5)
        assert excinfo.value.returncode == 3
        assert "boom" in (excinfo.value.stderr or "")

    @SKIP_NT
    def test_timeout_raises_timeout_expired(self):
        cmd = f'"{PY}" -c "import time; time.sleep(30)"'
        with pytest.raises(subprocess.TimeoutExpired):
            _run_command_tts(cmd, timeout=1)

    @SKIP_NT
    def test_wait_times_out_after_streams_close(self):
        """When both output streams reach EOF but the process keeps running,
        the ``proc.wait`` post-loop branch must still time out and clean up."""
        cmd = (
            f'"{PY}" -c "import sys,time; '
            f"sys.stdout.close(); sys.stderr.close(); time.sleep(30)\""
        )
        with pytest.raises(subprocess.TimeoutExpired):
            _run_command_tts(cmd, timeout=1)


# ---------------------------------------------------------------------------
# _terminate_command_tts_process_tree
# ---------------------------------------------------------------------------


class TestTerminateCommandTtsProcessTree:
    def test_already_exited_process_returns_immediately(self):
        proc = subprocess.Popen("true", shell=True, start_new_session=True)
        proc.wait()
        assert _terminate_command_tts_process_tree(proc) is None

    @SKIP_NT
    def test_terminates_live_child_process_tree(self):
        """Terminating the shell must also reap its child (``&& true`` forces
        sh to fork a real child instead of exec-ing in place)."""
        cmd = f'"{PY}" -c "import time; time.sleep(30)" ; true'
        proc = subprocess.Popen(
            cmd, shell=True, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # Give the shell a moment to fork its python child.
        import time as _t

        _t.sleep(0.3)
        assert _terminate_command_tts_process_tree(proc) is None
        returncode = proc.wait(timeout=5)
        assert returncode is not None


# ---------------------------------------------------------------------------
# _generate_command_tts — REAL subprocess execution (no mocks)
# ---------------------------------------------------------------------------


class TestGenerateCommandTtsRealExecution:
    def test_writes_valid_wav_and_replaces_existing_output(self, tmp_path):
        """Success path: a real command writes a valid WAV at {output_path},
        and a pre-existing output file is unlinked first."""
        out = tmp_path / "clip.wav"
        script = tmp_path / "gen_wav.py"
        script.write_text(
            "import sys, wave\n"
            "with wave.open(sys.argv[1], 'w') as w:\n"
            "    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)\n"
            "    w.writeframes(b'\\x00' * 320)\n",
            encoding="utf-8",
        )
        out.write_bytes(b"stale-bytes")

        config = {
            "type": "command",
            "command": f'"{PY}" "{script}" {{output_path}}',
            "output_format": "wav",
        }
        result = _generate_command_tts("hello", str(out), "py-wav", config, {})
        assert result == str(out)
        assert out.exists()
        data = out.read_bytes()
        assert data[:4] == b"RIFF"
        assert data != b"stale-bytes"

    def test_missing_command_raises_value_error(self):
        with pytest.raises(ValueError, match="command is not configured"):
            _generate_command_tts(
                "hi", "/tmp/x.mp3", "broken",
                {"type": "command", "command": "   "}, {},
            )

    def test_command_not_found_surfaces_sanitized_error(self):
        """Non-zero exit from a bad command yields a sanitized RuntimeError,
        not a raw traceback."""
        with pytest.raises(RuntimeError, match="exited with code"):
            _generate_command_tts(
                "hi", "/tmp/x.mp3", "nope",
                {"type": "command", "command": "definitely_not_a_command_xyz_123"},
                {},
            )

    def test_nonzero_exit_surfaces_stderr_cleanly(self, tmp_path):
        cmd = f'"{PY}" -c "import sys; sys.stderr.write(\'boom\'); sys.exit(3)"'
        with pytest.raises(RuntimeError) as excinfo:
            _generate_command_tts(
                "hi", str(tmp_path / "x.mp3"), "exit3",
                {"type": "command", "command": cmd}, {},
            )
        message = str(excinfo.value)
        assert "exited with code 3" in message
        assert "boom" in message
        assert "Traceback" not in message

    def test_nonzero_exit_surfaces_stdout_and_stderr(self, tmp_path):
        cmd = (
            f'"{PY}" -c "import sys; '
            f"sys.stdout.write('so'); sys.stderr.write('boom'); sys.exit(3)\""
        )
        with pytest.raises(RuntimeError) as excinfo:
            _generate_command_tts(
                "hi", str(tmp_path / "x.mp3"), "both",
                {"type": "command", "command": cmd}, {},
            )
        message = str(excinfo.value)
        assert "exited with code 3" in message
        assert "stderr: boom" in message
        assert "stdout: so" in message

    def test_empty_output_raises_runtime_error(self, tmp_path):
        cmd = f'"{PY}" -c "print(\'hi\')"'
        with pytest.raises(RuntimeError, match="produced no output"):
            _generate_command_tts(
                "hi", str(tmp_path / "x.mp3"), "echo-only",
                {"type": "command", "command": cmd}, {},
            )

    @SKIP_NT
    def test_timeout_raises_runtime_error(self, tmp_path):
        config = {
            "command": f'"{PY}" -c "import time; time.sleep(30)"',
            "timeout": 1,
        }
        with pytest.raises(RuntimeError, match="timed out"):
            _generate_command_tts(
                "hi", str(tmp_path / "x.mp3"), "slow", config, {},
            )


# ---------------------------------------------------------------------------
# _configured_command_tts_output_path / _has_any_command_tts_provider
# ---------------------------------------------------------------------------


class TestConfiguredCommandTtsOutputPath:
    def test_swaps_suffix_to_provider_format(self):
        path = _configured_command_tts_output_path(
            Path("/tmp/clip.mp3"), {"output_format": "wav"}
        )
        assert path == Path("/tmp/clip.wav")


class TestHasAnyCommandTtsProviderDefaultConfig:
    def test_default_load_finds_provider(self):
        cfg = {
            "provider": "x",
            "providers": {"x": {"type": "command", "command": "echo x"}},
        }
        with patch.object(tts_tool, "_load_tts_config", return_value=cfg):
            assert _has_any_command_tts_provider() is True

    def test_default_load_finds_none(self):
        with patch.object(tts_tool, "_load_tts_config", return_value={}):
            assert _has_any_command_tts_provider() is False
