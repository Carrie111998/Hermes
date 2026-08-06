"""Kanban forced-skill capability preflight regression coverage."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def _write_skill(root: Path, name: str, *, directory: str | None = None) -> Path:
    skill_dir = root / (directory or name)
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n\nTest instructions.\n",
        encoding="utf-8",
    )
    return skill_md


@pytest.fixture
def isolated_kanban_profiles(tmp_path, monkeypatch):
    """A real shared board plus two isolated named profile homes."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    for profile in ("alpha", "beta"):
        (home / "profiles" / profile / "skills").mkdir(parents=True)
    kb.init_db()
    conn = kb.connect()
    try:
        yield conn, home
    finally:
        conn.close()


def test_create_task_rejects_all_missing_assignee_skills_before_insert(
    isolated_kanban_profiles,
):
    conn, _home = isolated_kanban_profiles

    with pytest.raises(ValueError, match=r"alpha.*missing-one.*missing-two"):
        kb.create_task(
            conn,
            title="requires unavailable capabilities",
            assignee="alpha",
            skills=["missing-one", "missing-two"],
        )

    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_create_task_reports_only_partial_missing_assignee_skills(
    isolated_kanban_profiles,
):
    conn, home = isolated_kanban_profiles
    _write_skill(home / "profiles" / "alpha" / "skills", "available")

    with pytest.raises(ValueError, match=r"alpha.*unavailable") as exc_info:
        kb.create_task(
            conn,
            title="requires one unavailable capability",
            assignee="alpha",
            skills=["available", "unavailable"],
        )

    assert "required skill(s): unavailable." in str(exc_info.value)
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_create_task_accepts_target_local_and_external_skills(
    isolated_kanban_profiles,
):
    conn, home = isolated_kanban_profiles
    profile = home / "profiles" / "alpha"
    _write_skill(profile / "skills", "bundled-like", directory="development/bundled-like")
    external = home / "shared-skills"
    _write_skill(external, "external-only")
    (profile / "config.yaml").write_text(
        f"skills:\n  external_dirs:\n    - {external}\n",
        encoding="utf-8",
    )

    task_id = kb.create_task(
        conn,
        title="uses target capabilities",
        assignee="alpha",
        skills=["bundled-like", "external-only"],
    )

    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.skills == ["bundled-like", "external-only"]


def test_dispatch_blocks_skill_removed_after_valid_create_without_starting(
    isolated_kanban_profiles,
):
    conn, home = isolated_kanban_profiles
    skill_md = _write_skill(home / "profiles" / "alpha" / "skills", "removable")
    task_id = kb.create_task(
        conn, title="skill can disappear", assignee="alpha", skills=["removable"]
    )
    skill_md.unlink()
    spawned = []

    kb.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: spawned.append(args) or 7)

    row = conn.execute(
        "SELECT status, block_kind, claim_lock, worker_pid, current_run_id, "
        "consecutive_failures FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    events = list(conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
    ))
    assert spawned == []
    assert row["status"] == "blocked"
    assert row["block_kind"] == "capability"
    assert row["claim_lock"] is None
    assert row["worker_pid"] is None
    assert row["current_run_id"] is None
    assert row["consecutive_failures"] == 0
    assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (task_id,)).fetchone()[0] == 0
    assert [event["kind"] for event in events] == ["created", "blocked"]
    assert "alpha" in events[-1]["payload"]
    assert "removable" in events[-1]["payload"]


def test_dispatch_revalidates_a_reassigned_card_before_claim_or_spawn(
    isolated_kanban_profiles,
):
    conn, home = isolated_kanban_profiles
    _write_skill(home / "profiles" / "alpha" / "skills", "alpha-only")
    task_id = kb.create_task(
        conn, title="reassigned after create", assignee="alpha", skills=["alpha-only"]
    )
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET assignee = 'beta' WHERE id = ?", (task_id,))
    spawned = []

    kb.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: spawned.append(args) or 7)

    row = conn.execute(
        "SELECT status, block_kind, claim_lock, worker_pid, current_run_id, "
        "consecutive_failures FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    blocked = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked'",
        (task_id,),
    ).fetchone()
    assert spawned == []
    assert row["status"] == "blocked"
    assert row["block_kind"] == "capability"
    assert row["claim_lock"] is None
    assert row["worker_pid"] is None
    assert row["current_run_id"] is None
    assert row["consecutive_failures"] == 0
    assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (task_id,)).fetchone()[0] == 0
    assert "beta" in blocked["payload"]
    assert "alpha-only" in blocked["payload"]
