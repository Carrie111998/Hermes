"""Production-path coverage for board-scoped automatic WIP limits."""

from __future__ import annotations

import argparse
import json

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    (home / "profiles" / "default").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in ("HERMES_KANBAN_HOME", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    import hermes_constants

    hermes_constants._cached_default_hermes_root = None
    kb._INITIALIZED_PATHS.clear()
    return home


def _parser():
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    return parser


def _spawn(*_args, **_kwargs):
    return 12345


def test_metadata_set_omit_clear_and_invalid_persisted_values(fresh_home):
    meta = kb.create_board("metadata", wip_limit=3)
    assert meta["wip_limit"] == 3
    kb.write_board_metadata("metadata", description="kept")
    assert kb.read_board_metadata("metadata")["wip_limit"] == 3

    cleared = kb.write_board_metadata("metadata", wip_limit=None)
    assert cleared["wip_limit"] is None

    path = kb.board_metadata_path("metadata")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["wip_limit"] = True
    raw["unknown"] = "preserved"
    path.write_text(json.dumps(raw), encoding="utf-8")
    read = kb.read_board_metadata("metadata")
    assert read["wip_limit"] is None
    assert read["unknown"] == "preserved"

    before = path.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        kb.write_board_metadata("metadata", wip_limit=0)
    assert path.read_text(encoding="utf-8") == before


def test_explicit_dispatch_respects_board_limit_after_cleanup(fresh_home):
    kb.create_board("capped", wip_limit=1)
    with kb.connect(board="capped") as conn:
        running = kb.create_task(conn, title="running", assignee="default")
        ready = kb.create_task(conn, title="ready", assignee="default")
        assert kb.claim_task(conn, running, claimer="default") is not None
        result = kb.dispatch_once(
            conn, board="capped", spawn_fn=_spawn, dry_run=True, max_in_progress=4
        )
        assert result.spawned == []
        assert ready in result.skipped_wip_capped
        assert kb.get_task(conn, ready).status == "ready"


def test_explicit_dispatch_composes_board_limit_with_existing_running_tasks(fresh_home):
    kb.create_board("composed", wip_limit=3)
    with kb.connect(board="composed") as conn:
        running_ids = [
            kb.create_task(conn, title=f"running-{index}", assignee="default")
            for index in range(2)
        ]
        ready = kb.create_task(conn, title="ready", assignee="default")
        for task_id in running_ids:
            assert kb.claim_task(conn, task_id, claimer="default") is not None

        result = kb.dispatch_once(
            conn, board="composed", spawn_fn=_spawn, dry_run=True, max_spawn=8
        )

        assert [item[0] for item in result.spawned] == [ready]
        assert result.skipped_wip_capped == []


def test_dispatch_honors_installation_cap_without_max_spawn(fresh_home):
    kb.create_board("installation")
    with kb.connect(board="installation") as conn:
        running_ids = [
            kb.create_task(conn, title=f"running-{index}", assignee="default")
            for index in range(2)
        ]
        ready_ids = [
            kb.create_task(conn, title=f"ready-{index}", assignee="default")
            for index in range(3)
        ]
        for task_id in running_ids:
            assert kb.claim_task(conn, task_id, claimer="default") is not None

        result = kb.dispatch_once(
            conn,
            board="installation",
            spawn_fn=_spawn,
            dry_run=True,
            max_in_progress=4,
        )

        assert [item[0] for item in result.spawned] == ready_ids[:2]


def test_dry_run_reports_board_wip_deferrals(fresh_home):
    kb.create_board("dry-run", wip_limit=2)
    with kb.connect(board="dry-run") as conn:
        ready_ids = [
            kb.create_task(conn, title=f"ready-{index}", assignee="default")
            for index in range(3)
        ]

        result = kb.dispatch_once(
            conn, board="dry-run", spawn_fn=_spawn, dry_run=True, max_spawn=8
        )

        assert [item[0] for item in result.spawned] == ready_ids[:2]
        assert result.skipped_wip_capped == ready_ids[2:]


def test_dispatch_falls_back_after_malformed_board_selector(fresh_home, monkeypatch):
    kb.create_board("fallback", wip_limit=2)
    kb.set_current_board("fallback")
    with kb.connect(board="fallback") as conn:
        ready = kb.create_task(conn, title="ready", assignee="default")
        normalize_board_slug = kb._normalize_board_slug
        monkeypatch.setattr(
            kb,
            "_normalize_board_slug",
            lambda slug: (
                (_ for _ in ()).throw(ValueError("malformed"))
                if slug == "malformed"
                else normalize_board_slug(slug)
            ),
        )

        workspace = fresh_home / "workspace"
        seen_board = {}

        def resolve_workspace(_task, board=None):
            seen_board["value"] = board
            workspace.mkdir(exist_ok=True)
            return workspace

        monkeypatch.setattr(kb, "resolve_workspace", resolve_workspace)
        result = kb.dispatch_once(conn, board="malformed", spawn_fn=_spawn)

        assert [item[0] for item in result.spawned] == [ready]
        assert seen_board["value"] == "fallback"


def test_implicit_or_precedence_current_board(fresh_home, monkeypatch):
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    kb.create_board("current", wip_limit=2)
    kb.set_current_board("current")
    with kb.connect(board="current") as conn:
        running = kb.create_task(conn, title="running", assignee="default")
        ready = kb.create_task(conn, title="ready", assignee="default")
        assert kb.claim_task(conn, running, claimer="default") is not None

        result = kb.dispatch_once(conn, spawn_fn=_spawn, dry_run=True, max_in_progress=4)
        assert [item[0] for item in result.spawned] == [ready]

    with kb.connect(board="current") as conn:
        # The installation cap is narrower than the board cap and therefore
        # prevents the ready task from being claimed.
        result = kb.dispatch_once(conn, spawn_fn=_spawn, dry_run=True, max_in_progress=1)
        assert result.spawned == []


def test_cli_create_show_set_list_and_clear(fresh_home, capsys):
    parser = _parser()
    assert kc.kanban_command(parser.parse_args([
        "kanban", "boards", "create", "cli-board", "--wip-limit", "2"
    ])) == 0
    assert kb.read_board_metadata("cli-board")["wip_limit"] == 2

    assert kc.kanban_command(parser.parse_args([
        "kanban", "boards", "set-wip-limit", "cli-board", "4"
    ])) == 0
    assert kb.read_board_metadata("cli-board")["wip_limit"] == 4
    assert kc.kanban_command(parser.parse_args([
        "kanban", "boards", "list"
    ])) == 0
    assert "WIP=4" in capsys.readouterr().out
    assert kc.kanban_command(parser.parse_args([
        "kanban", "boards", "switch", "cli-board"
    ])) == 0
    capsys.readouterr()
    assert kc.kanban_command(parser.parse_args([
        "kanban", "boards", "show"
    ])) == 0
    assert "WIP limit:    4" in capsys.readouterr().out
    assert kc.kanban_command(parser.parse_args([
        "kanban", "boards", "set-wip-limit", "cli-board"
    ])) == 0
    assert kb.read_board_metadata("cli-board")["wip_limit"] is None

    assert kc.kanban_command(parser.parse_args([
        "kanban", "boards", "list"
    ])) == 0
    assert "WIP=unlimited" in capsys.readouterr().out
    assert kc.kanban_command(parser.parse_args([
        "kanban", "boards", "show"
    ])) == 0
    assert "WIP limit:    unlimited" in capsys.readouterr().out

    with pytest.raises(SystemExit):
        parser.parse_args(["kanban", "boards", "set-wip-limit", "cli-board", "0"])
