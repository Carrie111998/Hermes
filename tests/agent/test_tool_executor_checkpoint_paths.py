"""Behavioral coverage for tool checkpoint path resolution."""

from types import SimpleNamespace

from agent.tool_executor import _ensure_file_checkpoint, _ensure_terminal_checkpoint
import tools.terminal_tool as terminal_tool
from tools.checkpoint_manager import CheckpointManager


def test_relative_file_checkpoint_uses_task_workspace(tmp_path, monkeypatch):
    """Checkpoint lookup must use the same cwd as a relative file mutation."""
    process_cwd = tmp_path / "opt" / "hermes"
    workspace_cwd = tmp_path / "opt" / "data" / "workspace"
    process_cwd.mkdir(parents=True)
    workspace_cwd.mkdir(parents=True)

    # Both directories contain content so checkpointing the wrong one would
    # still succeed and remain observable as the regression did in Docker.
    (process_cwd / "pyproject.toml").write_text(
        "[project]\nname = 'hermes'\n", encoding="utf-8"
    )
    (workspace_cwd / "pyproject.toml").write_text(
        "[project]\nname = 'workspace'\n", encoding="utf-8"
    )
    (workspace_cwd / "existing.txt").write_text("before\n", encoding="utf-8")

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


def test_terminal_checkpoint_covers_commands_outside_mutation_heuristic(
    tmp_path,
    monkeypatch,
):
    """Arbitrary shell programs still get rollback coverage inside workdir."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    victim = workspace / "victim.txt"
    victim.write_text("before\n", encoding="utf-8")

    monkeypatch.setattr(
        "tools.checkpoint_manager.CHECKPOINT_BASE",
        tmp_path / "checkpoints",
    )
    manager = CheckpointManager(enabled=True)
    agent = SimpleNamespace(_checkpoint_mgr=manager)
    command = "python -c \"from pathlib import Path; Path('victim.txt').unlink()\""

    _ensure_terminal_checkpoint(
        agent,
        {"command": command, "workdir": str(workspace)},
        "gateway-session",
    )
    victim.unlink()

    checkpoints = manager.list_checkpoints(str(workspace))
    assert checkpoints
    result = manager.restore(str(workspace), checkpoints[0]["hash"])
    assert result["success"] is True
    assert victim.read_text(encoding="utf-8") == "before\n"


def test_terminal_checkpoint_without_workdir_uses_session_cwd(tmp_path, monkeypatch):
    """Checkpoint resolution follows the terminal session after a prior cd."""
    process_cwd = tmp_path / "process"
    session_cwd = tmp_path / "session"
    process_cwd.mkdir()
    session_cwd.mkdir()
    (process_cwd / "process.txt").write_text("outside\n", encoding="utf-8")
    (session_cwd / "victim.txt").write_text("before\n", encoding="utf-8")

    monkeypatch.chdir(process_cwd)
    monkeypatch.setenv("TERMINAL_CWD", str(process_cwd))
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "local", "cwd": str(process_cwd)},
    )
    monkeypatch.setattr(
        "tools.checkpoint_manager.CHECKPOINT_BASE",
        tmp_path / "checkpoints",
    )
    terminal_tool.record_session_cwd("gateway-session", str(session_cwd))

    manager = CheckpointManager(enabled=True)
    agent = SimpleNamespace(_checkpoint_mgr=manager)
    _ensure_terminal_checkpoint(
        agent,
        {"command": "python -c 'from pathlib import Path; Path(\"victim.txt\").unlink()'"},
        "gateway-session",
    )

    assert manager.list_checkpoints(str(session_cwd))
    assert manager.list_checkpoints(str(process_cwd)) == []
