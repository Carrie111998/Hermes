from __future__ import annotations

import json
from pathlib import Path

from hermes_cli import kanban_db as kb


def _profile(tmp_path: Path, name: str = "worker") -> Path:
    home = tmp_path / ".hermes"
    profile = home / "profiles" / name
    (profile / "skills" / "available").mkdir(parents=True)
    (profile / "skills" / "available" / "SKILL.md").write_text(
        "---\nname: available\ndescription: ready\nplatforms: [linux]\n---\n",
        encoding="utf-8",
    )
    return profile


def test_missing_pinned_skill_blocks_before_claim_without_retry_charge(
    tmp_path: Path, monkeypatch,
) -> None:
    _profile(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="needs skill",
            assignee="worker",
            skills=["missing"],
        )
        spawned = []
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *args, **kwargs: spawned.append(args) or 123,
        )

        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"
        assert task.block_kind == "capability"
        assert "missing" in kb.list_comments(conn, task_id)[-1].body
        assert spawned == []
        assert result.capability_blocked == [task_id]
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (task_id,)
        ).fetchone()[0] == 0
        row = conn.execute(
            "SELECT consecutive_failures FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        assert row[0] == 0


def test_disabled_pinned_skill_is_not_spawned(tmp_path: Path, monkeypatch) -> None:
    profile = _profile(tmp_path)
    (profile / "config.yaml").write_text(
        "skills:\n  disabled: [available]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="disabled skill",
            assignee="worker",
            skills=["available"],
        )
        result = kb.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: 123)

        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "blocked"
        assert "disabled" in kb.list_comments(conn, task_id)[-1].body
        assert result.capability_blocked == [task_id]
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (task_id,)
        ).fetchone()[0] == 0


def test_available_profile_skill_claims_and_spawns(tmp_path: Path, monkeypatch) -> None:
    _profile(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="ready skill",
            assignee="worker",
            skills=["available"],
        )
        result = kb.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: 123)

        assert [item[0] for item in result.spawned] == [task_id]
        assert result.capability_blocked == []
        row = conn.execute(
            "SELECT skills FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        assert json.loads(row[0]) == ["available"]
