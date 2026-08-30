"""Behavioral coverage for file-tool checkpoint path resolution."""

from types import SimpleNamespace

from agent.tool_executor import _ensure_file_checkpoint
from tools.checkpoint_manager import CheckpointManager, _run_git, _store_path


def test_relative_file_checkpoint_uses_task_workspace(tmp_path, monkeypatch):
    """Checkpoint lookup must use the same cwd as a relative file mutation."""
    process_cwd = tmp_path / "opt" / "hermes"
    workspace_cwd = tmp_path / "opt" / "data" / "workspace"
    process_cwd.mkdir(parents=True)
    workspace_cwd.mkdir(parents=True)

    # Both directories contain content so checkpointing the wrong one would
    # still succeed and remain observable as the regression did in Docker.
    (process_cwd / "pyproject.toml").write_text("[project]\nname = 'hermes'\n")
    (workspace_cwd / "pyproject.toml").write_text("[project]\nname = 'workspace'\n")
    (workspace_cwd / "existing.txt").write_text("before\n")

    monkeypatch.chdir(process_cwd)
    monkeypatch.setenv("TERMINAL_CWD", str(workspace_cwd))
    monkeypatch.setattr(
        "tools.checkpoint_manager.CHECKPOINT_BASE",
        tmp_path / "checkpoints",
    )

    manager = CheckpointManager(enabled=True)
    agent = SimpleNamespace(_checkpoint_mgr=manager)

    _ensure_file_checkpoint(
        agent,
        "write_file",
        {"path": "test_permissions2.txt"},
        "gateway-session",
    )

    assert manager.list_checkpoints(str(workspace_cwd))
    assert manager.list_checkpoints(str(process_cwd)) == []


def test_v4a_patch_checkpoint_preserves_pre_edit_content(tmp_path, monkeypatch):
    workspace_cwd = tmp_path / "workspace"
    workspace_cwd.mkdir()
    baseline = workspace_cwd / "baseline.py"
    baseline.write_text("VALUE = 1\n")
    sibling = workspace_cwd / "sibling.py"
    sibling.write_text("SIBLING = 1\n")
    checkpoint_base = tmp_path / "checkpoints"

    monkeypatch.setenv("TERMINAL_CWD", str(workspace_cwd))
    monkeypatch.setattr(
        "tools.checkpoint_manager.CHECKPOINT_BASE",
        checkpoint_base,
    )

    manager = CheckpointManager(enabled=True)
    agent = SimpleNamespace(_checkpoint_mgr=manager)
    _ensure_file_checkpoint(
        agent,
        "patch",
        {
            "mode": "patch",
            "patch": (
                "*** Begin Patch\n"
                "*** Update File: baseline.py\n"
                "@@\n"
                "-VALUE = 1\n"
                "+VALUE = 2\n"
                "*** Update File: sibling.py\n"
                "@@\n"
                "-SIBLING = 1\n"
                "+SIBLING = 2\n"
                "*** End Patch\n"
            ),
        },
        "session",
    )

    checkpoints = manager.list_checkpoints(str(workspace_cwd))
    assert len(checkpoints) == 1
    assert checkpoints[0]["reason"] == "before patch"

    baseline.write_text("VALUE = 2\n")
    ok, content, error = _run_git(
        ["show", f"{checkpoints[0]['hash']}:baseline.py"],
        _store_path(checkpoint_base),
        str(workspace_cwd),
    )
    assert ok, error
    assert content == "VALUE = 1"

    ok, content, error = _run_git(
        ["show", f"{checkpoints[0]['hash']}:sibling.py"],
        _store_path(checkpoint_base),
        str(workspace_cwd),
    )
    assert ok, error
    assert content == "SIBLING = 1"


def test_v4a_patch_checkpoints_each_target_workspace(tmp_path, monkeypatch):
    left_workspace = tmp_path / "left"
    right_workspace = tmp_path / "right"
    for workspace, value in ((left_workspace, 1), (right_workspace, 2)):
        workspace.mkdir()
        (workspace / "pyproject.toml").write_text("[project]\n")
        (workspace / "baseline.py").write_text(f"VALUE = {value}\n")
    checkpoint_base = tmp_path / "checkpoints"

    monkeypatch.setenv("TERMINAL_CWD", str(left_workspace))
    monkeypatch.setattr(
        "tools.checkpoint_manager.CHECKPOINT_BASE",
        checkpoint_base,
    )

    manager = CheckpointManager(enabled=True)
    agent = SimpleNamespace(_checkpoint_mgr=manager)
    _ensure_file_checkpoint(
        agent,
        "patch",
        {
            "mode": "patch",
            "patch": (
                "*** Begin Patch\n"
                f"*** Update File: {left_workspace / 'baseline.py'}\n"
                f"*** Update File: {right_workspace / 'baseline.py'}\n"
                "*** End Patch\n"
            ),
        },
        "session",
    )

    for workspace, value in ((left_workspace, 1), (right_workspace, 2)):
        checkpoints = manager.list_checkpoints(str(workspace))
        assert len(checkpoints) == 1
        assert checkpoints[0]["reason"] == "before patch"
        ok, content, error = _run_git(
            ["show", f"{checkpoints[0]['hash']}:baseline.py"],
            _store_path(checkpoint_base),
            str(workspace),
        )
        assert ok, error
        assert content == f"VALUE = {value}"
