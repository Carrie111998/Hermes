"""Protected workspace policy invariants at the Kanban claim boundary."""

from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban as kanban_cli


@pytest.fixture
def isolated_kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _make_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            "add",
            "README.md",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "init",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return repo


@pytest.mark.parametrize(
    "candidate",
    [
        r"c:\work\repo",
        r"C:/WORK/REPO/",
        r"C:\Work\Repo\\",
    ],
)
def test_normalize_workspace_root_handles_windows_case_slashes_and_trailing_separators(
    candidate: str,
) -> None:
    assert kb.normalize_workspace_root(candidate) == "c:/work/repo"


def test_protect_workspace_persists_exact_owner_tuple_on_one_board(
    isolated_kanban_home: Path,
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    with kb.connect() as conn:
        owner_id = kb.create_task(
            conn,
            title="sole integration owner",
            workspace_kind="dir",
            workspace_path=str(root),
        )
        policy = kb.protect_workspace(conn, root, authorized_task_id=owner_id)

        assert policy == {
            "normalized_root": kb.normalize_workspace_root(root),
            "root_path": str(root.resolve()),
            "authorized_task_id": owner_id,
            "authorized_title": "sole integration owner",
            "authorized_workspace_kind": "dir",
            "is_git_root": False,
        }
        assert kb.list_protected_workspaces(conn) == [policy]

    other_db = tmp_path / "other-board.db"
    with kb.connect(db_path=other_db) as other:
        assert kb.list_protected_workspaces(other) == []


def test_manual_claim_allows_exact_owner_and_blocks_newer_dir_before_run_creation(
    isolated_kanban_home: Path,
    tmp_path: Path,
) -> None:
    root = tmp_path / "primary"
    root.mkdir()
    with kb.connect() as conn:
        owner_id = kb.create_task(
            conn,
            title="sole integration owner",
            assignee="reviewer",
            workspace_kind="dir",
            workspace_path=str(root),
        )
        kb.protect_workspace(conn, root, authorized_task_id=owner_id)
        duplicate_id = kb.create_task(
            conn,
            title="newer duplicate writer",
            assignee="reviewer",
            workspace_kind="dir",
            workspace_path=str(root),
        )

        owner = kb.claim_task(conn, owner_id, claimer="manual-owner")
        assert owner is not None and owner.status == "running"
        assert kb.claim_task(conn, duplicate_id, claimer="manual-duplicate") is None

        duplicate = kb.get_task(conn, duplicate_id)
        assert duplicate is not None
        assert duplicate.status == "blocked"
        assert duplicate.current_run_id is None
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?",
            (duplicate_id,),
        ).fetchone()[0] == 0
        conflict = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'protected_workspace_conflict' "
            "ORDER BY id DESC LIMIT 1",
            (duplicate_id,),
        ).fetchone()
        assert conflict is not None
        payload = json.loads(conflict["payload"])
        assert payload["severity"] == "critical"
        assert payload["authorized_task_id"] == owner_id
        assert payload["conflicting_task_ids"] == [owner_id, duplicate_id]
        blocked = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'blocked' ORDER BY id DESC LIMIT 1",
            (duplicate_id,),
        ).fetchone()
        assert blocked is not None
        assert json.loads(blocked["payload"])["kind"] == "capability"


def test_manual_review_claim_blocks_unauthorized_root_before_run_creation(
    isolated_kanban_home: Path,
    tmp_path: Path,
) -> None:
    root = tmp_path / "primary-review"
    root.mkdir()
    with kb.connect() as conn:
        owner_id = kb.create_task(
            conn,
            title="review root owner",
            workspace_kind="dir",
            workspace_path=str(root),
        )
        kb.protect_workspace(conn, root, authorized_task_id=owner_id)
        review_id = kb.create_task(
            conn,
            title="unauthorized reviewer",
            assignee="reviewer",
            workspace_kind="dir",
            workspace_path=str(root),
        )
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (review_id,))

        assert kb.claim_review_task(conn, review_id, claimer="manual-review") is None
        review_task = kb.get_task(conn, review_id)
        assert review_task is not None
        assert review_task.status == "blocked"
        assert review_task.current_run_id is None
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?",
            (review_id,),
        ).fetchone()[0] == 0


