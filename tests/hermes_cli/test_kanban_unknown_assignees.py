from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli.profiles import create_profile


pytestmark = pytest.mark.real_kanban_profile_registry


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb.init_db()
    return home


def _install_profile(home: Path, name: str) -> Path:
    profile = home / "profiles" / name
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "config.yaml").write_text("{}\n", encoding="utf-8")
    return profile


def _insert_unknown(conn, task_id: str, *, assignee: str, status: str = "ready") -> None:
    conn.execute(
        "INSERT INTO tasks (id,title,assignee,status,priority,created_at,workspace_kind) "
        "VALUES (?,?,?,?,0,1,'scratch')",
        (task_id, task_id, assignee, status),
    )
    conn.execute(
        "INSERT INTO task_events(task_id,kind,payload,created_at) VALUES(?,?,?,1)",
        (task_id, "created", json.dumps({"assignee": assignee, "status": status})),
    )
    conn.commit()


def test_shared_create_boundary_rejects_unknown_profile_without_inserting(kanban_home):
    with kb.connect_closing() as conn:
        before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        with pytest.raises(kb.UnknownKanbanAssigneeError) as excinfo:
            kb.create_task(conn, title="bad route", assignee="worker")
        after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

    assert before == after
    message = str(excinfo.value)
    assert "unknown Kanban assignee 'worker'" in message
    assert "hermes profile list" in message
    assert "hermes profile create worker" in message
    assert "never treats generic words" in message


@pytest.mark.parametrize("junk_file", [None, "scratch.txt"])
def test_incomplete_profile_directory_is_rejected_and_quarantined(
    kanban_home, junk_file
):
    incomplete = kanban_home / "profiles" / "incomplete"
    incomplete.mkdir(parents=True)
    if junk_file is not None:
        (incomplete / junk_file).write_text("not a profile\n", encoding="utf-8")

    with kb.connect_closing() as conn:
        with pytest.raises(kb.UnknownKanbanAssigneeError):
            kb.create_task(conn, title="bad route", assignee="incomplete")

        _insert_unknown(conn, "t_incomplete", assignee="incomplete")
        quarantined = kb.quarantine_unknown_assignees(conn)
        task = kb.get_task(conn, "t_incomplete")

    assert quarantined == [("t_incomplete", "incomplete", "ready")]
    assert task is not None
    assert task.status == "triage"


def test_fresh_profile_without_config_is_still_an_installed_assignee(kanban_home):
    profile = create_profile("fresh", no_alias=True)

    assert not (profile / "config.yaml").exists()
    assert (profile / ".env").is_file()
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="fresh route", assignee="fresh")
        task = kb.get_task(conn, task_id)

    assert task is not None
    assert task.assignee == "fresh"


def test_shared_boundary_preserves_default_and_installed_named_profiles(kanban_home):
    _install_profile(kanban_home, "engineer")
    with kb.connect_closing() as conn:
        default_id = kb.create_task(conn, title="default", assignee="Default")
        named_id = kb.create_task(conn, title="named", assignee="Engineer")

        assert kb.get_task(conn, default_id).assignee == "default"
        assert kb.get_task(conn, named_id).assignee == "engineer"


def test_auto_decomposer_rejects_unknown_child_atomically(kanban_home):
    _install_profile(kanban_home, "orchestrator")
    with kb.connect_closing() as conn:
        root = kb.create_task(conn, title="root", assignee="orchestrator", triage=True)
        with pytest.raises(kb.UnknownKanbanAssigneeError):
            kb.decompose_triage_task(
                conn,
                root,
                root_assignee="orchestrator",
                children=[{"title": "child", "assignee": "worker"}],
            )
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert kb.get_task(conn, root).status == "triage"


def test_dispatch_quarantines_profile_removed_after_creation_without_spawning(kanban_home):
    profile = _install_profile(kanban_home, "ephemeral")
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="race", assignee="ephemeral")
        shutil.rmtree(profile)
        spawned = []

        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *args, **kwargs: spawned.append(args),
        )

        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)

    assert spawned == []
    assert result.unknown_assignee_quarantined == [(task_id, "ephemeral", "ready")]
    assert task.status == "triage"
    assert task.assignee == "ephemeral"
    assert any(event.kind == "unknown_assignee_quarantined" for event in events)


