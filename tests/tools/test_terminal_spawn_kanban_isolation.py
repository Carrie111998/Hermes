"""Regression tests: terminal-spawned children of kanban workers must not
inherit the dispatcher's board identity.

Repro (2026-08-19, t_ed6d4271): a ``hermes chat -q`` child spawned from a
kanban worker via the terminal tool inherited ``HERMES_KANBAN_TASK`` (+
workspace/run-id vars) and the kanban lifecycle toolset. The one-shot child
decided it was working on the parent's card and called ``kanban_complete`` on
it — marking the parent done mid-run, wiping its scratch workspace while the
real worker was still writing artifacts, and losing the real completion
summary.

The fix has three layers:
  1. Every subprocess env a worker spawns is scrubbed of all
     ``HERMES_KANBAN_*`` vars and stamped ``HERMES_DELEGATED_CHILD_CONTEXT=1``
     (``tools/environments/local.py::_scrub_delegated_child_kanban_env``).
  2. The child-side identity predicate
     (``is_dispatcher_owned_worker_context``) fails closed for any process
     carrying that marker, so even a partial env leak cannot re-grant the
     kanban toolset or an implicit task id.
  3. ``kanban_tools._enforce_worker_task_ownership`` refuses lifecycle
     mutation when ``HERMES_KANBAN_TASK`` is present but the process is not
     the dispatcher-owned worker (defense-in-depth for orchestrator-profile
     children that see the tools but must not touch a leaked task id).
"""
from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

import pytest

# The subprocess-boundary tests below spawn ``sys.executable -c`` with a tmp
# cwd. Without an explicit PYTHONPATH the child resolves ``hermes_cli`` /
# ``agent`` through whatever install is on sys.path. Pin the repo root so the
# child always imports the tree being tested.
_REPO_ROOT = Path(__file__).resolve().parents[2]

_WORKER_ENV_KEYS = {
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_CLAIM_LOCK",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_DB",
    # Behaviour knobs must be scrubbed too — a child must not reconstruct
    # worker behaviour from a partial leak.
    "HERMES_KANBAN_BRANCH",
    "HERMES_KANBAN_GOAL_MODE",
    "HERMES_KANBAN_GOAL_MAX_TURNS",
}


def _python_with_repo_path(code: str) -> str:
    return (
        f"PYTHONPATH={shlex.quote(str(_REPO_ROOT))} "
        f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
    )


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    """Simulate a dispatcher-owned worker's os.environ."""
    home = tmp_path / ".hermes"
    home.mkdir()
    workspace = tmp_path / "parent-workspace"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "worker-profile")
    for key in _WORKER_ENV_KEYS:
        monkeypatch.setenv(key, "x" if key != "HERMES_KANBAN_WORKSPACE" else str(workspace))
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    monkeypatch.setenv("TERMINAL_CWD", str(workspace))
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "kanban")
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    return home, workspace


# ---------------------------------------------------------------------------
# Layer 1: subprocess env scrubbing
# ---------------------------------------------------------------------------

def test_worker_subprocess_env_strips_all_kanban_identity(worker_env):
    """A terminal subprocess of a worker must not inherit any HERMES_KANBAN_*."""
    from tools.environments.local import _sanitize_subprocess_env

    child_env = _sanitize_subprocess_env(dict(os.environ), {})

    leaked = {k: v for k, v in child_env.items() if k.startswith("HERMES_KANBAN_")}
    assert leaked == {}, f"worker subprocess leaked kanban identity: {leaked}"
    assert child_env.get("HERMES_DELEGATED_CHILD_CONTEXT") == "1"
    # Worker session tag must not leak either.
    assert child_env.get("HERMES_SESSION_SOURCE") != "kanban"
    # Non-kanban env must survive the scrub untouched.
    assert child_env.get("HERMES_HOME") == os.environ["HERMES_HOME"]
    assert child_env.get("HERMES_PROFILE") == "worker-profile"


def test_worker_subprocess_env_scrubs_goal_mode_and_branch(worker_env):
    """Behaviour knobs are not identity, but must not leak either."""
    from tools.environments.local import _sanitize_subprocess_env

    child_env = _sanitize_subprocess_env(dict(os.environ), {})
    for key in ("HERMES_KANBAN_GOAL_MODE", "HERMES_KANBAN_GOAL_MAX_TURNS", "HERMES_KANBAN_BRANCH"):
        assert key not in child_env


