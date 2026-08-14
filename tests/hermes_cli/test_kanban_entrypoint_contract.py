from __future__ import annotations

import argparse
import json
from pathlib import Path

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
    }
    assert {symbol: by_symbol[symbol]["authority_policy"] for symbol in required} == required
    assert all(row.get("test_id") for row in rows)


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
