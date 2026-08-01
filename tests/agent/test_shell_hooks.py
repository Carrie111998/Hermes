"""Tests for the shell-hooks subprocess bridge (agent.shell_hooks).

These tests focus on the pure translation layer — JSON serialisation,
JSON parsing, matcher behaviour, block-schema correctness, and the
subprocess runner's graceful error handling.  Consent prompts are
covered in ``test_shell_hooks_consent.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent import shell_hooks


# ── helpers ───────────────────────────────────────────────────────────────


def _write_script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body)
    path.chmod(0o755)
    return path


def _allowlist_pair(monkeypatch, tmp_path, event: str, command: str) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
    shell_hooks._record_approval(event, command)


@pytest.fixture(autouse=True)
def _reset_registration_state():
    shell_hooks.reset_for_tests()
    yield
    shell_hooks.reset_for_tests()


# ── _parse_response ───────────────────────────────────────────────────────


class TestParseResponse:
    def test_block_claude_code_style(self):
        r = shell_hooks._parse_response(
            "pre_tool_call",
            '{"decision": "block", "reason": "nope"}',
        )
        assert r == {"action": "block", "message": "nope"}



    def test_empty_stdout_returns_none(self):
        assert shell_hooks._parse_response("pre_tool_call", "") is None
        assert shell_hooks._parse_response("pre_tool_call", "   ") is None















# ── _serialize_payload ────────────────────────────────────────────────────


class TestSerializePayload:
    def test_basic_pre_tool_call_schema(self):
        raw = shell_hooks._serialize_payload(
            "pre_tool_call",
            {
                "tool_name": "terminal",
                "args": {"command": "ls"},
                "session_id": "sess-1",
                "task_id": "t-1",
                "tool_call_id": "c-1",
            },
        )
        payload = json.loads(raw)
        assert payload["hook_event_name"] == "pre_tool_call"
        assert payload["tool_name"] == "terminal"
        assert payload["tool_input"] == {"command": "ls"}
        assert payload["session_id"] == "sess-1"
        assert "cwd" in payload
        # task_id / tool_call_id end up under extra
        assert payload["extra"]["task_id"] == "t-1"
        assert payload["extra"]["tool_call_id"] == "c-1"

    def test_args_not_dict_becomes_null(self):
        raw = shell_hooks._serialize_payload(
            "pre_tool_call", {"args": ["not", "a", "dict"]},
        )
        payload = json.loads(raw)
        assert payload["tool_input"] is None




# ── Matcher behaviour ─────────────────────────────────────────────────────


class TestMatcher:


    def test_alternation_matcher(self):
        spec = shell_hooks.ShellHookSpec(
            event="pre_tool_call", command="echo", matcher="terminal|file",
        )
        assert spec.matches_tool("terminal")
        assert spec.matches_tool("file")
        assert not spec.matches_tool("web")



    def test_matcher_leading_whitespace_stripped(self):
        """YAML quirks can introduce leading/trailing whitespace — must
        not silently break the matcher."""
        spec = shell_hooks.ShellHookSpec(
            event="pre_tool_call", command="echo", matcher=" terminal ",
        )
        assert spec.matcher == "terminal"
        assert spec.matches_tool("terminal")


    def test_whitespace_only_matcher_becomes_none(self):
        """A matcher that's pure whitespace is treated as 'no matcher'."""
        spec = shell_hooks.ShellHookSpec(
            event="pre_tool_call", command="echo", matcher="   ",
        )
        assert spec.matcher is None
        assert spec.matches_tool("anything")


# ── End-to-end subprocess behaviour ───────────────────────────────────────


