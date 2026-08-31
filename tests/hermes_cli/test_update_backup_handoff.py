"""Regression tests for pre-update state transferred across Windows re-exec."""

import json
from argparse import Namespace


def test_reexec_reuses_parent_snapshot_without_new_backup(monkeypatch):
    from hermes_cli import main as cli_main
    from hermes_cli import update_cmd

    monkeypatch.setenv(cli_main._UPDATE_REEXEC_ENV, "1")
    monkeypatch.setenv(cli_main._UPDATE_PRE_SNAPSHOT_ENV, "snapshot-123")

    def _unexpected_backup(_args):
        raise AssertionError("re-exec child must not create another backup")

    monkeypatch.setattr(cli_main, "_run_pre_update_backup", _unexpected_backup)

    snapshot_id, reused = update_cmd._run_or_resume_pre_update_backup(
        Namespace(no_backup=False, backup=True)
    )

    assert snapshot_id == "snapshot-123"
    assert reused is True


def test_reexec_restores_sibling_snapshot_ids(monkeypatch):
    from hermes_cli import main as cli_main
    from hermes_cli import update_cmd

    monkeypatch.setenv(cli_main._UPDATE_REEXEC_ENV, "1")
    monkeypatch.setenv(cli_main._UPDATE_PRE_SNAPSHOT_ENV, "snapshot-123")
    monkeypatch.setenv(
        cli_main._UPDATE_SIBLING_SNAPSHOTS_ENV,
        json.dumps({"work": "snapshot-work"}),
    )
    monkeypatch.setattr(update_cmd, "_LAST_SIBLING_SNAPSHOTS", {})

    update_cmd._run_or_resume_pre_update_backup(Namespace())

    assert update_cmd._LAST_SIBLING_SNAPSHOTS == {"work": "snapshot-work"}