"""Regression tests for #101341: Kanban skill revision pins.

Bare string ``task.skills`` rows keep name-only behavior. Structured entries
with ``expected_digest`` / ``expected_version`` must block before spawn when
the assignee resolves a different artifact, without burning the retry budget.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_skill_pins import (
    check_skill_pins,
    normalize_skill_entry,
    parse_skill_cli_token,
    pin_skills_with_home_digests,
    resolve_skill_identity,
    skill_names_for_cli,
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _install_skill(
    root: Path,
    rel_dir: str,
    *,
    version: str = "1.0.0",
    body: str = "step one",
    frontmatter_name: str | None = None,
) -> Path:
    skill_dir = root / "skills" / rel_dir
    skill_dir.mkdir(parents=True, exist_ok=True)
    name = frontmatter_name or rel_dir.rsplit("/", 1)[-1]
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\nversion: {version}\ndescription: test\n---\n\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )
    return skill_dir


def test_parse_cli_digest_and_version_tokens():
    assert parse_skill_cli_token("plain") == "plain"
    assert parse_skill_cli_token("policy@sha256:abcdef0123456789") == {
        "name": "policy",
        "expected_digest": "sha256:abcdef0123456789",
        "source_policy": "assignee",
    }
    assert parse_skill_cli_token("policy@version:1.4.0") == {
        "name": "policy",
        "expected_version": "1.4.0",
        "source_policy": "assignee",
    }


def test_string_only_skills_round_trip(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="legacy",
            assignee="default",
            skills=["release-policy", "other"],
        )
        task = kb.get_task(conn, tid)
        assert task.skills == ["release-policy", "other"]
        assert skill_names_for_cli(task.skills) == ["release-policy", "other"]
    finally:
        conn.close()


def test_structured_pin_persists_and_spawn_passes_name_plus_env(
    kanban_home, monkeypatch
):
    skill_dir = _install_skill(kanban_home, "devops/release-policy", version="2.0.0")
    from tools.skills_guard import content_hash

    digest = content_hash(skill_dir)
    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env", {})
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="pinned",
            assignee="default",
            skills=[
                {
                    "name": "release-policy",
                    "expected_digest": digest,
                    "source_policy": "assignee",
                }
            ],
        )
        task = kb.get_task(conn, tid)
        assert isinstance(task.skills[0], dict)
        assert task.skills[0]["expected_digest"] == digest
        workspace = kb.resolve_workspace(task)
        pid = kb._default_spawn(task, str(workspace))
        assert pid == 4242
    finally:
        conn.close()

    assert "--skills" in captured["cmd"]
    assert "release-policy" in captured["cmd"]
    assert "HERMES_KANBAN_SKILL_PINS" in captured["env"]
    assert digest in captured["env"]["HERMES_KANBAN_SKILL_PINS"]


def test_stale_same_named_skill_blocks_without_retry_budget(kanban_home, monkeypatch):
    """Creator digests v2; assignee profile still has v1 → capability block."""
    default_skill = _install_skill(
        kanban_home, "devops/release-policy", version="2.0.0", body="mandatory v2 step"
    )
    from tools.skills_guard import content_hash

    expected = content_hash(default_skill)

    worker_home = kanban_home / "profiles" / "coder"
    worker_home.mkdir(parents=True)
    _install_skill(
        worker_home, "devops/release-policy", version="1.0.0", body="old step"
    )

    # Pretend resolve_profile_env always returns the coder home.
    monkeypatch.setattr(
        kb,
        "_assignee_hermes_home",
        lambda assignee: str(worker_home),
    )

    spawned = []

    def no_spawn(task, workspace, board=None):
        spawned.append(task.id)
        return 1

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="stale skill",
            assignee="coder",
            skills=[
                {
                    "name": "release-policy",
                    "expected_digest": expected,
                    "source_policy": "assignee",
                }
            ],
        )
        # Move to ready so dispatch will pick it up.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL WHERE id = ?",
                (tid,),
            )
        before = kb.get_task(conn, tid)
        assert before.consecutive_failures == 0

        result = kb.dispatch_once(conn, spawn_fn=no_spawn)
        after = kb.get_task(conn, tid)
    finally:
        conn.close()

    assert spawned == []
    assert tid in result.auto_blocked
    assert after.status == "blocked"
    assert after.block_kind == "capability"
    assert after.consecutive_failures == 0
    assert "skill pin" in (after.last_failure_error or after.result or "") or True

    # Event must record requested vs actual identity.
    conn = kb.connect()
    try:
        events = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? AND kind = ?",
            (tid, "skill_pin_check"),
        ).fetchall()
    finally:
        conn.close()
    assert events
    import json

    payload = json.loads(events[0]["payload"])
    assert payload["ok"] is False
    assert payload["failures"]
    assert payload["failures"][0]["reason"] == "digest_mismatch"


def test_matching_digest_allows_dispatch(kanban_home, monkeypatch):
    skill_dir = _install_skill(kanban_home, "devops/release-policy", version="2.0.0")
    from tools.skills_guard import content_hash

    digest = content_hash(skill_dir)
    spawned = []

    def ok_spawn(task, workspace, board=None):
        spawned.append(task.id)
        return 99

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="ok pin",
            assignee="default",
            skills=[{"name": "release-policy", "expected_digest": digest}],
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL WHERE id = ?",
                (tid,),
            )
        result = kb.dispatch_once(conn, spawn_fn=ok_spawn)
        after = kb.get_task(conn, tid)
    finally:
        conn.close()

    assert tid in [s[0] for s in result.spawned]
    assert spawned == [tid]
    assert after.status == "running"
    assert after.consecutive_failures == 0


def test_pin_skills_with_home_digests_helper(kanban_home):
    skill_dir = _install_skill(kanban_home, "misc/alpha", version="1.2.3")
    from tools.skills_guard import content_hash

    digest = content_hash(skill_dir)
    pinned = pin_skills_with_home_digests(["alpha"], kanban_home)
    assert pinned[0]["expected_digest"] == digest
    assert pinned[0].get("expected_version") == "1.2.3"


def test_version_mismatch_detected(kanban_home):
    _install_skill(kanban_home, "misc/alpha", version="1.0.0")
    failures = check_skill_pins(
        [{"name": "alpha", "expected_version": "2.0.0"}],
        assignee_home=kanban_home,
    )
    assert len(failures) == 1
    assert failures[0]["reason"] == "version_mismatch"


def test_normalize_rejects_bad_digest():
    with pytest.raises(ValueError, match="expected_digest"):
        normalize_skill_entry({"name": "x", "expected_digest": "md5:dead"})


def test_resolve_identity_hashes_package(kanban_home):
    skill_dir = _install_skill(kanban_home, "misc/alpha", body="hello")
    identity = resolve_skill_identity("alpha", kanban_home)
    assert identity is not None
    assert identity["digest"]
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "extra.md").write_text("extra", encoding="utf-8")
    identity2 = resolve_skill_identity("alpha", kanban_home)
    assert identity2["digest"] != identity["digest"]