class TestCallbackSubprocess:



    def test_block_translation_end_to_end(self, tmp_path):
        """v1 schema-bug regression gate.

        Shell hook returns the Claude-Code-style payload and the bridge
        must translate it to the canonical Hermes block shape so that
        get_pre_tool_call_block_message() surfaces the block.
        """
        script = _write_script(
            tmp_path, "blocker.sh",
            "#!/usr/bin/env bash\n"
            'printf \'{"decision": "block", "reason": "no terminal"}\\n\'\n',
        )
        spec = shell_hooks.ShellHookSpec(
            event="pre_tool_call",
            command=str(script),
            matcher="terminal",
        )
        cb = shell_hooks._make_callback(spec)
        result = cb(tool_name="terminal", args={"command": "rm -rf /"})
        assert result == {"action": "block", "message": "no terminal"}

    def test_block_aggregation_through_plugin_manager(self, tmp_path, monkeypatch):
        """Registering via register_from_config makes
        get_pre_tool_call_block_message surface the block — the real
        end-to-end control flow used by run_agent._invoke_tool."""
        from hermes_cli import plugins

        script = _write_script(
            tmp_path, "block.sh",
            "#!/usr/bin/env bash\n"
            'printf \'{"decision": "block", "reason": "blocked-by-shell"}\\n\'\n',
        )

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("HERMES_ACCEPT_HOOKS", "1")

        # Fresh manager
        plugins._plugin_manager = plugins.PluginManager()

        cfg = {
            "hooks": {
                "pre_tool_call": [
                    {"matcher": "terminal", "command": str(script)},
                ],
            },
        }
        registered = shell_hooks.register_from_config(cfg, accept_hooks=True)
        assert len(registered) == 1

        msg = plugins.get_pre_tool_call_block_message(
            tool_name="terminal",
            args={"command": "rm"},
        )
        assert msg == "blocked-by-shell"

    def test_matcher_regex_filters_callback(self, tmp_path, monkeypatch):
        """A matcher set to 'terminal' must not fire for 'web_search'."""
        calls = tmp_path / "calls.log"
        script = _write_script(
            tmp_path, "log.sh",
            f"#!/usr/bin/env bash\n"
            f"echo \"$(cat -)\" >> {calls}\n"
            f"printf '{{}}\\n'\n",
        )
        spec = shell_hooks.ShellHookSpec(
            event="pre_tool_call",
            command=str(script),
            matcher="terminal",
        )
        cb = shell_hooks._make_callback(spec)
        cb(tool_name="terminal", args={"command": "ls"})
        cb(tool_name="web_search", args={"q": "x"})
        cb(tool_name="file_read", args={"path": "x"})
        assert calls.exists()
        # Only the terminal call wrote to the log
        assert calls.read_text().count("pre_tool_call") == 1

    def test_payload_schema_delivered(self, tmp_path):
        capture = tmp_path / "payload.json"
        script = _write_script(
            tmp_path, "capture.sh",
            f"#!/usr/bin/env bash\ncat - > {capture}\nprintf '{{}}\\n'\n",
        )
        spec = shell_hooks.ShellHookSpec(
            event="pre_tool_call", command=str(script),
        )
        cb = shell_hooks._make_callback(spec)
        cb(
            tool_name="terminal",
            args={"command": "echo hi"},
            session_id="sess-77",
            task_id="task-77",
        )
        payload = json.loads(capture.read_text())
        assert payload["hook_event_name"] == "pre_tool_call"
        assert payload["tool_name"] == "terminal"
        assert payload["tool_input"] == {"command": "echo hi"}
        assert payload["session_id"] == "sess-77"
        assert "cwd" in payload
        assert payload["extra"]["task_id"] == "task-77"






# ── config parsing ────────────────────────────────────────────────────────


class TestParseHooksBlock:
    def test_valid_entry(self):
        specs = shell_hooks._parse_hooks_block({
            "pre_tool_call": [
                {"matcher": "terminal", "command": "/tmp/hook.sh", "timeout": 30},
            ],
        })
        assert len(specs) == 1
        assert specs[0].event == "pre_tool_call"
        assert specs[0].matcher == "terminal"
        assert specs[0].command == "/tmp/hook.sh"
        assert specs[0].timeout == 30






    def test_none_hooks_block(self):
        assert shell_hooks._parse_hooks_block(None) == []
        assert shell_hooks._parse_hooks_block("string") == []
        assert shell_hooks._parse_hooks_block([]) == []

    def test_non_tool_event_matcher_warns_and_drops(self, caplog):
        """matcher: is only honored for pre/post_tool_call; must warn
        and drop on other events so the spec reflects runtime."""
        import logging
        cfg = {"pre_llm_call": [{"matcher": "terminal", "command": "/bin/echo"}]}
        with caplog.at_level(logging.WARNING, logger=shell_hooks.logger.name):
            specs = shell_hooks._parse_hooks_block(cfg)
        assert len(specs) == 1 and specs[0].matcher is None
        assert any(
            "only honored for pre_tool_call" in r.getMessage()
            and "pre_llm_call" in r.getMessage()
            for r in caplog.records
        )


# ── Idempotent registration ───────────────────────────────────────────────