def test_manual_claim_redirects_worktree_root_before_claim(
    isolated_kanban_home: Path,
    tmp_path: Path,
) -> None:
    repo = _make_git_repo(tmp_path)
    with kb.connect() as conn:
        owner_id = kb.create_task(
            conn,
            title="git root owner",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        policy = kb.protect_workspace(conn, repo, authorized_task_id=owner_id)
        assert policy["is_git_root"] is True
        worktree_id = kb.create_task(
            conn,
            title="isolated implementation",
            assignee="worker",
            workspace_kind="worktree",
            workspace_path=str(repo),
            branch_name="feat/custom-isolated",
        )

        claimed = kb.claim_task(conn, worktree_id, claimer="manual-worktree")
        assert claimed is not None
        expected = (repo / ".worktrees" / worktree_id).resolve()
        assert Path(claimed.workspace_path).resolve() == expected
        assert claimed.branch_name == "feat/custom-isolated"
        events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
            (worktree_id,),
        ).fetchall()
        kinds = [row["kind"] for row in events]
        assert kinds.index("protected_workspace_redirected") < kinds.index("claimed")


def test_explicit_exact_tuple_allowlist_can_claim_protected_dir(
    isolated_kanban_home: Path,
    tmp_path: Path,
) -> None:
    root = tmp_path / "allowlisted-root"
    root.mkdir()
    with kb.connect() as conn:
        owner_id = kb.create_task(
            conn,
            title="root owner",
            workspace_kind="dir",
            workspace_path=str(root),
        )
        kb.protect_workspace(conn, root, authorized_task_id=owner_id)
        helper_id = kb.create_task(
            conn,
            title="explicit read-only audit",
            workspace_kind="dir",
            workspace_path=str(root),
        )

        allowed = kb.allow_task_at_protected_workspace(
            conn,
            root,
            task_id=helper_id,
        )
        assert allowed == {
            "normalized_root": kb.normalize_workspace_root(root),
            "task_id": helper_id,
            "title": "explicit read-only audit",
            "workspace_kind": "dir",
        }
        claimed = kb.claim_task(conn, helper_id, claimer="allowlisted-helper")
        assert claimed is not None and claimed.status == "running"


def test_authorized_task_id_with_changed_title_is_not_the_exact_owner(
    isolated_kanban_home: Path,
    tmp_path: Path,
) -> None:
    root = tmp_path / "exact-owner-root"
    root.mkdir()
    with kb.connect() as conn:
        owner_id = kb.create_task(
            conn,
            title="recorded owner title",
            workspace_kind="dir",
            workspace_path=str(root),
        )
        kb.protect_workspace(conn, root, authorized_task_id=owner_id)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET title='mutated owner title' WHERE id=?",
                (owner_id,),
            )

        assert kb.claim_task(conn, owner_id, claimer="mutated-owner") is None
        owner = kb.get_task(conn, owner_id)
        assert owner is not None
        assert owner.status == "blocked"
        assert owner.current_run_id is None


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_embedded_dispatch_never_calls_spawn_for_protected_root_conflict(
    isolated_kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lane: str,
) -> None:
    import hermes_cli.config as config_module
    import hermes_cli.profiles as profiles_module

    root = tmp_path / f"dispatch-{lane}"
    root.mkdir()
    with kb.connect() as conn:
        owner_id = kb.create_task(
            conn,
            title=f"{lane} root owner",
            assignee="worker",
            workspace_kind="dir",
            workspace_path=str(root),
        )
        kb.protect_workspace(conn, root, authorized_task_id=owner_id)
        assert kb.claim_task(conn, owner_id, claimer="live-owner") is not None
        conflict_id = kb.create_task(
            conn,
            title=f"unauthorized {lane} task",
            assignee="worker",
            workspace_kind="dir",
            workspace_path=str(root),
        )
        if lane == "review":
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status='review' WHERE id=?",
                    (conflict_id,),
                )

        monkeypatch.setattr(profiles_module, "profile_exists", lambda _name: True)
        monkeypatch.setattr(
            config_module,
            "load_config",
            lambda *args, **kwargs: {"kanban": {"review_dispatch": True}},
        )
        monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "ok")
        monkeypatch.setattr(kb, "reap_worker_zombies", lambda: 0)
        spawned: list[str] = []

        def record_spawn(task, _workspace, board=None):
            spawned.append(task.id)
            return None

        kb.dispatch_once(conn, spawn_fn=record_spawn, reconcile_orphans=False)

        assert conflict_id not in spawned
        conflict = kb.get_task(conn, conflict_id)
        assert conflict is not None
        assert conflict.status == "blocked"
        assert conflict.current_run_id is None