def test_plain_session_subprocess_env_is_not_scratched(monkeypatch, tmp_path):
    """A normal (non-worker) session keeps its env and gets no child marker."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for key in list(_WORKER_ENV_KEYS):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)

    from tools.environments.local import _sanitize_subprocess_env

    child_env = _sanitize_subprocess_env(dict(os.environ), {})
    assert "HERMES_DELEGATED_CHILD_CONTEXT" not in child_env
    assert not any(k.startswith("HERMES_KANBAN_") for k in child_env)


def test_scrub_kanban_env_strips_prefix_wide(monkeypatch):
    """scrub_kanban_env drops every HERMES_KANBAN_* var, known or not."""
    from agent.delegation_context import scrub_kanban_env

    env = {"HERMES_KANBAN_TASK": "t_x", "HERMES_KANBAN_FUTURE_VAR": "1", "HOME": "/h"}
    cleaned = scrub_kanban_env(env)
    assert "HERMES_KANBAN_TASK" not in cleaned
    assert "HERMES_KANBAN_FUTURE_VAR" not in cleaned
    assert cleaned["HOME"] == "/h"
    assert cleaned["HERMES_DELEGATED_CHILD_CONTEXT"] == "1"
    # Original dict untouched.
    assert env["HERMES_KANBAN_TASK"] == "t_x"


# ---------------------------------------------------------------------------
# Layer 2: child-side identity predicate fails closed on the marker
# ---------------------------------------------------------------------------

def test_dispatcher_owned_predicate_false_when_marker_present(monkeypatch):
    from agent.delegation_context import is_dispatcher_owned_worker_context

    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    assert is_dispatcher_owned_worker_context() is False


def test_dispatcher_owned_predicate_true_for_real_worker(worker_env):
    from agent.delegation_context import is_dispatcher_owned_worker_context

    assert is_dispatcher_owned_worker_context() is True


# ---------------------------------------------------------------------------
# Layer 3: tool-level ownership enforcement
# ---------------------------------------------------------------------------

def test_leaked_env_child_cannot_mutate_even_its_inherited_task(monkeypatch, tmp_path):
    """A process with HERMES_KANBAN_TASK but no run ownership is refused."""
    import tools.kanban_tools  # noqa: F401 - registers tools
    from tools.kanban_tools import _enforce_worker_task_ownership

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_parent")
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    err = _enforce_worker_task_ownership("t_parent")
    assert err is not None
    assert "not the dispatcher-owned worker" in err
    # Foreign ids are refused too (same non-ownership path).
    err_foreign = _enforce_worker_task_ownership("t_sibling")
    assert err_foreign is not None


def test_real_worker_can_close_own_task_but_not_foreign(worker_env):
    import tools.kanban_tools  # noqa: F401
    from tools.kanban_tools import _enforce_worker_task_ownership

    assert _enforce_worker_task_ownership("x") is None  # env key is "x"
    err = _enforce_worker_task_ownership("t_other")
    assert err is not None
    assert "refusing to mutate t_other" in err


def test_handle_complete_refused_for_marker_child(monkeypatch, tmp_path):
    """End-to-end tool path: a marker child calling kanban_complete gets a
    refusal at whichever gate fires first — the implicit task-id default is
    refused, and an explicit inherited id is refused by ownership check."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_parent")
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")

    import tools.kanban_tools  # noqa: F401
    from tools.kanban_tools import _handle_complete

    # No task_id: the env default must NOT be trusted (child is not the run
    # owner), so the tool refuses before touching the board.
    raw = _handle_complete({"summary": "rogue child completion"})
    assert raw is not None
    assert "task_id is required" in raw

    # Explicit inherited task id: ownership gate refuses the mutation.
    raw_explicit = _handle_complete({"task_id": "t_parent", "summary": "rogue"})
    assert raw_explicit is not None
    assert "not the dispatcher-owned worker" in raw_explicit


# ---------------------------------------------------------------------------
# Real subprocess boundary
# ---------------------------------------------------------------------------

def test_spawned_child_process_sees_no_kanban_env(worker_env, tmp_path):
    """A REAL child process spawned with the scrubbed env has zero kanban
    identity and its kanban CLI mutations are refused (marker guard)."""
    from tools.environments.local import _sanitize_subprocess_env

    child_env = _sanitize_subprocess_env(dict(os.environ), {})
    probe = (
        "import os; "
        "kanban=[k for k in os.environ if k.startswith('HERMES_KANBAN_')]; "
        "marker=os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'); "
        "print('KANBAN_KEYS=' + repr(kanban)); "
        "print('MARKER=' + repr(marker))"
    )
    proc = __import__("subprocess").run(
        [sys.executable, "-c", probe],
        env=child_env,
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "KANBAN_KEYS=[]" in proc.stdout
    assert "MARKER='1'" in proc.stdout


def test_spawned_child_kanban_cli_mutations_refused(worker_env, tmp_path):
    """A spawned child (marker set) hitting the kanban CLI is refused by the
    CLI's own delegate-child guard, matching the in-process tool guard."""
    from tools.environments.local import _sanitize_subprocess_env

    child_env = _sanitize_subprocess_env(dict(os.environ), {})
    code = (
        "from hermes_cli import kanban; "
        "import argparse; "
        "p=argparse.ArgumentParser(); "
        "sub=p.add_subparsers(dest='cmd'); "
        "kanban.build_parser(sub); "
        "args=p.parse_args(['kanban','boards','rm','victim','--delete']); "
        "raise SystemExit(kanban.kanban_command(args))"
    )
    proc = __import__("subprocess").run(
        f"{sys.executable} -c {shlex.quote(code)}",
        shell=True,
        env=child_env,
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=60,
    )
    assert proc.returncode == 1, proc.stdout
    combined = proc.stdout + proc.stderr
    assert "delegate_task child contexts cannot mutate Kanban tasks" in combined