class TestIdempotentRegistration:
    def test_double_call_registers_once(self, tmp_path, monkeypatch):
        from hermes_cli import plugins

        script = _write_script(tmp_path, "h.sh",
                               "#!/usr/bin/env bash\nprintf '{}\\n'\n")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("HERMES_ACCEPT_HOOKS", "1")

        plugins._plugin_manager = plugins.PluginManager()

        cfg = {"hooks": {"on_session_start": [{"command": str(script)}]}}

        first = shell_hooks.register_from_config(cfg, accept_hooks=True)
        second = shell_hooks.register_from_config(cfg, accept_hooks=True)
        assert len(first) == 1
        assert second == []
        # Only one callback on the manager
        mgr = plugins.get_plugin_manager()
        assert len(mgr._hooks.get("on_session_start", [])) == 1

    def test_same_command_different_matcher_registers_both(
        self, tmp_path, monkeypatch,
    ):
        """Same script used for different matchers under one event must
        register both callbacks — dedupe keys on (event, matcher, command)."""
        from hermes_cli import plugins

        script = _write_script(tmp_path, "h.sh",
                               "#!/usr/bin/env bash\nprintf '{}\\n'\n")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("HERMES_ACCEPT_HOOKS", "1")

        plugins._plugin_manager = plugins.PluginManager()

        cfg = {
            "hooks": {
                "pre_tool_call": [
                    {"matcher": "terminal", "command": str(script)},
                    {"matcher": "web_search", "command": str(script)},
                ],
            },
        }

        registered = shell_hooks.register_from_config(cfg, accept_hooks=True)
        assert len(registered) == 2
        mgr = plugins.get_plugin_manager()
        assert len(mgr._hooks.get("pre_tool_call", [])) == 2


# ── Allowlist concurrency ─────────────────────────────────────────────────


