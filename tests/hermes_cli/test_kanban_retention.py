from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_retention as kr

NOW = 1_800_000_000
FREE = 35 * kr.GIB


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return root


def _add_task(
    home: Path,
    task_id: str,
    *,
    status: str = "done",
    run_status: str | None = None,
    run_outcome: str | None = None,
    age: int = 100 * 3600,
    worker_pid: int | None = None,
    heartbeat: int | None = None,
    claim_expires: int | None = None,
    kind: str = "scratch",
    path: Path | None = None,
) -> Path:
    ws = path or (home / "kanban" / "workspaces" / task_id)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "payload.txt").write_text("reproducible\n", encoding="utf-8")
    old = NOW - age
    conn = sqlite3.connect(home / "kanban.db")
    try:
        conn.execute(
            "INSERT INTO tasks "
            "(id,title,status,created_at,completed_at,workspace_kind,workspace_path,"
            "worker_pid,last_heartbeat_at,claim_expires) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (task_id, task_id, status, old - 100, old if status in {"done", "archived", "failed", "cancelled"} else None,
             kind, str(ws), worker_pid, heartbeat if heartbeat is not None else old, claim_expires),
        )
        if run_status:
            cur = conn.execute(
                "INSERT INTO task_runs "
                "(task_id,status,started_at,ended_at,outcome,last_heartbeat_at,worker_pid,claim_expires) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (task_id, run_status, old - 60, old, run_outcome or run_status, old, worker_pid, claim_expires),
            )
            conn.execute("UPDATE tasks SET current_run_id=NULL WHERE id=?", (task_id,))
        conn.commit()
    finally:
        conn.close()
    return ws


def _sweep(home: Path, *, apply: bool = True, policy: kr.Policy | None = None, **kwargs):
    return kr.sweep(
        home=home,
        policy=policy or kr.Policy(max_reclaimed_bytes=100 * kr.GIB),
        apply=apply,
        now=NOW,
        activity_probe=kwargs.pop("activity_probe", lambda _p, _t: (False, None)),
        free_probe=kwargs.pop("free_probe", lambda _p: FREE),
        **kwargs,
    )


@pytest.mark.parametrize(
    ("status", "run_status", "run_outcome"),
    [("done", None, None), ("archived", None, None),
     ("failed", None, None), ("cancelled", None, None),
     ("blocked", "blocked", "gave_up"),
     ("blocked", "timed_out", "timed_out"),
     ("blocked", "crashed", "interrupted"),
     ("blocked", "reclaimed", "reclaimed"),
     ("blocked", "stale", "unknown")],
)
def test_terminal_states_remove_clean_expired_workspace(
    home: Path, status: str, run_status: str | None, run_outcome: str | None
) -> None:
    ws = _add_task(
        home, "t_a1b2c3d4", status=status,
        run_status=run_status, run_outcome=run_outcome,
    )
    report, code = _sweep(home)
    assert code == 0
    assert report["removed"] == 1
    assert report["terminal_backlog_count"] == 0
    assert not ws.exists()


def test_nonterminal_and_unexpired_are_preserved(home: Path) -> None:
    active = _add_task(home, "t_11111111", status="todo")
    recent = _add_task(home, "t_22222222", status="done", age=60)
    report, code = _sweep(home)
    assert code == 0
    assert active.exists() and recent.exists()
    assert report["removed"] == 0
    assert report["skipped_by_reason"]["nonterminal"] == 1
    assert report["skipped_by_reason"]["ttl"] == 1


@pytest.mark.parametrize(
    ("updates", "reason"),
    [({"worker_pid": os.getpid()}, "active_task"),
     ({"heartbeat": NOW - 10}, "active_heartbeat"),
     ({"claim_expires": NOW + 60}, "active_lease")],
)
def test_active_pid_heartbeat_and_lease_preserved(home: Path, updates: dict, reason: str) -> None:
    ws = _add_task(home, "t_33333333", **updates)
    report, code = _sweep(home)
    assert code == 0 and ws.exists()
    assert report["skipped_by_reason"][reason] == 1


def test_open_fd_or_cwd_preserved(home: Path) -> None:
    ws = _add_task(home, "t_44444444")
    report, code = _sweep(home, activity_probe=lambda _p, _t: (True, None))
    assert code == 2 and ws.exists()
    assert report["skipped_by_reason"]["open_fd_or_cwd"] == 1


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=False)


def _repo_workspace(home: Path, task_id: str) -> tuple[Path, Path]:
    origin = home.parent / f"{task_id}.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    ws = home / "kanban" / "workspaces" / task_id
    ws.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", str(origin), str(ws)], check=True, capture_output=True)
    _git("config", "user.email", "test@example.invalid", cwd=ws)
    _git("config", "user.name", "test", cwd=ws)
    (ws / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=ws)
    _git("commit", "-m", "base", cwd=ws)
    _git("push", "origin", "HEAD", cwd=ws)
    old = NOW - 100 * 3600
    conn = sqlite3.connect(home / "kanban.db")
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,completed_at,workspace_kind,workspace_path,last_heartbeat_at) "
        "VALUES (?,?,?,?,?,'scratch',?,?)",
        (task_id, task_id, "done", old - 10, old, str(ws), old),
    )
    conn.commit(); conn.close()
    return ws, origin


