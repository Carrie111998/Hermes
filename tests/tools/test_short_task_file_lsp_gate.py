"""LSP processes must not outlive a managed Phase-1 file edit."""

from __future__ import annotations

import json

import pytest

from tools.environments.local import LocalEnvironment
from tools.file_operations import (
    ExecuteResult,
    LintResult,
    ShellFileOperations,
)


def _policy_snapshot(*, enabled: bool) -> str:
    return json.dumps(
        {
            "schema": 2,
            "enabled": enabled,
            "soft_iteration_limit": 36,
            "max_handoffs": 8,
            "max_iterations": 90,
            "failure_limit": 2,
            "validation_error": None,
        }
    )


def _local_ops(tmp_path, monkeypatch):
    # ``isinstance(env, LocalEnvironment)`` is the production LSP gate. Avoid
    # LocalEnvironment.__init__ here because this unit only needs a controlled
    # synchronous file backend, not a real shell session.
    env = LocalEnvironment.__new__(LocalEnvironment)
    env.cwd = str(tmp_path)
    ops = ShellFileOperations(env, cwd=str(tmp_path))
    state = {"content": "old\n"}

    def fake_exec(command, cwd=None, timeout=None, stdin_data=None):
        if command.startswith("cat "):
            return ExecuteResult(stdout=state["content"], exit_code=0)
        if "wc -c" in command:
            return ExecuteResult(
                stdout=str(len(state["content"].encode("utf-8"))),
                exit_code=0,
            )
        return ExecuteResult(stdout="", exit_code=0)

    def fake_atomic_write(_path, content):
        state["content"] = content
        return ExecuteResult(stdout="", exit_code=0)

    monkeypatch.setattr(ops, "_exec", fake_exec)
    monkeypatch.setattr(ops, "_atomic_write", fake_atomic_write)
    monkeypatch.setattr(ops, "_detect_file_line_ending", lambda *_args: None)
    monkeypatch.setattr(ops, "_file_has_bom", lambda *_args: False)
    monkeypatch.setattr(
        ops,
        "_check_lint_delta",
        lambda *_args, **_kwargs: LintResult(skipped=True),
    )
    return ops, state


@pytest.mark.parametrize(
    "snapshot",
    [_policy_snapshot(enabled=True), "{malformed"],
)
def test_managed_write_and_patch_never_touch_lsp_service(
    tmp_path, monkeypatch, snapshot
):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_short")
    monkeypatch.setenv("HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY", snapshot)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    ops, state = _local_ops(tmp_path, monkeypatch)

    def forbidden_get_service():
        pytest.fail("managed write/patch attempted to start or query LSP")

    monkeypatch.setattr("agent.lsp.get_service", forbidden_get_service)

    write = ops.write_file(str(tmp_path / "sample.txt"), "written\n")
    patch = ops.patch_replace(
        str(tmp_path / "sample.txt"), "written", "patched"
    )

    assert ops._lsp_local_only() is False
    assert write.error is None
    assert patch.error is None
    assert state["content"] == "patched\n"


@pytest.mark.parametrize(
    "worker,snapshot",
    [
        (False, None),
        (True, _policy_snapshot(enabled=False)),
    ],
)
def test_ordinary_goal_or_review_file_edits_keep_lsp_behavior(
    tmp_path, monkeypatch, worker, snapshot
):
    if worker:
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_review")
    else:
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    if snapshot is None:
        monkeypatch.delenv(
            "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY", raising=False
        )
    else:
        monkeypatch.setenv("HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY", snapshot)

    ops, state = _local_ops(tmp_path, monkeypatch)
    calls = {"get": 0, "snapshot": 0, "diagnostics": 0}

    class FakeService:
        def enabled_for(self, _path):
            return True

        def snapshot_baseline(self, _path):
            calls["snapshot"] += 1

        def get_diagnostics_sync(self, _path, **_kwargs):
            calls["diagnostics"] += 1
            return []

    service = FakeService()

    def get_service():
        calls["get"] += 1
        return service

    monkeypatch.setattr("agent.lsp.get_service", get_service)

    write = ops.write_file(str(tmp_path / "sample.txt"), "written\n")
    patch = ops.patch_replace(
        str(tmp_path / "sample.txt"), "written", "patched"
    )

    assert ops._lsp_local_only() is True
    assert write.error is None
    assert patch.error is None
    assert state["content"] == "patched\n"
    assert calls["get"] >= 4
    assert calls["snapshot"] >= 2
    assert calls["diagnostics"] >= 2