def test_quarantine_reuses_one_fresh_profile_snapshot_for_all_rows(
    kanban_home, monkeypatch
):
    original_list_profiles = kb.list_profiles_on_disk
    scan_count = 0
    transaction_states = []

    def reinstall_after_outer_scan():
        nonlocal scan_count
        scan_count += 1
        transaction_states.append(conn.in_transaction)
        installed = original_list_profiles()
        if scan_count == 1:
            _install_profile(kanban_home, "recovered")
        return installed

    monkeypatch.setattr(kb, "list_profiles_on_disk", reinstall_after_outer_scan)
    with kb.connect_closing() as conn:
        _insert_unknown(conn, "t_missing", assignee="missing")
        _insert_unknown(conn, "t_recovered", assignee="recovered")

        quarantined = kb.quarantine_unknown_assignees(conn)
        missing = kb.get_task(conn, "t_missing")
        recovered = kb.get_task(conn, "t_recovered")

    assert scan_count == 2
    assert transaction_states == [False, True]
    assert quarantined == [("t_missing", "missing", "ready")]
    assert missing is not None
    assert missing.status == "triage"
    assert recovered is not None
    assert recovered.status == "ready"


def test_quarantine_cas_preserves_claimed_rerouted_and_moved_cards(
    kanban_home, monkeypatch
):
    original_list_profiles = kb.list_profiles_on_disk
    scan_count = 0

    def mutate_candidates_during_fresh_scan():
        nonlocal scan_count
        scan_count += 1
        if scan_count == 2:
            assert conn.in_transaction
            conn.execute(
                "UPDATE tasks SET claim_lock = 'worker' WHERE id = 't_claimed'"
            )
            conn.execute(
                "UPDATE tasks SET assignee = 'rerouted' WHERE id = 't_rerouted'"
            )
            conn.execute(
                "UPDATE tasks SET status = 'running' WHERE id = 't_moved'"
            )
        return original_list_profiles()

    monkeypatch.setattr(
        kb, "list_profiles_on_disk", mutate_candidates_during_fresh_scan
    )
    with kb.connect_closing() as conn:
        _insert_unknown(conn, "t_claimed", assignee="missing")
        _insert_unknown(conn, "t_rerouted", assignee="missing")
        _insert_unknown(conn, "t_moved", assignee="missing")

        quarantined = kb.quarantine_unknown_assignees(conn)
        claimed = kb.get_task(conn, "t_claimed")
        rerouted = kb.get_task(conn, "t_rerouted")
        moved = kb.get_task(conn, "t_moved")
        quarantine_events = [
            event
            for task_id in ("t_claimed", "t_rerouted", "t_moved")
            for event in kb.list_events(conn, task_id)
            if event.kind == "unknown_assignee_quarantined"
        ]

    assert quarantined == []
    assert claimed is not None
    assert claimed.claim_lock == "worker"
    assert claimed.status == "ready"
    assert rerouted is not None
    assert rerouted.assignee == "rerouted"
    assert rerouted.status == "ready"
    assert moved is not None
    assert moved.status == "running"
    assert quarantine_events == []


def test_explicit_repair_reassigns_and_restores_quarantined_lane(kanban_home):
    _install_profile(kanban_home, "engineer")
    with kb.connect_closing() as conn:
        _insert_unknown(conn, "t_missing", assignee="worker")
        kb.quarantine_unknown_assignees(conn)

        assert kb.repair_unknown_assignee_task(
            conn, "t_missing", target_profile="engineer"
        )
        task = kb.get_task(conn, "t_missing")
        events = kb.list_events(conn, "t_missing")

    assert task.assignee == "engineer"
    assert task.status == "ready"
    assert any(event.kind == "unknown_assignee_repaired" for event in events)


def test_explicit_archive_is_terminal_and_never_reassigns(kanban_home):
    with kb.connect_closing() as conn:
        _insert_unknown(conn, "t_archive", assignee="agent")
        assert kb.repair_unknown_assignee_task(conn, "t_archive", archive=True)
        task = kb.get_task(conn, "t_archive")

    assert task.status == "archived"
    assert task.assignee == "agent"


