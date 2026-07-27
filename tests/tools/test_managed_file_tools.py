"""Pure-Python file boundary for verified managed short-task workers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tools import managed_file_tools as managed


def _managed_env(monkeypatch, workspace: Path, *, lane: str = "implementation"):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_managed_files")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "101")
    monkeypatch.setenv("HERMES_KANBAN_MANAGED_LANE", lane)
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_MODE", "1" if lane == "review" else "0")
    monkeypatch.setenv("HERMES_KANBAN_MANAGED_BOOTSTRAP", "1")
    monkeypatch.setenv("HERMES_KANBAN_MANAGED_BOOTSTRAP_VERIFIED", "1")
    monkeypatch.delenv("HERMES_KANBAN_MANAGED_BOOTSTRAP_ERROR", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))


def _payload(result: str) -> dict:
    return json.loads(result)


def test_four_managed_file_tools_work_without_terminal_backend(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _managed_env(monkeypatch, workspace)

    def forbidden_popen(*_args, **_kwargs):
        pytest.fail("managed file operation attempted to launch a process")

    monkeypatch.setattr(subprocess, "Popen", forbidden_popen)

    written = _payload(managed.write_file_tool("notes/item.txt", "alpha\n"))
    read = _payload(managed.read_file_tool("notes/item.txt"))
    patched = _payload(
        managed.patch_tool(
            path="notes/item.txt",
            old_string="alpha",
            new_string="beta",
        )
    )
    searched = _payload(
        managed.search_files_tool(
            "beta", path="notes", file_glob="*.txt"
        )
    )

    assert written["success"] is True
    assert "alpha" in read["content"]
    assert patched["success"] is True
    assert searched["total_count"] == 1
    assert (workspace / "notes" / "item.txt").read_text() == "beta\n"
    assert "tools.terminal_tool" not in sys.modules
    assert "tools.file_tools" not in sys.modules
    assert "tools.file_operations" not in sys.modules


@pytest.mark.parametrize(
    "call",
    [
        lambda outside: managed.read_file_tool(str(outside / "outside.txt")),
        lambda outside: managed.write_file_tool("../outside/new.txt", "x"),
        lambda outside: managed.patch_tool(
            path=str(outside / "outside.txt"),
            old_string="outside",
            new_string="changed",
        ),
        lambda outside: managed.search_files_tool("outside", path=str(outside)),
    ],
)
def test_all_managed_file_tools_reject_absolute_or_parent_escape(
    monkeypatch, tmp_path, call
):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "outside.txt").write_text("outside\n")
    _managed_env(monkeypatch, workspace)

    result = _payload(call(outside))

    assert result.get("error")
    assert (outside / "outside.txt").read_text() == "outside\n"
    assert not (outside / "new.txt").exists()


@pytest.mark.parametrize("operation", ["read", "write", "patch", "search"])
def test_all_managed_file_tools_reject_symlink_paths(
    monkeypatch, tmp_path, operation
):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "outside.txt").write_text("outside\n")
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    _managed_env(monkeypatch, workspace)

    if operation == "read":
        result = managed.read_file_tool("escape/outside.txt")
    elif operation == "write":
        result = managed.write_file_tool("escape/new.txt", "x")
    elif operation == "patch":
        result = managed.patch_tool(
            path="escape/outside.txt",
            old_string="outside",
            new_string="changed",
        )
    else:
        result = managed.search_files_tool("outside", path="escape")

    assert _payload(result).get("error")
    assert (outside / "outside.txt").read_text() == "outside\n"
    assert not (outside / "new.txt").exists()


def test_managed_read_refuses_fifo_hardlink_large_file_and_secrets(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _managed_env(monkeypatch, workspace)
    fifo = workspace / "pipe"
    os.mkfifo(fifo)
    original = workspace / "original.txt"
    original.write_text("linked\n")
    hardlink = workspace / "hardlink.txt"
    os.link(original, hardlink)
    large = workspace / "large.txt"
    large.write_bytes(b"x" * (managed._MAX_READ_FILE_BYTES + 1))
    secret = workspace / ".ENV.Production"
    secret.write_text("TOKEN=secret\n")

    assert "regular files" in _payload(managed.read_file_tool("pipe"))["error"]
    assert "Hard-linked" in _payload(
        managed.read_file_tool("hardlink.txt")
    )["error"]
    assert "read limit" in _payload(managed.read_file_tool("large.txt"))["error"]
    assert "secret-bearing" in _payload(
        managed.read_file_tool(".ENV.Production")
    )["error"]


def test_review_reads_record_relative_evidence_but_cannot_edit(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "candidate.txt").write_text("candidate\n")
    _managed_env(monkeypatch, workspace, lane="review")
    managed._REVIEW_READ_EVIDENCE.clear()

    read = _payload(managed.read_file_tool("candidate.txt"))
    write = _payload(managed.write_file_tool("new.txt", "no"))
    patch = _payload(
        managed.patch_tool(
            path="candidate.txt",
            old_string="candidate",
            new_string="changed",
        )
    )

    assert "candidate" in read["content"]
    evidence = managed.managed_review_read_evidence()
    assert evidence == [
        {
            "path": "candidate.txt",
            "sha256": "1e81270f1a47dce22a2e4985250c74b2e3374443734f1492b03ea2cd2af4ec48",
            "size": 10,
        }
    ]
    assert "只能读取文件" in write["error"]
    assert "只能读取文件" in patch["error"]
    assert not (workspace / "new.txt").exists()
    assert (workspace / "candidate.txt").read_text() == "candidate\n"

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_other_review")
    assert managed.managed_review_read_evidence() == []
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_managed_files")
    assert managed.managed_review_read_evidence() == evidence
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "102")
    assert managed.managed_review_read_evidence() == []
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "101")
    assert managed.managed_review_read_evidence() == evidence


def test_partial_review_read_does_not_form_completion_evidence(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    candidate = workspace / "candidate.txt"
    candidate.write_text("".join(f"line-{number}\n" for number in range(501)))
    _managed_env(monkeypatch, workspace, lane="review")
    managed._REVIEW_READ_EVIDENCE.clear()

    partial = _payload(managed.read_file_tool("candidate.txt"))
    assert partial["truncated"] is True
    assert managed.managed_review_read_evidence() == []

    complete = _payload(
        managed.read_file_tool("candidate.txt", offset=1, limit=1000)
    )
    assert complete["truncated"] is False
    assert managed.managed_review_read_evidence()[0]["path"] == "candidate.txt"


@pytest.mark.parametrize(
    "patch_text, message",
    [
        (
            "*** Begin Patch\n*** Delete File: candidate.txt\n*** End Patch",
            "delete or move",
        ),
        (
            "*** Begin Patch\n"
            "*** Move File: candidate.txt -> moved.txt\n"
            "*** End Patch",
            "delete or move",
        ),
        (
            "*** Begin Patch\n"
            "*** Add File: first.txt\n+first\n"
            "*** Add File: second.txt\n+second\n"
            "*** End Patch",
            "one file",
        ),
    ],
)
def test_managed_v4a_rejects_delete_move_and_multi_file_changes(
    monkeypatch, tmp_path, patch_text, message
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "candidate.txt").write_text("candidate\n")
    _managed_env(monkeypatch, workspace)

    result = _payload(managed.patch_tool(mode="patch", patch=patch_text))

    assert message.lower() in result["error"].lower()
    assert (workspace / "candidate.txt").read_text() == "candidate\n"
    assert not (workspace / "moved.txt").exists()
    assert not (workspace / "first.txt").exists()
    assert not (workspace / "second.txt").exists()


def test_fresh_process_dispatches_four_tools_with_zero_popen_calls(tmp_path):
    """Exercise the registered production handlers under an import canary."""
    repo_root = Path(__file__).resolve().parents[2]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "HERMES_KANBAN_TASK": "t_fresh_file_canary",
            "HERMES_KANBAN_MANAGED_LANE": "implementation",
            "HERMES_KANBAN_REVIEW_MODE": "0",
            "HERMES_KANBAN_MANAGED_BOOTSTRAP": "1",
            "HERMES_KANBAN_MANAGED_BOOTSTRAP_VERIFIED": "1",
            "HERMES_KANBAN_WORKSPACE": str(workspace),
            "PYTHONPYCACHEPREFIX": "/private/tmp/hermes-short-task-pycache",
        }
    )
    env.pop("HERMES_KANBAN_MANAGED_BOOTSTRAP_ERROR", None)
    script = f"""
