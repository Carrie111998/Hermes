"""Legacy one-shot dispatcher is an intentionally non-mutating migration stub."""
from __future__ import annotations

import argparse
import os
import tempfile

import pytest


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    test_home = tempfile.mkdtemp(prefix="kanban_cli_passthrough_")
    os.makedirs(os.path.join(test_home, "profiles", "default"), exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", test_home)
    yield test_home


def test_cli_dispatch_is_non_mutating_even_with_legacy_config(isolated_kanban_home, monkeypatch, capsys):
    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_db

    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"kanban": {"max_spawn": 5}})
    monkeypatch.setattr(kanban_db, "connect", lambda *_a, **_k: pytest.fail("board opened"))
    rc = kb_cli._cmd_dispatch(argparse.Namespace(dry_run=True, max=None, failure_limit=2, json=False))
    assert rc != 0
    assert "kanban dispatcher" in capsys.readouterr().err


def test_cli_max_flag_cannot_restore_legacy_mutation(isolated_kanban_home, monkeypatch, capsys):
    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_db

    monkeypatch.setattr(kanban_db, "dispatch_once", lambda *_a, **_k: pytest.fail("dispatch called"))
    rc = kb_cli._cmd_dispatch(argparse.Namespace(dry_run=True, max=2, failure_limit=2, json=False))
    assert rc != 0
    assert "kanban dispatcher" in capsys.readouterr().err