def test_delegated_child_cli_rejects_assignee_archive_before_mutation(
    kanban_home, capsys
):
    from agent.delegation_context import delegated_child_context

    with kb.connect_closing() as conn:
        _insert_unknown(conn, "t_archive", assignee="agent")
        task_before = kb.get_task(conn, "t_archive")
        events_before = kb.list_events(conn, "t_archive")

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    kanban_cli.build_parser(subparsers)
    args = parser.parse_args(
        ["kanban", "repair-assignees", "--task", "t_archive", "--archive"]
    )
    with delegated_child_context():
        result = kanban_cli.kanban_command(args)

    assert result == 1
    assert (
        "delegate_task child contexts cannot mutate Kanban tasks via the CLI"
        in capsys.readouterr().err
    )
    with kb.connect_closing() as conn:
        task_after = kb.get_task(conn, "t_archive")
        events_after = kb.list_events(conn, "t_archive")

    assert task_before is not None
    assert task_after == task_before
    assert events_after == events_before


def test_cross_board_report_uses_qualified_ids_and_requires_explicit_action(
    kanban_home, capsys
):
    kb.create_board("dedicated", name="Dedicated")
    with kb.connect_closing(board="default") as conn:
        _insert_unknown(conn, "t_default_bad", assignee="worker")
    with kb.connect_closing(board="dedicated") as conn:
        _insert_unknown(conn, "t_dedicated_bad", assignee="agent")

    args = argparse.Namespace(
        all_boards=True,
        board=None,
        repair_tasks=[],
        repair_profile=None,
        repair_archive=False,
        json=False,
    )
    assert kanban_cli._cmd_repair_assignees(args) == 0
    output = capsys.readouterr().out

    assert "default/t_default_bad" in output
    assert "dedicated/t_dedicated_bad" in output
    assert "Read-only report" in output
    with kb.connect_closing(board="default") as conn:
        assert kb.get_task(conn, "t_default_bad").status == "ready"
    with kb.connect_closing(board="dedicated") as conn:
        assert kb.get_task(conn, "t_dedicated_bad").status == "ready"


def test_cross_board_repair_requires_qualified_selection_and_does_not_guess(
    kanban_home, capsys
):
    _install_profile(kanban_home, "engineer")
    kb.create_board("dedicated", name="Dedicated")
    with kb.connect_closing(board="default") as conn:
        _insert_unknown(conn, "t_bad", assignee="worker")
    with kb.connect_closing(board="dedicated") as conn:
        _insert_unknown(conn, "t_bad", assignee="agent")

    args = argparse.Namespace(
        all_boards=True,
        board=None,
        repair_tasks=["dedicated/t_bad"],
        repair_profile="engineer",
        repair_archive=False,
        json=False,
    )
    assert kanban_cli._cmd_repair_assignees(args) == 0
    capsys.readouterr()

    with kb.connect_closing(board="default") as conn:
        task = kb.get_task(conn, "t_bad")
        assert task is not None
        assert task.assignee == "worker"
    with kb.connect_closing(board="dedicated") as conn:
        task = kb.get_task(conn, "t_bad")
        assert task is not None
        assert task.assignee == "engineer"


def test_all_board_repair_ignores_single_board_db_pin_without_cross_board_ambiguity(
    kanban_home, monkeypatch, capsys
):
    """A profile .env DB pin cannot collapse an all-board scan to one DB."""
    _install_profile(kanban_home, "engineer")
    kb.create_board("alpha", name="Alpha")
    kb.create_board("beta", name="Beta")
    for board, assignee in (
        ("default", "default-missing"),
        ("alpha", "alpha-missing"),
        ("beta", "beta-missing"),
    ):
        with kb.connect_closing(db_path=kb.canonical_kanban_db_path(board)) as conn:
            _insert_unknown(conn, "t_same", assignee=assignee)

    # Reproduce the live profile .env pollution: every ordinary board= lookup
    # now resolves to the default DB, but --all-boards must bypass this pin.
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.canonical_kanban_db_path("default")))
    args = argparse.Namespace(
        all_boards=True,
        board=None,
        repair_tasks=["beta/t_same"],
        repair_profile="engineer",
        repair_archive=False,
        json=True,
    )

    assert kanban_cli._cmd_repair_assignees(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["boards"] == ["default", "alpha", "beta"]
    assert {
        (item["board"], item["task_id"], item["assignee"])
        for item in payload["cards"]
    } == {
        ("default", "t_same", "default-missing"),
        ("alpha", "t_same", "alpha-missing"),
        ("beta", "t_same", "beta-missing"),
    }

    for board, expected_assignee in (
        ("default", "default-missing"),
        ("alpha", "alpha-missing"),
        ("beta", "engineer"),
    ):
        with kb.connect_closing(db_path=kb.canonical_kanban_db_path(board)) as conn:
            task = kb.get_task(conn, "t_same")
            assert task is not None
            assert task.assignee == expected_assignee
