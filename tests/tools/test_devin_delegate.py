#!/usr/bin/env python3
"""Tests for the Devin CLI delegate backend (tools/devin_delegate.py).

Covers the check_fn gate, config resolution, prompt building, and the
subprocess handler's success/error/timeout paths — all with the `devin`
binary and its auth probe mocked so no real Devin invocation happens.

Run with:  python -m pytest tests/tools/test_devin_delegate.py -v
   or:     python tests/tools/test_devin_delegate.py
"""

import json
import subprocess
import unittest
from unittest.mock import patch

import tools.devin_delegate as mod
from tools.devin_delegate import (
    DELEGATE_TO_DEVIN_SCHEMA,
    _build_prompt,
    _resolve_max_result_chars,
    _resolve_permission_mode,
    _resolve_timeout,
    check_devin_requirements,
    delegate_to_devin,
)


def _ok_proc(stdout="", stderr="", returncode=0):
    """A CompletedProcess-like object for mocked subprocess.run."""
    return subprocess.CompletedProcess(
        args=["devin"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class TestCheckRequirements(unittest.TestCase):
    """check_devin_requirements is the schema gate — must be cheap and exact."""

    def test_disabled_when_not_enabled(self):
        with patch.object(mod, "_devin_config", lambda: {}), \
             patch.object(mod, "_devin_binary", lambda: "/usr/bin/devin"):
            self.assertFalse(check_devin_requirements())

    def test_disabled_when_enabled_but_no_binary(self):
        with patch.object(mod, "_devin_config", lambda: {"enabled": True}), \
             patch.object(mod, "_devin_binary", lambda: None):
            self.assertFalse(check_devin_requirements())

    def test_enabled_when_enabled_and_binary_present(self):
        with patch.object(mod, "_devin_config", lambda: {"enabled": "true"}), \
             patch.object(mod, "_devin_binary", lambda: "/usr/bin/devin"):
            self.assertTrue(check_devin_requirements())

    def test_falsy_enabled_value_rejected(self):
        with patch.object(mod, "_devin_config", lambda: {"enabled": "false"}), \
             patch.object(mod, "_devin_binary", lambda: "/usr/bin/devin"):
            self.assertFalse(check_devin_requirements())


class TestConfigResolution(unittest.TestCase):
    def test_permission_mode_default(self):
        self.assertEqual(_resolve_permission_mode({}), "dangerous")

    def test_permission_mode_override(self):
        self.assertEqual(_resolve_permission_mode({"permission_mode": "auto"}), "auto")

    def test_permission_mode_invalid_falls_back(self):
        self.assertEqual(_resolve_permission_mode({"permission_mode": "yolo"}), "dangerous")

    def test_timeout_default(self):
        self.assertEqual(_resolve_timeout({}, None), 1800.0)

    def test_timeout_floored_at_60(self):
        self.assertEqual(_resolve_timeout({}, 5), 60.0)

    def test_timeout_override(self):
        self.assertEqual(_resolve_timeout({}, 120), 120.0)

    def test_timeout_config_used_when_no_override(self):
        self.assertEqual(_resolve_timeout({"timeout_seconds": 300}, None), 300.0)

    def test_timeout_invalid_falls_back(self):
        self.assertEqual(_resolve_timeout({"timeout_seconds": "abc"}, None), 1800.0)

    def test_max_result_chars_default(self):
        self.assertEqual(_resolve_max_result_chars({}), 20000)

    def test_max_result_chars_floor(self):
        self.assertEqual(_resolve_max_result_chars({"max_result_chars": 10}), 20000)


class TestBuildPrompt(unittest.TestCase):
    def test_goal_only(self):
        self.assertEqual(_build_prompt("fix the bug", None), "fix the bug")
        self.assertEqual(_build_prompt("fix the bug", ""), "fix the bug")

    def test_goal_with_context(self):
        out = _build_prompt("fix the bug", "see src/foo.py line 42")
        self.assertIn("fix the bug", out)
        self.assertIn("--- Context ---", out)
        self.assertIn("see src/foo.py line 42", out)


class TestDelegateToDevin(unittest.TestCase):
    """Handler paths with subprocess + auth fully mocked."""

    def _patch_env(self, cfg=None, binary="/usr/bin/devin", logged_in=True,
                   auth_detail=""):
        cfg = cfg if cfg is not None else {"enabled": True}
        return (
            patch.object(mod, "_devin_config", lambda: cfg),
            patch.object(mod, "_devin_binary", lambda: binary),
            patch.object(mod, "_is_logged_in", lambda: (logged_in, auth_detail)),
        )

    def test_missing_goal_returns_error(self):
        with patch.object(mod, "_devin_config", lambda: {"enabled": True}):
            result = json.loads(delegate_to_devin(goal="   "))
        self.assertIn("error", result)
        self.assertIn("goal", result["error"])

    def test_no_binary_returns_error(self):
        with patch.object(mod, "_devin_config", lambda: {"enabled": True}), \
             patch.object(mod, "_devin_binary", lambda: None):
            result = json.loads(delegate_to_devin(goal="do thing"))
        self.assertIn("error", result)
        self.assertIn("not on $PATH", result["error"])

    def test_not_logged_in_returns_error(self):
        p1, p2, p3 = self._patch_env(logged_in=False,
                                     auth_detail="Devin is not authenticated (logged out).")
        with p1, p2, p3:
            result = json.loads(delegate_to_devin(goal="do thing"))
        self.assertIn("error", result)
        self.assertIn("not authenticated", result["error"])

    def test_success_returns_completed_result(self):
        p1, p2, p3 = self._patch_env()
        with p1, p2, p3, \
             patch.object(mod.subprocess, "run",
                          return_value=_ok_proc(stdout="All done. Fixed the bug.")):
            result = json.loads(delegate_to_devin(goal="fix the bug", context="see foo.py"))
        self.assertEqual(result["results"][0]["status"], "completed")
        self.assertEqual(result["results"][0]["summary"], "All done. Fixed the bug.")
        self.assertEqual(result["results"][0]["exit_reason"], "completed")
        self.assertEqual(result["results"][0]["backend"], "devin")
        self.assertFalse(result["results"][0]["truncated"])
        self.assertIn("duration_seconds", result["results"][0])

    def test_nonzero_exit_returns_error_result(self):
        p1, p2, p3 = self._patch_env()
        with p1, p2, p3, \
             patch.object(mod.subprocess, "run",
                          return_value=_ok_proc(stdout="", stderr="boom",
                                                returncode=2)):
            result = json.loads(delegate_to_devin(goal="do thing"))
        entry = result["results"][0]
        self.assertEqual(entry["status"], "error")
        self.assertEqual(entry["exit_reason"], "error")
        self.assertIn("code 2", entry["error"])
        self.assertIn("boom", entry["error"])

    def test_timeout_returns_timeout_result(self):
        p1, p2, p3 = self._patch_env(cfg={"enabled": True, "timeout_seconds": 60})
        with p1, p2, p3, \
             patch.object(mod.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired(cmd=["devin"], timeout=60)):
            result = json.loads(delegate_to_devin(goal="do thing"))
        entry = result["results"][0]
        self.assertEqual(entry["status"], "timeout")
        self.assertEqual(entry["exit_reason"], "timeout")
        self.assertIn("60s", entry["error"])

    def test_truncation_flag_when_stdout_exceeds_limit(self):
        p1, p2, p3 = self._patch_env(cfg={"enabled": True, "max_result_chars": 256})
        long_out = "x" * 1000
        with p1, p2, p3, \
             patch.object(mod.subprocess, "run",
                          return_value=_ok_proc(stdout=long_out)):
            result = json.loads(delegate_to_devin(goal="do thing"))
        entry = result["results"][0]
        self.assertEqual(entry["status"], "completed")
        self.assertTrue(entry["truncated"])
        self.assertEqual(len(entry["summary"]), 256)

    def test_model_override_passed_to_argv(self):
        p1, p2, p3 = self._patch_env()
        captured = {}

        def fake_run(argv, **kw):
            captured["argv"] = argv
            return _ok_proc(stdout="ok")

        with p1, p2, p3, patch.object(mod.subprocess, "run", side_effect=fake_run):
            delegate_to_devin(goal="do thing", model="opus")
        argv = captured["argv"]
        self.assertIn("--model", argv)
        self.assertIn("opus", argv)
        # Print mode + unattended defaults always present.
        self.assertIn("-p", argv)
        self.assertIn("--permission-mode", argv)
        self.assertIn("dangerous", argv)
        self.assertIn("--respect-workspace-trust", argv)
        self.assertIn("false", argv)
        # Prompt after the -- separator.
        self.assertEqual(argv[argv.index("--") + 1:], ["do thing"])

    def test_permission_mode_from_config_not_model_controllable(self):
        p1, p2, p3 = self._patch_env(cfg={"enabled": True, "permission_mode": "accept-edits"})
        captured = {}

        def fake_run(argv, **kw):
            captured["argv"] = argv
            return _ok_proc(stdout="ok")

        with p1, p2, p3, patch.object(mod.subprocess, "run", side_effect=fake_run):
            delegate_to_devin(goal="do thing")
        argv = captured["argv"]
        idx = argv.index("--permission-mode")
        self.assertEqual(argv[idx + 1], "accept-edits")


class TestSchema(unittest.TestCase):
    def test_name_and_required(self):
        self.assertEqual(DELEGATE_TO_DEVIN_SCHEMA["name"], "delegate_to_devin")
        self.assertEqual(DELEGATE_TO_DEVIN_SCHEMA["parameters"]["required"], ["goal"])

    def test_has_goal_context_model_timeout(self):
        props = DELEGATE_TO_DEVIN_SCHEMA["parameters"]["properties"]
        for key in ("goal", "context", "model", "timeout"):
            self.assertIn(key, props)

    def test_permission_mode_not_model_controllable(self):
        """permission_mode is a security-sensitive knob — config-only, never
        in the model-facing schema (a prompt-injected model must not be able
        to escalate Devin's autonomy)."""
        props = DELEGATE_TO_DEVIN_SCHEMA["parameters"]["properties"]
        self.assertNotIn("permission_mode", props)


class TestRegistryWiring(unittest.TestCase):
    """The tool self-registers and is gated by check_fn in the registry."""

    def test_registered_in_delegation_toolset(self):
        from tools.registry import registry
        entry = registry.get_entry("delegate_to_devin")
        self.assertIsNotNone(entry, "delegate_to_devin not registered")
        self.assertEqual(entry.toolset, "delegation")
        self.assertEqual(entry.check_fn, check_devin_requirements)

    def test_listed_in_core_tools_and_delegation_toolset(self):
        import toolsets
        self.assertIn("delegate_to_devin", toolsets._HERMES_CORE_TOOLS)
        self.assertIn("delegate_to_devin", toolsets.TOOLSETS["delegation"]["tools"])


if __name__ == "__main__":
    unittest.main()