def test_cli_can_set_and_list_board_scoped_protected_workspace_policy(
    isolated_kanban_home: Path,
    tmp_path: Path,
) -> None:
    root = tmp_path / "cli-root"
    root.mkdir()
    with kb.connect() as conn:
        owner_id = kb.create_task(
            conn,
            title="CLI root owner",
            workspace_kind="dir",
            workspace_path=str(root),
        )

    output = kanban_cli.run_slash(
        f"protect-workspace set '{root}' --owner {owner_id}"
    )
    assert f"Protected {root}" in output
    policies = json.loads(kanban_cli.run_slash("protect-workspace list --json"))
    assert policies == [
        {
            "normalized_root": kb.normalize_workspace_root(root),
            "root_path": str(root.resolve()),
            "authorized_task_id": owner_id,
            "authorized_title": "CLI root owner",
            "authorized_workspace_kind": "dir",
            "is_git_root": False,
        }
    ]


def test_cli_can_allow_exact_task_and_remove_policy(
    isolated_kanban_home: Path,
    tmp_path: Path,
) -> None:
    root = tmp_path / "cli-allow-root"
    root.mkdir()
    with kb.connect() as conn:
        owner_id = kb.create_task(
            conn,
            title="CLI allow owner",
            workspace_kind="dir",
            workspace_path=str(root),
        )
        helper_id = kb.create_task(
            conn,
            title="CLI allow helper",
            workspace_kind="dir",
            workspace_path=str(root),
        )

    kanban_cli.run_slash(f"protect-workspace set '{root}' --owner {owner_id}")
    allowed = kanban_cli.run_slash(
        f"protect-workspace allow '{root}' --task {helper_id}"
    )
    assert f"Allowlisted {helper_id}" in allowed
    removed = kanban_cli.run_slash(f"protect-workspace remove '{root}'")
    assert f"Removed protected workspace policy for {root}" in removed
    assert json.loads(kanban_cli.run_slash("protect-workspace list --json")) == []
    with kb.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM protected_workspace_allowlist"
        ).fetchone()[0] == 0


def test_protected_non_git_root_blocks_worktree_before_claim(
    isolated_kanban_home: Path,
    tmp_path: Path,
) -> None:
    root = tmp_path / "plain-directory"
    root.mkdir()
    with kb.connect() as conn:
        owner_id = kb.create_task(
            conn,
            title="plain root owner",
            workspace_kind="dir",
            workspace_path=str(root),
        )
        kb.protect_workspace(conn, root, authorized_task_id=owner_id)
        worktree_id = kb.create_task(
            conn,
            title="cannot isolate without git",
            workspace_kind="worktree",
            workspace_path=str(root),
        )

        assert kb.claim_task(conn, worktree_id) is None
        task = kb.get_task(conn, worktree_id)
        assert task is not None
        assert task.status == "blocked"
        assert task.current_run_id is None


def test_unprotected_shared_dir_claim_semantics_are_unchanged(
    isolated_kanban_home: Path,
    tmp_path: Path,
) -> None:
    shared = tmp_path / "ordinary-shared-directory"
    shared.mkdir()
    with kb.connect() as conn:
        first = kb.create_task(
            conn,
            title="ordinary first",
            workspace_kind="dir",
            workspace_path=str(shared),
        )
        second = kb.create_task(
            conn,
            title="ordinary second",
            workspace_kind="dir",
            workspace_path=str(shared),
        )

        assert kb.claim_task(conn, first) is not None
        assert kb.claim_task(conn, second) is not None


