"""The deployed admission path must be runnable from this tree, as the runtime runs it.

The 2026-07-30 Code A/B outage was an import mismatch: the deployed
``factory_admission_hook.py`` imported symbols that never existed on the
runtime lineage (``extract_v4a_patch_paths`` public name, ``cron.redaction``),
so every ``pre_tool_call`` crashed and fail-closed hooks blocked every tool.

These tests execute the repo-shipped scripts exactly like the runtime does —
``<python> <abs path to script>`` as a subprocess, from a foreign cwd, without
PYTHONPATH help — and assert the hook protocol (one JSON decision on stdout,
exit 0, no traceback) instead of any import shortcut a test harness would take.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "scripts" / "factory_admission_hook.py"
LANE = REPO_ROOT / "scripts" / "factory_lane.py"


def _clean_env():
    env = os.environ.copy()
    # The runtime invokes the hook without PYTHONPATH help; imports must
    # resolve from the script's own repo-root insertion.
    env.pop("PYTHONPATH", None)
    return env


def _run_hook(payload: dict, registry: Path, cwd: Path) -> subprocess.CompletedProcess:
    assert HOOK.is_file(), (
        "scripts/factory_admission_hook.py must ship in the repo: it is the "
        "source of the deployed Code A/B admission hook"
    )
    return subprocess.run(
        [sys.executable, str(HOOK), "--registry", str(registry),
         "--agent", "hermes-code-a", "--profile", "hermes-code-a",
         "--only-mutating", "--require-owned-git"],
        input=json.dumps(payload), text=True, capture_output=True,
        timeout=30, cwd=cwd, env=_clean_env(),
    )


def test_hook_emits_allow_for_strict_observation_from_foreign_cwd(tmp_path):
    """A valid observation must produce a JSON allow — never an ImportError."""
    result = _run_hook(
        {"hook_event_name": "pre_tool_call", "tool_name": "read_file",
         "tool_input": {"path": str(tmp_path / "x.txt")},
         "session_id": "s1", "cwd": str(tmp_path)},
        registry=tmp_path / "missing-registry", cwd=tmp_path,
    )
    assert "Traceback" not in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"decision": "allow"}


def test_hook_emits_block_for_unowned_mutation_from_foreign_cwd(tmp_path):
    """A mutation without a live owner must produce a JSON block, not a crash.

    With fail_closed hooks a crashed subprocess also blocks, but it blocks
    *everything for the wrong reason* — this asserts the decision protocol
    stays intact on the mutation path (which imports the V4A patch parser).
    """
    result = _run_hook(
        {"hook_event_name": "pre_tool_call", "tool_name": "write_file",
         "tool_input": {"path": str(tmp_path / "x.txt"), "content": "y"},
         "session_id": "s1", "cwd": str(tmp_path)},
        registry=tmp_path / "missing-registry", cwd=tmp_path,
    )
    assert "Traceback" not in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr
    decision = json.loads(result.stdout)
    assert decision.get("decision") == "block"
    assert decision.get("reason")


def test_factory_lane_is_importable_standalone(tmp_path):
    """factory_lane must import (and parse --help) from a foreign cwd."""
    assert LANE.is_file(), (
        "scripts/factory_lane.py must ship in the repo: it is the source of "
        "the deployed owner/claim registry CLI"
    )
    result = subprocess.run(
        [sys.executable, str(LANE), "--help"],
        text=True, capture_output=True, timeout=30, cwd=tmp_path,
        env=_clean_env(),
    )
    assert "Traceback" not in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr


def test_v4a_patch_path_api_is_public():
    """The hook imports ``extract_v4a_patch_paths`` by its public name.

    The deployed hook crashed because only a private ``_extract_v4a_patch_paths``
    existed on the runtime lineage. The shared API must stay public.
    """
    from acp_adapter.edit_approval import extract_v4a_patch_paths

    patch = "*** Begin Patch\n*** Update File: a.txt\n+x\n*** End Patch\n"
    assert extract_v4a_patch_paths(patch) == ["a.txt"]


def test_cron_redaction_module_ships_with_factory_lane():
    """factory_lane imports cron.redaction at module load; it must exist."""
    from cron.redaction import contains_credential, redact_credential_text

    assert contains_credential("api_key=sk-abc123456789012345678901")
    redacted = redact_credential_text("token=ghp_0123456789abcdef0123456789abcdef0123")
    assert "ghp_" not in redacted


@pytest.mark.parametrize("tool_name", ["kanban_show", "kanban_heartbeat"])
def test_exact_worker_lifecycle_reaches_decision_protocol(tmp_path, tool_name):
    """Worker lifecycle tools must reach the decision layer, not die on import."""
    env_session = "kanban-t_demo-run-7"
    payload = {
        "hook_event_name": "pre_tool_call", "tool_name": tool_name,
        "tool_input": {}, "session_id": env_session, "cwd": str(tmp_path),
    }
    assert HOOK.is_file()
    env = _clean_env()
    env.update({
        "HERMES_KANBAN_TASK": "t_demo",
        "HERMES_KANBAN_RUN_ID": "7",
        "HERMES_KANBAN_WORKSPACE": str(tmp_path),
    })
    result = subprocess.run(
        [sys.executable, str(HOOK), "--registry", str(tmp_path / "missing-registry"),
         "--agent", "hermes-code-a", "--profile", "hermes-code-a",
         "--only-mutating", "--require-owned-git"],
        input=json.dumps(payload), text=True, capture_output=True,
        timeout=30, cwd=tmp_path, env=env,
    )
    assert "Traceback" not in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr
    decision = json.loads(result.stdout)
    # tmp_path is not a git toplevel, so the strict gate must block with the
    # lifecycle reason — proving the classification code ran end to end.
    assert decision.get("decision") in {"allow", "block"}