class TestAllowlistConcurrency:
    """Regression tests for the Codex#1 finding: simultaneous
    _record_approval() calls used to collide on a fixed tmp path and
    silently lose entries under read-modify-write races."""


    def test_non_posix_fallback_does_not_self_deadlock(
        self, tmp_path, monkeypatch,
    ):
        """Regression: on platforms without fcntl, the fallback lock must
        be separate from _registered_lock.  register_from_config holds
        _registered_lock while calling _record_approval (via the consent
        prompt path), so a shared non-reentrant lock would self-deadlock."""
        import threading

        monkeypatch.setattr(shell_hooks, "fcntl", None)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

        completed = threading.Event()
        errors: list = []

        def target() -> None:
            try:
                with shell_hooks._registered_lock:
                    shell_hooks._record_approval(
                        "on_session_start", "/bin/x.sh",
                    )
                completed.set()
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
                completed.set()

        t = threading.Thread(target=target, daemon=True)
        t.start()
        if not completed.wait(timeout=3.0):
            pytest.fail(
                "non-POSIX fallback self-deadlocked — "
                "_locked_update_approvals must not reuse _registered_lock",
            )
        t.join(timeout=1.0)
        assert not errors, f"errors: {errors}"
        assert shell_hooks._is_allowlisted(
            "on_session_start", "/bin/x.sh",
        )




    def test_save_allowlist_uses_unique_tmp_paths(self, tmp_path, monkeypatch):
        """Two save_allowlist calls in flight must use distinct tmp files
        so the loser's os.replace does not ENOENT on the winner's sweep."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        p = shell_hooks.allowlist_path()
        p.parent.mkdir(parents=True, exist_ok=True)

        tmp_paths_seen: list = []
        real_mkstemp = shell_hooks.tempfile.mkstemp

        def spying_mkstemp(*args, **kwargs):
            fd, path = real_mkstemp(*args, **kwargs)
            tmp_paths_seen.append(path)
            return fd, path

        monkeypatch.setattr(shell_hooks.tempfile, "mkstemp", spying_mkstemp)

        shell_hooks.save_allowlist({"approvals": [{"event": "a", "command": "x"}]})
        shell_hooks.save_allowlist({"approvals": [{"event": "b", "command": "y"}]})

        assert len(tmp_paths_seen) == 2
        assert tmp_paths_seen[0] != tmp_paths_seen[1]


# ── script_content_hash ───────────────────────────────────────────────────


class TestScriptContentHash:
    def test_returns_sha256_of_script(self, tmp_path):
        script = tmp_path / "hook.sh"
        script.write_text("#!/bin/bash\necho hello\n")
        script.chmod(0o755)
        h = shell_hooks.script_content_hash(str(script))
        assert h is not None
        assert len(h) == 64  # SHA-256 hex digest

    def test_returns_none_for_missing_script(self, tmp_path):
        assert shell_hooks.script_content_hash(str(tmp_path / "nope")) is None

    def test_changes_when_content_changes(self, tmp_path):
        script = tmp_path / "hook.sh"
        script.write_text("echo original")
        script.chmod(0o755)
        h1 = shell_hooks.script_content_hash(str(script))
        script.write_text("echo modified")
        h2 = shell_hooks.script_content_hash(str(script))
        assert h1 != h2


# ── _is_allowlisted content-hash enforcement ──────────────────────────────


class TestAllowlistContentHash:
    def test_allowlisted_when_hash_matches(self, tmp_path, monkeypatch):
        script = tmp_path / "hook.sh"
        script.write_text("#!/bin/bash\necho safe\n")
        script.chmod(0o755)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        shell_hooks._record_approval("pre_tool_call", str(script))
        assert shell_hooks._is_allowlisted("pre_tool_call", str(script))

    def test_blocked_when_hash_mismatches(self, tmp_path, monkeypatch, caplog):
        """Editing a script after approval must trigger re-prompt."""
        import logging
        script = tmp_path / "hook.sh"
        script.write_text("#!/bin/bash\necho safe\n")
        script.chmod(0o755)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        shell_hooks._record_approval("pre_tool_call", str(script))
        assert shell_hooks._is_allowlisted("pre_tool_call", str(script))
        # Edit the script
        script.write_text("#!/bin/bash\necho MALICIOUS\n")
        with caplog.at_level(logging.WARNING, logger=shell_hooks.logger.name):
            assert not shell_hooks._is_allowlisted("pre_tool_call", str(script))
        assert any("hash mismatch" in r.getMessage() for r in caplog.records)

    def test_backward_compat_old_entry_without_hash(self, tmp_path, monkeypatch):
        """Old allowlist entries without script_hash_at_approval must
        still pass (backward compatibility)."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        # Manually write an old-format entry (no hash)
        data = {
            "approvals": [{
                "event": "pre_tool_call",
                "command": "/some/script.sh",
                "approved_at": "2025-01-01T00:00:00Z",
                "script_mtime_at_approval": None,
            }]
        }
        shell_hooks.save_allowlist(data)
        assert shell_hooks._is_allowlisted("pre_tool_call", "/some/script.sh")

    def test_record_approval_stores_hash(self, tmp_path, monkeypatch):
        script = tmp_path / "hook.sh"
        script.write_text("#!/bin/bash\necho test\n")
        script.chmod(0o755)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        shell_hooks._record_approval("pre_tool_call", str(script))
        entry = shell_hooks.allowlist_entry_for("pre_tool_call", str(script))
        assert entry is not None
        assert "script_hash_at_approval" in entry
        assert len(entry["script_hash_at_approval"]) == 64


class TestRuntimeHashEnforcement:
    """Regression: live callback must block execution when script content
    changes after approval (TOCTOU between registration and firing).

    Uses a script that emits a ``{"decision": "block", "reason": "..."}``
    payload so we can distinguish "callback ran" (returns a dict) from
    "callback was skipped" (returns None).
    """

    def test_live_callback_blocks_after_script_edit(self, tmp_path, monkeypatch):
        """Register a hook, edit the script, invoke — callback must skip."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
        script = _write_script(
            tmp_path, "hook.sh",
            '#!/bin/bash\necho \'{"decision": "block", "reason": "original"}\'\n',
        )
        command = str(script)
        _allowlist_pair(monkeypatch, tmp_path, "pre_tool_call", command)
        spec = shell_hooks.ShellHookSpec(
            event="pre_tool_call", command=command, matcher=None, timeout=5,
        )
        cb = shell_hooks._make_callback(spec)
        # First invocation should succeed (returns block dict)
        r1 = cb(tool_name="read_file", args={}, session_id="s1")
        assert r1 is not None, "First invocation should succeed"
        assert r1["action"] == "block"

        # Edit the script (simulate TOCTOU attack)
        script.write_text("#!/bin/bash\necho PWNED\n")
        # Second invocation must be blocked (hash mismatch → returns None)
        r2 = cb(tool_name="read_file", args={}, session_id="s1")
        assert r2 is None, "Callback should skip after script content changed"