def test_embedded_dispatch_materializes_redirected_worktree_before_spawn(
    isolated_kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hermes_cli.profiles as profiles_module

    repo = _make_git_repo(tmp_path)
    with kb.connect() as conn:
        owner_id = kb.create_task(
            conn,
            title="dispatch git owner",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        kb.protect_workspace(conn, repo, authorized_task_id=owner_id)
        worktree_id = kb.create_task(
            conn,
            title="dispatch isolated worktree",
            assignee="worker",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        monkeypatch.setattr(profiles_module, "profile_exists", lambda _name: True)
        monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "ok")
        monkeypatch.setattr(kb, "reap_worker_zombies", lambda: 0)
        spawned: list[tuple[str, Path]] = []

        def record_spawn(task, workspace, board=None):
            spawned.append((task.id, Path(workspace).resolve()))
            return None

        kb.dispatch_once(conn, spawn_fn=record_spawn, reconcile_orphans=False)

        expected = (repo / ".worktrees" / worktree_id).resolve()
        assert spawned == [(worktree_id, expected)]
        assert expected.exists()
        task = kb.get_task(conn, worktree_id)
        assert task is not None and task.status == "running"
        assert Path(task.workspace_path).resolve() == expected


def test_setting_policy_reports_preexisting_multiple_live_root_tasks(
    isolated_kanban_home: Path,
    tmp_path: Path,
) -> None:
    root = tmp_path / "preexisting-conflict"
    root.mkdir()
    with kb.connect() as conn:
        owner_id = kb.create_task(
            conn,
            title="preexisting owner",
            workspace_kind="dir",
            workspace_path=str(root),
        )
        duplicate_id = kb.create_task(
            conn,
            title="preexisting duplicate",
            workspace_kind="dir",
            workspace_path=str(root),
        )

        kb.protect_workspace(conn, root, authorized_task_id=owner_id)

        event = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'protected_workspace_conflict' "
            "ORDER BY id DESC LIMIT 1",
            (owner_id,),
        ).fetchone()
        assert event is not None
        payload = json.loads(event["payload"])
        assert payload["severity"] == "critical"
        assert payload["conflicting_task_ids"] == [owner_id, duplicate_id]


def test_creating_new_live_root_conflict_emits_critical_event_immediately(
    isolated_kanban_home: Path,
    tmp_path: Path,
) -> None:
    root = tmp_path / "new-conflict"
    root.mkdir()
    with kb.connect() as conn:
        owner_id = kb.create_task(
            conn,
            title="new conflict owner",
            workspace_kind="dir",
            workspace_path=str(root),
        )
        kb.protect_workspace(conn, root, authorized_task_id=owner_id)

        duplicate_id = kb.create_task(
            conn,
            title="new conflict duplicate",
            workspace_kind="dir",
            workspace_path=str(root),
        )

        event = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'protected_workspace_conflict' "
            "ORDER BY id DESC LIMIT 1",
            (duplicate_id,),
        ).fetchone()
        assert event is not None
        payload = json.loads(event["payload"])
        assert payload["severity"] == "critical"
        assert payload["conflicting_task_ids"] == [owner_id, duplicate_id]


def test_persisting_workspace_path_into_protected_root_emits_conflict(
    isolated_kanban_home: Path,
    tmp_path: Path,
) -> None:
    root = tmp_path / "persisted-conflict"
    elsewhere = tmp_path / "elsewhere"
    root.mkdir()
    elsewhere.mkdir()
    with kb.connect() as conn:
        owner_id = kb.create_task(
            conn,
            title="persisted conflict owner",
            workspace_kind="dir",
            workspace_path=str(root),
        )
        kb.protect_workspace(conn, root, authorized_task_id=owner_id)
        duplicate_id = kb.create_task(
            conn,
            title="persisted conflict duplicate",
            workspace_kind="dir",
            workspace_path=str(elsewhere),
        )

        kb.set_workspace_path(conn, duplicate_id, root)

        event = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'protected_workspace_conflict' "
            "ORDER BY id DESC LIMIT 1",
            (duplicate_id,),
        ).fetchone()
        assert event is not None
        assert json.loads(event["payload"])["conflicting_task_ids"] == [
            owner_id,
            duplicate_id,
        ]


def test_stale_claim_lock_does_not_emit_false_redirect_or_block_events(
    isolated_kanban_home: Path,
    tmp_path: Path,
) -> None:
    root = tmp_path / "stale-lock"
    root.mkdir()
    with kb.connect() as conn:
        owner_id = kb.create_task(
            conn,
            title="stale lock owner",
            workspace_kind="dir",
            workspace_path=str(root),
        )
        kb.protect_workspace(conn, root, authorized_task_id=owner_id)
        duplicate_id = kb.create_task(
            conn,
            title="stale lock duplicate",
            workspace_kind="dir",
            workspace_path=str(root),
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET claim_lock='stale-lock' WHERE id=?",
                (duplicate_id,),
            )

        assert kb.claim_task(conn, duplicate_id) is None
        duplicate = kb.get_task(conn, duplicate_id)
        assert duplicate is not None and duplicate.status == "ready"
        kinds = [event.kind for event in kb.list_events(conn, duplicate_id)]
        assert "protected_workspace_redirected" not in kinds
        assert "protected_workspace_claim_rejected" not in kinds
        assert "blocked" not in kinds