import json, subprocess, sys
sys.path.insert(0, {str(repo_root)!r})
calls = []
def blocked(*args, **kwargs):
    calls.append(args)
    raise AssertionError('Popen was reached')
subprocess.Popen = blocked
import model_tools
from tools.registry import registry
results = [
    registry.dispatch('write_file', {{'path': 'candidate.txt', 'content': 'alpha\\n'}}),
    registry.dispatch('read_file', {{'path': 'candidate.txt'}}),
    registry.dispatch('patch', {{'path': 'candidate.txt', 'old_string': 'alpha', 'new_string': 'beta'}}),
    registry.dispatch('search_files', {{'pattern': 'beta', 'path': '.'}}),
    registry.dispatch('read_file', {{'path': '../outside.txt'}}),
]
print(json.dumps({{
    'results': [json.loads(item) for item in results],
    'popen_calls': len(calls),
    'modules': sorted(sys.modules),
}}))
"""

    completed = subprocess.run(
        [sys.executable, "-B", "-I", "-s", "-E", "-c", script],
        cwd=workspace,
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    evidence = json.loads(completed.stdout.strip().splitlines()[-1])

    assert evidence["popen_calls"] == 0
    assert all("error" not in item for item in evidence["results"][:4])
    assert evidence["results"][4].get("error")
    assert "tools.managed_file_tools" in evidence["modules"]
    assert "tools.terminal_tool" not in evidence["modules"]
    assert "tools.file_tools" not in evidence["modules"]
    assert "tools.file_operations" not in evidence["modules"]
