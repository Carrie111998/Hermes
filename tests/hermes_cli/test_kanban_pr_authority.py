"""Authority boundaries for exact-head pull-request automation tasks."""

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _write_profile(home: Path, name: str, description: str) -> None:
    profile_dir = home / "profiles" / name
    profile_dir.mkdir(parents=True)
    (profile_dir / "profile.yaml").write_text(
        f"name: {name}\ndescription: {description!r}\n",
        encoding="utf-8",
    )


def _repair_body() -> str:
    return (
        '{"repository":"mrkillbob/luna-bot","pr_number":132,'
        '"expected_head_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        '"action":"repair_and_push"}'
    )


def test_create_rejects_read_only_owner_for_atomic_pr_repair(kanban_home):
    _write_profile(
        kanban_home,
        "review-verification-steward",
        "Read-only verifier; never edits, pushes, replies, refreshes, or merges.",
    )

    with kb.connect() as conn, pytest.raises(ValueError, match="read-only profile"):
        kb.create_task(
            conn,
            title="Repair and push LunaBot PR #132",
            body=_repair_body(),
            assignee="review-verification-steward",
            idempotency_key="github-pr-feedback:repair:132:abc",
        )


def test_reassign_rejects_read_only_owner_and_preserves_current_owner(kanban_home):
    _write_profile(
        kanban_home,
        "review-verification-steward",
        "Read-only verifier; never edits, pushes, replies, refreshes, or merges.",
    )
    _write_profile(
        kanban_home,
        "pr-repair-steward",
        "Repairs pull requests, pushes exact-head fixes, and posts factual replies.",
    )

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Resolve merge conflict and push PR #132",
            body=_repair_body(),
            assignee="pr-repair-steward",
            idempotency_key="github-pr-feedback:repair:132:abc",
        )
        with pytest.raises(ValueError, match="read-only profile"):
            kb.reassign_task(conn, tid, "review-verification-steward")
        assert kb.get_task(conn, tid).assignee == "pr-repair-steward"


def test_claim_rejects_authority_revoked_between_admission_and_claim(kanban_home):
    """review NousResearch/hermes-agent#97368: proof consumed once at
    create_task() time must not silently authorize a claim after the
    assignee's profile has since become read-only."""
    _write_profile(
        kanban_home,
        "pr-repair-steward",
        "Repairs pull requests, pushes exact-head fixes, and posts factual replies.",
    )

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Repair and push LunaBot PR #132",
            body=_repair_body(),
            assignee="pr-repair-steward",
            idempotency_key="github-pr-feedback:repair:132:abc",
        )
        # Authority is revoked in place after admission -- no reassignment,
        # just an edit to the already-admitted assignee's own profile.
        (kanban_home / "profiles" / "pr-repair-steward" / "profile.yaml").write_text(
            "name: pr-repair-steward\n"
            "description: 'Read-only verifier now; never edits, pushes, replies, "
            "refreshes, or merges.'\n",
            encoding="utf-8",
        )
        assert kb.claim_task(conn, tid) is None
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "capability"


def test_claim_rejects_unreadable_authority_metadata(kanban_home):
    """review NousResearch/hermes-agent#97368: unknown authority (missing
    or unreadable profile.yaml at claim time) must fail closed for a
    write-class task, not be treated as proof of write capability."""
    _write_profile(
        kanban_home,
        "pr-repair-steward",
        "Repairs pull requests, pushes exact-head fixes, and posts factual replies.",
    )

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Repair and push LunaBot PR #132",
            body=_repair_body(),
            assignee="pr-repair-steward",
            idempotency_key="github-pr-feedback:repair:132:abc",
        )
        profile_yaml = kanban_home / "profiles" / "pr-repair-steward" / "profile.yaml"
        profile_yaml.write_text("not: [valid, yaml", encoding="utf-8")
        assert kb.claim_task(conn, tid) is None
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "capability"


def test_read_only_profile_may_own_exact_head_verification(kanban_home):
    _write_profile(
        kanban_home,
        "review-verification-steward",
        "Read-only verifier; never edits, pushes, replies, refreshes, or merges.",
    )

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Review exact-head CI evidence for PR #132",
            body=(
                '{"repository":"mrkillbob/luna-bot","pr_number":132,'
                '"expected_head_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
                '"action":"verify_ci_receipt"}'
            ),
            assignee="review-verification-steward",
            idempotency_key="github-pr-feedback:review:132:abc",
        )
        assert kb.get_task(conn, tid).assignee == "review-verification-steward"