def test_refusal_reclaims_leaked_prior_run_without_creating_new_run(
    isolated_kanban_home: Path,
    tmp_path: Path,
) -> None:
    root = tmp_path / "leaked-run"
    root.mkdir()
    with kb.connect() as conn:
        owner_id = kb.create_task(
            conn,
            title="leaked run owner",
            workspace_kind="dir",
            workspace_path=str(root),
        )
        kb.protect_workspace(conn, root, authorized_task_id=owner_id)
        duplicate_id = kb.create_task(
            conn,
            title="leaked run duplicate",
            workspace_kind="dir",
            workspace_path=str(root),
        )
        with kb.write_txn(conn):
            run_id = conn.execute(
                "INSERT INTO task_runs (task_id, status, started_at) "
                "VALUES (?, 'running', 1)",
                (duplicate_id,),
            ).lastrowid
            conn.execute(
                "UPDATE tasks SET current_run_id=? WHERE id=?",
                (run_id, duplicate_id),
            )

        assert kb.claim_task(conn, duplicate_id) is None
        duplicate = kb.get_task(conn, duplicate_id)
        assert duplicate is not None
        assert duplicate.status == "blocked"
        assert duplicate.current_run_id is None
        runs = conn.execute(
            "SELECT status, outcome, ended_at FROM task_runs WHERE task_id=?",
            (duplicate_id,),
        ).fetchall()
        assert len(runs) == 1
        assert runs[0]["status"] == "reclaimed"
        assert runs[0]["outcome"] == "reclaimed"
        assert runs[0]["ended_at"] is not None


def test_repeated_policy_refusal_uses_existing_block_loop_accounting(
    isolated_kanban_home: Path,
    tmp_path: Path,
) -> None:
    root = tmp_path / "block-loop"
    root.mkdir()
    with kb.connect() as conn:
        owner_id = kb.create_task(
            conn,
            title="block loop owner",
            workspace_kind="dir",
            workspace_path=str(root),
        )
        kb.protect_workspace(conn, root, authorized_task_id=owner_id)
        duplicate_id = kb.create_task(
            conn,
            title="block loop duplicate",
            workspace_kind="dir",
            workspace_path=str(root),
        )

        assert kb.claim_task(conn, duplicate_id) is None
        first = kb.get_task(conn, duplicate_id)
        assert first is not None
        assert first.status == "blocked"
        assert first.block_recurrences == 1
        assert kb.unblock_task(conn, duplicate_id) is True
        assert kb.claim_task(conn, duplicate_id) is None

        second = kb.get_task(conn, duplicate_id)
        assert second is not None
        assert second.status == "triage"
        assert second.block_recurrences == kb.BLOCK_RECURRENCE_LIMIT
        assert any(
            event.kind == "block_loop_detected"
            for event in kb.list_events(conn, duplicate_id)
        )


def test_policy_captures_owner_tuple_inside_write_transaction(
    isolated_kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "policy-toctou"
    root.mkdir()
    with kb.connect() as conn:
        owner_id = kb.create_task(
            conn,
            title="owner before transaction",
            workspace_kind="dir",
            workspace_path=str(root),
        )

        def mutate_owner_before_policy_write(_path):
            with kb.connect() as other:
                other.execute(
                    "UPDATE tasks SET title='owner inside transaction' WHERE id=?",
                    (owner_id,),
                )
                other.commit()
            return None

        monkeypatch.setattr(kb, "_git_toplevel", mutate_owner_before_policy_write)
        policy = kb.protect_workspace(conn, root, authorized_task_id=owner_id)
        assert policy["authorized_title"] == "owner inside transaction"


def test_allowlist_captures_task_tuple_inside_write_transaction(
    isolated_kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "allowlist-toctou"
    root.mkdir()
    with kb.connect() as conn:
        owner_id = kb.create_task(
            conn,
            title="allowlist owner",
            workspace_kind="dir",
            workspace_path=str(root),
        )
        kb.protect_workspace(conn, root, authorized_task_id=owner_id)
        helper_id = kb.create_task(
            conn,
            title="helper before transaction",
            workspace_kind="dir",
            workspace_path=str(root),
        )
        original_write_txn = kb.write_txn
        interleaved = False

        @contextmanager
        def interleaving_write_txn(target_conn, *args, **kwargs):
            nonlocal interleaved
            if not interleaved:
                interleaved = True
                with kb.connect() as other:
                    other.execute(
                        "UPDATE tasks SET title='helper inside transaction' WHERE id=?",
                        (helper_id,),
                    )
                    other.commit()
            with original_write_txn(target_conn, *args, **kwargs):
                yield

        monkeypatch.setattr(kb, "write_txn", interleaving_write_txn)
        allowed = kb.allow_task_at_protected_workspace(
            conn,
            root,
            task_id=helper_id,
        )
        assert allowed["title"] == "helper inside transaction"
