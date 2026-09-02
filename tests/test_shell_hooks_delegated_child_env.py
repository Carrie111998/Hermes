"""Regression tests for delegated-child lineage across shell hook spawns.

``_spawn`` builds the ``subprocess.Popen`` kwargs for a shell hook. It used to
leave ``env`` unset, which inherits ``os.environ``. That silently drops the
delegated-child marker: ``delegate_task`` marks a child through a ContextVar,
and ContextVars do not cross a process boundary. A hook that gates on caller
identity therefore saw every child invocation as if it came from the parent
agent, and dispatcher-owned ``HERMES_KANBAN_*`` leaked into the hook as well.

``delegated_child_subprocess_env()`` already solves both halves and is used by
tts_tool, transcription_tools and code_execution_tool. It returns ``None``
outside a delegated child, so the parent keeps plain inherit semantics.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from agent import shell_hooks
from agent.delegation_context import (
    DELEGATED_CHILD_ENV_MARKER,
    delegated_child_context,
)


def _marker_echo_spec(tmp_path):
    """A hook that writes whatever it sees for the marker to stderr."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import os, sys\n"
        f"sys.stderr.write(repr(os.environ.get({DELEGATED_CHILD_ENV_MARKER!r})))\n",
        encoding="utf-8",
    )
    return shell_hooks.ShellHookSpec(
        event="pre_tool_call",
        command=f"{sys.executable} {probe}",
        timeout=30,
    )


def _run(spec):
    return shell_hooks._spawn(spec, json.dumps({"tool_name": "write_file"}))


def test_parent_spawn_has_no_child_marker():
    with tempfile.TemporaryDirectory() as td:
        spec = _marker_echo_spec(Path(td))
        assert _run(spec).get("stderr", "").strip() == "None"


def test_delegated_child_spawn_carries_marker():
    with tempfile.TemporaryDirectory() as td:
        spec = _marker_echo_spec(Path(td))
        with delegated_child_context("test-child"):
            seen = _run(spec).get("stderr", "").strip()
        assert seen not in ("None", "", "''")


def test_parent_spawn_after_child_is_clean_again():
    """The child context must not bleed into subsequent parent spawns."""
    with tempfile.TemporaryDirectory() as td:
        spec = _marker_echo_spec(Path(td))
        with delegated_child_context("test-child"):
            _run(spec)
        assert _run(spec).get("stderr", "").strip() == "None"


def test_parent_env_untouched_outside_delegation(monkeypatch):
    """Outside a delegated child the helper returns None, so Popen inherits.

    Guards the branch that keeps the pre-existing behaviour: a sentinel set in
    ``os.environ`` must still reach the hook when no child context is active.
    """
    monkeypatch.setenv("HERMES_TEST_INHERIT_SENTINEL", "kept")
    with tempfile.TemporaryDirectory() as td:
        probe = Path(td) / "probe.py"
        probe.write_text(
            "import os, sys\n"
            "sys.stderr.write(os.environ.get('HERMES_TEST_INHERIT_SENTINEL', ''))\n",
            encoding="utf-8",
        )
        spec = shell_hooks.ShellHookSpec(
            event="pre_tool_call",
            command=f"{sys.executable} {probe}",
            timeout=30,
        )
        assert _run(spec).get("stderr", "").strip() == "kept"


def test_kanban_dispatch_vars_scrubbed_for_child(monkeypatch):
    """Dispatcher-owned Kanban vars must not reach a delegated child's hook."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "parent-task-123")
    with tempfile.TemporaryDirectory() as td:
        probe = Path(td) / "probe.py"
        probe.write_text(
            "import os, sys\n"
            "sys.stderr.write(repr(os.environ.get('HERMES_KANBAN_TASK')))\n",
            encoding="utf-8",
        )
        spec = shell_hooks.ShellHookSpec(
            event="pre_tool_call",
            command=f"{sys.executable} {probe}",
            timeout=30,
        )
        assert _run(spec).get("stderr", "").strip() == "'parent-task-123'"
        with delegated_child_context("test-child"):
            assert _run(spec).get("stderr", "").strip() == "None"
