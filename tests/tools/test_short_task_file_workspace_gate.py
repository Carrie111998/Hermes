"""Behavior contracts for the Phase-1 managed-worker file boundary."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import tools.file_tools as ft
import tools.terminal_tool as terminal_tool


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


@pytest.fixture
def managed_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "inside.txt").write_text("inside\n")
    (outside / "outside.txt").write_text("outside\n")
    escape_link = workspace / "escape-link"
    escape_link.symlink_to(outside, target_is_directory=True)

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_short")
    monkeypatch.setenv(
        "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY",
        _policy_snapshot(enabled=True),
    )
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    monkeypatch.setenv("TERMINAL_CWD", str(workspace))
    monkeypatch.setattr(ft, "_uses_container_paths", lambda _task_id: False)
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    return workspace, outside, escape_link


def _error(payload: str) -> str:
    return str(json.loads(payload).get("error") or "")


def test_all_file_handlers_reject_direct_workspace_escape(
    managed_workspace, monkeypatch
):
    workspace, outside, _escape_link = managed_workspace

    def forbidden_backend(*_args, **_kwargs):
        pytest.fail("out-of-workspace request reached the file backend")

    monkeypatch.setattr(ft, "_get_file_ops", forbidden_backend)

    attempts = [
        ft.read_file_tool(str(outside / "outside.txt"), task_id="session-short"),
        ft.write_file_tool("../outside/new.txt", "x", task_id="session-short"),
        ft.patch_tool(
            mode="replace",
            path=str(outside / "outside.txt"),
            old_string="outside",
            new_string="changed",
            task_id="session-short",
        ),
        ft.search_tool(
            "outside", path=str(outside), task_id="session-short"
        ),
    ]

    for result in attempts:
        assert "limited to the assigned workspace" in _error(result)
    assert (outside / "outside.txt").read_text() == "outside\n"
    assert not (outside / "new.txt").exists()


@pytest.mark.parametrize(
    "operation",
    ["read", "write", "patch", "search"],
)
def test_all_file_handlers_reject_symlink_escape(
    managed_workspace, monkeypatch, operation
):
    _workspace, outside, escape_link = managed_workspace

    def forbidden_backend(*_args, **_kwargs):
        pytest.fail("symlink escape reached the file backend")

    monkeypatch.setattr(ft, "_get_file_ops", forbidden_backend)
    escaped_file = escape_link / "outside.txt"
    if operation == "read":
        result = ft.read_file_tool(str(escaped_file), task_id="session-short")
    elif operation == "write":
        result = ft.write_file_tool(
            str(escape_link / "new.txt"), "x", task_id="session-short"
        )
    elif operation == "patch":
        result = ft.patch_tool(
            mode="replace",
            path=str(escaped_file),
            old_string="outside",
            new_string="changed",
            task_id="session-short",
        )
    else:
        result = ft.search_tool(
            "outside", path=str(escape_link), task_id="session-short"
        )

    assert "limited to the assigned workspace" in _error(result)
    assert (outside / "outside.txt").read_text() == "outside\n"
    assert not (outside / "new.txt").exists()


def test_nonexistent_target_parent_is_resolved_before_allowing_write(
    managed_workspace,
):
    workspace, outside, _escape_link = managed_workspace

    inside_new = workspace / "not-yet-created" / "child" / "file.txt"
    escaped_new = (
        workspace
        / "not-yet-created"
        / "child"
        / ".."
        / ".."
        / ".."
        / "outside"
        / "new.txt"
    )

    assert ft._short_task_workspace_path_error(
        [str(inside_new)], "session-short"
    ) is None
    assert "limited to the assigned workspace" in str(
        ft._short_task_workspace_path_error(
            [str(escaped_new)], "session-short"
        )
    )
    assert not (outside / "new.txt").exists()


def test_v4a_patch_and_search_globs_cannot_reinterpret_outside_paths(
    managed_workspace, monkeypatch
):
    workspace, outside, escape_link = managed_workspace

    def forbidden_backend(*_args, **_kwargs):
        pytest.fail("escaped patch/search reached the file backend")

    monkeypatch.setattr(ft, "_get_file_ops", forbidden_backend)
    patch_result = ft.patch_tool(
        mode="patch",
        patch=(
            "*** Begin Patch\n"
            f"*** Update File: {escape_link / 'outside.txt'}\n"
            "@@\n"
            "-outside\n"
            "+changed\n"
            "*** End Patch\n"
        ),
        task_id="session-short",
    )
    content_glob_result = ft.search_tool(
        "needle",
        path=str(workspace),
        file_glob="../outside/*.txt",
        task_id="session-short",
    )
    file_glob_result = ft.search_tool(
        "/outside/*.txt",
        target="files",
        path=str(workspace),
        task_id="session-short",
    )

    assert "limited to the assigned workspace" in _error(patch_result)
    assert "must remain relative" in _error(content_glob_result)
    assert "must remain relative" in _error(file_glob_result)
    assert (outside / "outside.txt").read_text() == "outside\n"


def test_normal_workspace_paths_reach_all_file_backends(
    managed_workspace, monkeypatch
):
    workspace, _outside, _escape_link = managed_workspace
    calls = []

    class FakeResult:
        def __init__(self, kind):
            self.kind = kind
            self.content = " 1|inside"
            self.matches = []

        def to_dict(self, **_kwargs):
            if self.kind == "read":
                return {
                    "content": self.content,
                    "total_lines": 1,
                    "file_size": 7,
                    "truncated": False,
                }
            if self.kind == "search":
                return {"matches": [], "total_count": 0, "truncated": False}
            return {"status": "ok"}

    class FakeOps:
        def read_file(self, path, offset, limit):
            calls.append(("read", path, offset, limit))
            return FakeResult("read")

        def write_file(self, path, content):
            calls.append(("write", path, content))
            return FakeResult("write")

        def patch_replace(self, path, old, new, replace_all):
            calls.append(("patch", path, old, new, replace_all))
            return FakeResult("patch")

        def search(self, **kwargs):
            calls.append(("search", kwargs))
            return FakeResult("search")

    monkeypatch.setattr(ft, "_get_file_ops", lambda _task_id: FakeOps())

    read = json.loads(
        ft.read_file_tool("inside.txt", task_id="session-short")
    )
    write = json.loads(
        ft.write_file_tool(
            "new/inside.txt", "new\n", task_id="session-short"
        )
    )
    patch = json.loads(
        ft.patch_tool(
            mode="replace",
            path="inside.txt",
            old_string="inside",
            new_string="changed",
            task_id="session-short",
        )
    )
    search = json.loads(
        ft.search_tool(
            "inside",
            path=".",
            file_glob="src/**/*.txt",
            task_id="session-short",
        )
    )

    assert "error" not in read
    assert "error" not in write
    assert "error" not in patch
    assert "error" not in search
    assert {call[0] for call in calls} == {"read", "write", "patch", "search"}


@pytest.mark.parametrize(
    "snapshot",
    [_policy_snapshot(enabled=False), None],
)
def test_disabled_or_ordinary_worker_keeps_historical_outside_path_behavior(
    tmp_path, monkeypatch, snapshot
):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    monkeypatch.setenv("TERMINAL_CWD", str(workspace))
    monkeypatch.setattr(ft, "_uses_container_paths", lambda _task_id: False)
    if snapshot is None:
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        monkeypatch.delenv(
            "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY", raising=False
        )
    else:
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_review")
        monkeypatch.setenv("HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY", snapshot)

    reached = []
    result = SimpleNamespace(
        to_dict=lambda **_kwargs: {"status": "ok"}
    )
    monkeypatch.setattr(
        ft,
        "_get_file_ops",
        lambda _task_id: SimpleNamespace(
            write_file=lambda path, content: reached.append((path, content))
            or result
        ),
    )

    payload = json.loads(
        ft.write_file_tool(
            str(outside / "legacy.txt"), "ok", task_id="session-review"
        )
    )

    assert payload["status"] == "ok"
    assert reached == [(str(outside / "legacy.txt"), "ok")]


def test_malformed_worker_policy_and_missing_workspace_fail_closed(
    tmp_path, monkeypatch
):
    outside = tmp_path / "outside.txt"
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_short")
    monkeypatch.setenv(
        "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY", "{malformed"
    )
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACE", raising=False)

    error = ft._short_task_workspace_path_error(
        [str(outside)], "session-short"
    )

    assert "workspace is missing" in str(error)