@pytest.mark.parametrize(("mutation", "reason"), [
    ("dirty", "git_dirty"), ("untracked", "git_untracked"), ("unpushed", "git_unpushed"),
    ("detached", "git_detached"),
])
def test_git_unique_work_preserved(home: Path, mutation: str, reason: str) -> None:
    ws, _ = _repo_workspace(home, "t_55555555")
    if mutation == "dirty":
        (ws / "tracked.txt").write_text("changed\n", encoding="utf-8")
    elif mutation == "untracked":
        (ws / "unique.txt").write_text("unique\n", encoding="utf-8")
    elif mutation == "unpushed":
        (ws / "tracked.txt").write_text("next\n", encoding="utf-8")
        _git("add", "tracked.txt", cwd=ws); _git("commit", "-m", "local", cwd=ws)
    else:
        _git("checkout", "--detach", cwd=ws)
    report, code = _sweep(home)
    assert code == 2 and ws.exists()
    assert report["skipped_by_reason"][reason] == 1


def test_clean_remote_reachable_git_workspace_removed(home: Path) -> None:
    ws, _ = _repo_workspace(home, "t_66666666")
    report, code = _sweep(home)
    assert code == 0 and not ws.exists() and report["removed"] == 1


def test_symlink_escape_and_nested_mount_preserved(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _add_task(home, "t_77777777")
    (ws / "escape").symlink_to(home.parent)
    report, code = _sweep(home)
    assert code == 2 and ws.exists()
    assert report["skipped_by_reason"]["symlink_escape"] == 1

    (ws / "escape").unlink()
    nested = ws / "mounted"; nested.mkdir()
    real_ismount = kr.os.path.ismount
    monkeypatch.setattr(kr.os.path, "ismount", lambda p: Path(p) == nested or real_ismount(p))
    report, code = _sweep(home)
    assert code == 2 and ws.exists()
    assert report["skipped_by_reason"]["nested_mount"] == 1


def test_read_only_nested_directory_is_preserved_before_any_mutation(home: Path) -> None:
    workspace = _add_task(home, "t_readonly")
    protected = workspace / "protected"
    protected.mkdir()
    marker = protected / "marker.txt"
    marker.write_text("keep\n", encoding="utf-8")
    protected.chmod(0o555)
    try:
        report, code = _sweep(home)
    finally:
        protected.chmod(0o755)

    assert code == 2
    assert report["removed"] == 0
    assert report["skipped_by_reason"]["delete_permission"] == 1
    assert marker.read_text(encoding="utf-8") == "keep\n"


def test_changed_identity_and_partial_removal_are_unhealthy(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _add_task(home, "t_88888888")
    original = kr._inspect_tree
    calls = 0
    def changed(path, root):
        nonlocal calls
        calls += 1
        info = original(path, root)
        if calls >= 2:
            info.ino += 1
        return info
    monkeypatch.setattr(kr, "_inspect_tree", changed)
    report, code = _sweep(home)
    assert code == 2 and ws.exists()
    assert report["skipped_by_reason"]["changed_path_identity"] == 1

    monkeypatch.setattr(kr, "_inspect_tree", original)
    monkeypatch.setattr(kr, "_remove_tree_exact", lambda *_a: (False, "partial_removal"))
    report, code = _sweep(home)
    assert code == 2 and "partial_removal" in report["unhealthy_reasons"]


def test_overlap_lock_refuses_without_scanning(home: Path) -> None:
    lock = home / "kanban" / "retention.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        report, code = _sweep(home)
    assert code == 3
    assert report["lock_state"] == "overlap"


def test_dry_run_and_protected_backlog_cannot_report_success(home: Path) -> None:
    ws = _add_task(home, "t_99999999")
    report, code = _sweep(home, apply=False)
    assert code == 2 and not report["healthy"] and ws.exists()
    assert report["eligible"] == 1 and report["terminal_backlog_count"] == 1
    assert "eligible_backlog" in report["unhealthy_reasons"]
    assert not (home / "kanban" / "retention" / "state.json").exists()


def test_free_floor_hysteresis_and_repeated_growth(home: Path) -> None:
    policy = kr.Policy(free_floor_bytes=25 * kr.GIB, free_release_bytes=30 * kr.GIB,
                       workspace_cap_bytes=50 * kr.GIB, workspace_release_bytes=40 * kr.GIB)
    report, code = _sweep(home, policy=policy, free_probe=lambda _p: 20 * kr.GIB)
    assert code == 2 and report["pressure_active"]
    assert "free_floor_missed" in report["unhealthy_reasons"]
    state = home / "kanban" / "retention" / "state.json"
    kr._atomic_json(state, {"workspace_bytes_after": 1, "growth_streak": 1, "pressure_active": True})
    _add_task(home, "t_abababab", status="todo")
    report, code = _sweep(home, policy=policy)
    assert code == 2 and report["repeated_growth"]
    assert "repeated_growth_exceeds_cleanup" in report["unhealthy_reasons"]


def test_receipt_exactly_once_and_no_secret_or_absolute_path(home: Path) -> None:
    ws = _add_task(home, "t_cdcdcdcd")
    report, code = _sweep(home)
    assert code == 0 and report["receipts_created"] == 1 and not ws.exists()
    receipts = list((home / "kanban" / "retention" / "receipts").glob("*.json"))
    assert len(receipts) == 1
    text = receipts[0].read_text(encoding="utf-8")
    assert str(home) not in text
    assert "password" not in text.lower() and "token" not in text.lower()
    # Re-running is idempotent: no workspace, no second receipt, no removal.
    report2, code2 = _sweep(home)
    assert code2 == 0 and report2["receipts_created"] == 0 and report2["removed"] == 0
    assert len(list(receipts[0].parent.glob("*.json"))) == 1


def test_oldest_eligible_ordering_and_bound(home: Path) -> None:
    old = _add_task(home, "t_eeeeeeee", age=200 * 3600)
    new = _add_task(home, "t_ffffffff", age=100 * 3600)
    policy = kr.Policy(max_removals=1, max_reclaimed_bytes=100 * kr.GIB)
    report, code = _sweep(home, policy=policy)
    assert code == 2
    assert not old.exists() and new.exists()
    assert report["removed"] == 1 and report["skipped_by_reason"]["sweep_bound"] == 1


def test_activity_probe_uncertainty_marks_partial_inventory(home: Path) -> None:
    ws = _add_task(home, "t_acacacac")
    report, code = _sweep(home, activity_probe=lambda _p, _t: (False, "activity_probe_timeout"))
    assert code == 2 and ws.exists() and report["inventory_partial"]
    assert "inventory_partial" in report["unhealthy_reasons"]


def test_production_blocked_status_outcome_shape_is_eligible(home: Path) -> None:
    ws = _add_task(
        home, "t_bcbcbcbc", status="blocked",
        run_status="blocked", run_outcome="gave_up",
    )
    report, code = _sweep(home, apply=False)
    assert code == 2 and ws.exists()
    assert report["eligible"] == 1
    assert report["terminal_backlog_count"] == 1


def test_registered_worktree_removed_without_force_and_branch_preserved(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = home.parent / "retention-origin.git"
    project = home.parent / "project"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(origin), str(project)], check=True, capture_output=True)
    _git("config", "user.email", "test@example.invalid", cwd=project)
    _git("config", "user.name", "test", cwd=project)
    (project / "base.txt").write_text("base\n", encoding="utf-8")
    _git("add", "base.txt", cwd=project); _git("commit", "-m", "base", cwd=project)
    _git("push", "origin", "HEAD", cwd=project)
    task_id = "t_dededede"
    branch = f"wt/{task_id}"
    wt = project / ".worktrees" / task_id
    kb._ensure_git_worktree(project, wt, branch)
    old = NOW - 100 * 3600
    conn = sqlite3.connect(home / "kanban.db")
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,completed_at,workspace_kind,workspace_path,branch_name,last_heartbeat_at) "
        "VALUES (?,?,?,?,?,'worktree',?,?,?)",
        (task_id, task_id, "done", old - 10, old, str(wt), branch, old),
    )
    conn.commit(); conn.close()
    real_run = kr._run
    removal_commands: list[list[str]] = []
    def spy(args, **kwargs):
        if "worktree" in args and "remove" in args:
            removal_commands.append(list(args))
        return real_run(args, **kwargs)
    monkeypatch.setattr(kr, "_run", spy)
    report, code = _sweep(home)
    assert code == 0 and report["removed"] == 1 and not wt.exists(), report
    assert removal_commands and all("--force" not in cmd and "-f" not in cmd for cmd in removal_commands)
    assert _git("branch", "--list", branch, cwd=project).stdout.strip()


def test_receipt_collision_fails_closed(home: Path) -> None:
    ws = _add_task(home, "t_efefefef")
    terminal_at = NOW - 100 * 3600
    receipt = home / "kanban" / "retention" / "receipts" / f"t_efefefef-{terminal_at}.json"
    kr._atomic_json(receipt, {"task_id": "different", "run_id": None})
    report, code = _sweep(home)
    assert code == 2 and ws.exists()
    assert report["skipped_by_reason"]["receipt_failure"] == 1
