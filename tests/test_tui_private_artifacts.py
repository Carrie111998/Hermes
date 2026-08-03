import os
import stat
import sys
from pathlib import Path

import pytest

from tui_gateway import server


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX owner/mode/link hardening contract",
)


def _install_session(monkeypatch, home: Path, sid: str = "live-a") -> dict:
    home.mkdir(mode=0o700)
    session = {
        "session_key": f"conversation-{sid}",
        "profile_home": str(home),
        "running": False,
    }
    monkeypatch.setattr(server, "_sessions", {sid: session})
    return session


def _assert_private_tree(path: Path, home: Path) -> None:
    path.resolve().relative_to(home.resolve())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.stat().st_nlink == 1
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_collapsed_paste_is_session_profile_private(monkeypatch, tmp_path):
    profile = tmp_path / "profile-a"
    _install_session(monkeypatch, profile)

    response = server._methods["paste.collapse"](
        "paste-private",
        {"session_id": "live-a", "text": "private pasted content\nsecond line"},
    )

    assert "error" not in response
    path = Path(response["result"]["path"])
    _assert_private_tree(path, profile)
    assert path.read_text(encoding="utf-8") == "private pasted content\nsecond line"


def test_collapsed_paste_rejects_symlinked_storage_root(monkeypatch, tmp_path):
    profile = tmp_path / "profile-a"
    _install_session(monkeypatch, profile)
    external = tmp_path / "external"
    external.mkdir()
    (profile / "pastes").symlink_to(external, target_is_directory=True)

    response = server._methods["paste.collapse"](
        "paste-symlink",
        {"session_id": "live-a", "text": "must not escape"},
    )

    assert response["error"]["code"] == 5000
    assert list(external.iterdir()) == []


def test_spawn_tree_is_profile_private_no_follow_and_cross_profile_fenced(
    monkeypatch,
    tmp_path,
):
    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"
    profile_a.mkdir(mode=0o700)
    profile_b.mkdir(mode=0o700)
    sessions = {
        "live-a": {
            "session_key": "conversation-a",
            "profile_home": str(profile_a),
            "running": False,
        },
        "live-b": {
            "session_key": "conversation-b",
            "profile_home": str(profile_b),
            "running": False,
        },
    }
    monkeypatch.setattr(server, "_sessions", sessions)

    saved = server._methods["spawn_tree.save"](
        "spawn-save",
        {
            "session_id": "live-a",
            "finished_at": 1234.5,
            "label": "private task",
            "subagents": [{"id": "child", "summary": "private summary"}],
        },
    )
    assert "error" not in saved
    path = Path(saved["result"]["path"])
    _assert_private_tree(path, profile_a)
    assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700

    loaded = server._methods["spawn_tree.load"](
        "spawn-load-a",
        {"session_id": "live-a", "path": str(path)},
    )
    assert loaded["result"]["label"] == "private task"

    crossed = server._methods["spawn_tree.load"](
        "spawn-load-b",
        {"session_id": "live-b", "path": str(path)},
    )
    assert crossed["error"]["code"] == 4030


def test_spawn_tree_rejects_symlinked_root(monkeypatch, tmp_path):
    profile = tmp_path / "profile-a"
    _install_session(monkeypatch, profile)
    external = tmp_path / "external"
    external.mkdir()
    (profile / "spawn-trees").symlink_to(external, target_is_directory=True)

    response = server._methods["spawn_tree.save"](
        "spawn-symlink",
        {
            "session_id": "live-a",
            "subagents": [{"id": "child", "summary": "must not escape"}],
        },
    )

    assert response["error"]["code"] == 5000
    assert list(external.iterdir()) == []
