from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from typing import cast

import pytest
from fastapi import HTTPException


def test_manifest_classifies_all_mandatory_installed_entrypoints():
    manifest_path = Path(__file__).parents[1] / "contracts" / "kanban_mutating_entrypoints.json"
    rows = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_symbol = {row["symbol"]: row for row in rows}
    required = {
        "gateway.kanban_watchers.GatewayKanbanWatchersMixin._kanban_dispatcher_watcher": "GUARDED_DISPATCH_MUTATION",
        "hermes_cli.kanban._cmd_dispatcher": "GUARDED_DISPATCH_MUTATION",
        "hermes_cli.kanban._cmd_dispatch": "DISABLED_NONZERO",
        "hermes_cli.kanban._cmd_daemon": "DISABLED_NONZERO",
        "plugins.kanban.dashboard.plugin_api.dispatch": "DISABLED_NONZERO",
        "hermes_cli.kanban_db.dispatch_once_authorized": "GUARDED_DISPATCH_MUTATION",
        "hermes_cli.kanban_db.run_daemon": "GUARDED_DISPATCH_MUTATION",
        "hermes_cli.kanban_db._invoke_worker_spawn": "GUARDED_DISPATCH_MUTATION",
        "hermes_cli.kanban_delivery_outbox.materialize_parent": "DETERMINISTIC_DELIVERY_MUTATION",
        "hermes_cli.kanban_delivery_outbox.lease_child": "TOKEN_MINTING_DELIVERY_MUTATION",
        "hermes_cli.kanban_delivery_outbox.mark_sending": "TOKEN_GUARDED_DELIVERY_MUTATION",
        "hermes_cli.kanban_delivery_outbox.mark_sent": "TOKEN_GUARDED_DELIVERY_MUTATION",
        "hermes_cli.kanban_delivery_outbox.mark_failed": "TOKEN_GUARDED_DELIVERY_MUTATION",
        "hermes_cli.kanban_delivery_outbox.recover_expired": "LEASE_EXPIRY_DELIVERY_MUTATION",
        "hermes_cli.kanban_delivery_outbox.mark_dead": "STATE_GUARDED_DELIVERY_MUTATION",
        "hermes_cli.kanban_delivery_outbox.audit_dead_child": "AUDITED_DELIVERY_MUTATION",
        "hermes_cli.kanban_delivery_outbox.process_parent": "TOKEN_GUARDED_DELIVERY_MUTATION",
    }
    assert {symbol: by_symbol[symbol]["authority_policy"] for symbol in required} == required
    assert all(row.get("test_id") for row in rows)


def test_generated_inventory_matches_installed_surfaces_and_finds_new_mutator(tmp_path):
    root = Path(__file__).parents[2]
    scanner_path = root / "scripts/check_kanban_mutation_entrypoints.py"
    checked = subprocess.run(
        [sys.executable, str(scanner_path)],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "generated" in checked.stdout

    spec = importlib.util.spec_from_file_location("mutation_inventory_scanner", scanner_path)
    assert spec is not None and spec.loader is not None
    scanner = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = scanner
    spec.loader.exec_module(scanner)
    (tmp_path / "hermes_cli").mkdir()
    (tmp_path / "gateway").mkdir()
    (tmp_path / "plugins/kanban/dashboard").mkdir(parents=True)
    (tmp_path / "hermes_cli/kanban_delivery_outbox.py").write_text(
        "def newly_installed_mutator(conn):\n"
        "    conn.execute(\"UPDATE state SET value=1\")\n",
        encoding="utf-8",
    )
    (tmp_path / "gateway/kanban_watchers.py").write_text("", encoding="utf-8")
    (tmp_path / "plugins/kanban/dashboard/plugin_api.py").write_text("", encoding="utf-8")
    assert scanner.discover_entrypoints(tmp_path) == {
        "hermes_cli.kanban_delivery_outbox.newly_installed_mutator"
    }


def test_legacy_dispatch_denies_before_board_open(monkeypatch, capsys):
    from hermes_cli import kanban as cli

    monkeypatch.setattr(cli.kb, "connect_closing", lambda: pytest.fail("board opened"))
    rc = cli._cmd_dispatch(argparse.Namespace())
    assert rc != 0
    assert "kanban dispatcher" in capsys.readouterr().err


def test_legacy_daemon_force_denies_before_init(monkeypatch, capsys):
    from hermes_cli import kanban as cli

    monkeypatch.setattr(cli.kb, "init_db", lambda: pytest.fail("board initialized"))
    rc = cli._cmd_daemon(argparse.Namespace(force=True))
    assert rc != 0
    assert "kanban dispatcher" in capsys.readouterr().err


def test_dashboard_dispatch_is_status_only_409(monkeypatch):
    from hermes_cli.dispatcher_authority import AuthorityStatus
    from plugins.kanban.dashboard import plugin_api

    monkeypatch.setattr(plugin_api, "_resolve_board", lambda *_: pytest.fail("board resolved"))
    monkeypatch.setattr(plugin_api, "_conn", lambda *_a, **_k: pytest.fail("board opened"))
    monkeypatch.setattr(
        "hermes_cli.dispatcher_authority.read_status_no_side_effects",
        lambda: AuthorityStatus(True, "held", owner_hint="pid:1", freshness_seconds=2),
    )
    with pytest.raises(HTTPException) as raised:
        plugin_api.dispatch(board="ignored")
    assert raised.value.status_code == 409
    detail = raised.value.detail
    assert detail["dispatch_nudge_accepted"] is False
    assert detail["dispatch_performed"] is False


def test_dashboard_dispatch_status_error_is_503_and_secret_free(monkeypatch):
    from hermes_cli.dispatcher_authority import AuthorityStatus
    from plugins.kanban.dashboard import plugin_api

    monkeypatch.setattr(
        "hermes_cli.dispatcher_authority.read_status_no_side_effects",
        lambda: AuthorityStatus(False, "unavailable", error_class="permission_denied"),
    )
    with pytest.raises(HTTPException) as raised:
        plugin_api.dispatch()
    assert raised.value.status_code == 503
    encoded = json.dumps(raised.value.detail)
    assert "permission_denied" in encoded
    assert "token" not in encoded.lower()


def test_direct_dispatch_once_rejects_without_opaque_authority_before_tick(monkeypatch):
    from hermes_cli import kanban_db
    from hermes_cli.dispatcher_authority import DispatcherAuthorityError

    monkeypatch.setattr(
        kanban_db,
        "_dispatch_once_locked",
        lambda *_args, **_kwargs: pytest.fail("unguarded dispatch tick reached"),
    )
    with pytest.raises(DispatcherAuthorityError):
        kanban_db.dispatch_once(None, object(), dry_run=True)


def test_spawn_facade_rejects_without_opaque_authority_before_test_double(monkeypatch):
    from hermes_cli import kanban_db
    from hermes_cli.dispatcher_authority import DispatcherAuthorityError

    with pytest.raises(DispatcherAuthorityError):
        kanban_db._invoke_worker_spawn(
            None,
            lambda *_a, **_kw: pytest.fail("unguarded spawn reached"),
            cast(kanban_db.Task, object()),
            "/tmp/unused",
            board="default",
        )


def test_daemon_rejects_without_opaque_authority_before_board_open(monkeypatch):
    from hermes_cli import kanban_db
    from hermes_cli.dispatcher_authority import DispatcherAuthorityError

    monkeypatch.setattr(kanban_db, "connect", lambda: pytest.fail("board opened"))
    with pytest.raises(DispatcherAuthorityError):
        kanban_db.run_daemon(None, interval=0)
