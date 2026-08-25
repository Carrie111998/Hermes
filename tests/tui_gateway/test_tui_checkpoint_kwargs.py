"""TUI/desktop sessions honor config.yaml checkpoints.enabled."""

import threading
import types

from tools.checkpoint_manager import CheckpointManager
from tui_gateway import server
from tui_gateway.server import apply_checkpoint_kwargs, resolve_tui_checkpoint_kwargs


def test_resolve_defaults_off_without_config_or_env():
    kwargs = resolve_tui_checkpoint_kwargs({}, environ={})
    assert kwargs["checkpoints_enabled"] is False
    assert kwargs["checkpoint_max_snapshots"] == 20
    assert kwargs["checkpoint_max_total_size_mb"] == 500
    assert kwargs["checkpoint_max_file_size_mb"] == 10


def test_resolve_honors_config_enabled_without_env():
    kwargs = resolve_tui_checkpoint_kwargs(
        {"checkpoints": {"enabled": True, "max_snapshots": 9}},
        environ={},
    )
    assert kwargs["checkpoints_enabled"] is True
    assert kwargs["checkpoint_max_snapshots"] == 9


def test_resolve_env_overrides_config_off():
    kwargs = resolve_tui_checkpoint_kwargs(
        {"checkpoints": {"enabled": False}},
        environ={"HERMES_TUI_CHECKPOINTS": "1"},
    )
    assert kwargs["checkpoints_enabled"] is True


def test_resolve_legacy_bool_checkpoints_section():
    kwargs = resolve_tui_checkpoint_kwargs({"checkpoints": True}, environ={})
    assert kwargs["checkpoints_enabled"] is True


def test_apply_checkpoint_kwargs_toggles_live_manager():
    class _Mgr:
        enabled = False
        max_snapshots = 20
        max_total_size_mb = 500
        max_file_size_mb = 10

    mgr = _Mgr()
    apply_checkpoint_kwargs(
        mgr,
        {
            "checkpoints_enabled": True,
            "checkpoint_max_snapshots": 7,
            "checkpoint_max_total_size_mb": 100,
            "checkpoint_max_file_size_mb": 3,
        },
    )
    assert mgr.enabled is True
    assert mgr.max_snapshots == 7
    assert mgr.max_total_size_mb == 100
    assert mgr.max_file_size_mb == 3


def test_live_workspace_restore_keeps_conversation(tmp_path, monkeypatch):
    """Real shadow-store restore via rollback.restore, transcript unchanged."""
    work = tmp_path / "project"
    work.mkdir()
    target = work / "notes.txt"
    target.write_text("good\n")
    store = tmp_path / "checkpoints"
    monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", store)
    mgr = CheckpointManager(enabled=True, max_snapshots=20)
    assert mgr.ensure_checkpoint(str(work), "before-edit") is True
    mgr.new_turn()
    target.write_text("broken\n")

    history = [
        {"role": "user", "content": "please edit notes"},
        {"role": "assistant", "content": "edited"},
    ]
    sid = "live-cp"
    server._sessions[sid] = {
        "agent": types.SimpleNamespace(_checkpoint_mgr=mgr),
        "session_key": sid,
        "history": list(history),
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "cwd": str(work),
    }
    monkeypatch.setattr(server, "_session_cwd", lambda session: str(work))
    monkeypatch.setattr(
        server, "_load_cfg", lambda: {"checkpoints": {"enabled": True}}
    )
    try:
        cps = mgr.list_checkpoints(str(work))
        assert cps
        resp = server.handle_request(
            {
                "id": "1",
                "method": "rollback.restore",
                "params": {
                    "session_id": sid,
                    "hash": cps[0]["hash"],
                    "rewind_history": False,
                },
            }
        )
        assert resp["result"]["success"] is True
        assert resp["result"]["history_removed"] == 0
        assert target.read_text() == "good\n"
        assert [m["content"] for m in server._sessions[sid]["history"]] == [
            "please edit notes",
            "edited",
        ]
    finally:
        server._sessions.pop(sid, None)
