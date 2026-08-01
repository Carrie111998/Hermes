"""Tests for the update-approval gate and pending-store workflow."""

from __future__ import annotations

import argparse
import os
import shutil
import tempfile

import pytest


@pytest.fixture
def hermes_home(monkeypatch):
    d = tempfile.mkdtemp(prefix="hermes_update_approval_")
    home = f"{d}/.hermes"
    os.makedirs(home)
    monkeypatch.setenv("HERMES_HOME", home)
    yield home
    shutil.rmtree(d, ignore_errors=True)


class TestUpdateApprovalConfig:
    def test_default_gate_is_on(self, hermes_home):
        from tools import update_approval as ua

        assert ua.apply_approval_enabled() is True

    def test_can_turn_gate_off_via_config(self, hermes_home):
        from hermes_cli.config import load_config, save_config
        from tools import update_approval as ua

        cfg = load_config()
        cfg.setdefault("updates", {})["apply_approval"] = False
        save_config(cfg, merge_existing=True)

        assert ua.apply_approval_enabled() is False


class TestPendingStore:
    def test_stage_and_retrieve_pending_update(self, hermes_home):
        from tools import update_approval as ua

        rec = ua.stage_update({"branch": "main", "backup": False}, summary="update main")

        assert rec["id"]
        assert ua.pending_count() == 1
        loaded = ua.get_pending(rec["id"])
        assert loaded is not None
        assert loaded["summary"] == "update main"
        assert ua.list_pending()[0]["id"] == rec["id"]


class TestCmdUpdateGate:
    def test_cmd_update_stages_when_gate_on(self, hermes_home, monkeypatch, capsys):
        from hermes_cli.main import cmd_update
        from tools import update_approval as ua

        monkeypatch.setattr("hermes_cli.config.is_managed", lambda: False)
        monkeypatch.setattr("hermes_cli.config.detect_install_method", lambda _root: "git")

        args = argparse.Namespace(
            gateway=False,
            check=False,
            no_backup=False,
            backup=False,
            yes=False,
            branch=None,
            force=False,
            force_venv=False,
            update_action=None,
            update_value=None,
        )

        cmd_update(args)

        out = capsys.readouterr().out
        assert "Update staged for approval" in out
        assert "No changes were applied yet." in out
        assert ua.pending_count() == 1

    def test_cmd_update_approve_replays_and_discards_pending(self, hermes_home, monkeypatch):
        from hermes_cli.main import cmd_update
        from tools import update_approval as ua

        rec = ua.stage_update({"branch": "main", "backup": True}, summary="update main")
        called = {}

        monkeypatch.setattr("hermes_cli.config.is_managed", lambda: False)
        monkeypatch.setattr("hermes_cli.config.detect_install_method", lambda _root: "git")
        monkeypatch.setattr("hermes_cli.main._install_hangup_protection", lambda gateway_mode=False: None)
        monkeypatch.setattr("hermes_cli.main._finalize_update_output", lambda _state: None)
        monkeypatch.setattr("hermes_cli.main._cmd_update_impl", lambda args, gateway_mode=False: called.update(branch=args.branch, backup=args.backup, gateway=gateway_mode))
        monkeypatch.setattr("hermes_cli.update_lock.UpdateLock.acquire", lambda self: True)
        monkeypatch.setattr("hermes_cli.update_lock.UpdateLock.release", lambda self: None)

        args = argparse.Namespace(
            gateway=False,
            check=False,
            no_backup=False,
            backup=False,
            yes=False,
            branch=None,
            force=False,
            force_venv=False,
            update_action="approve",
            update_value=rec["id"],
        )

        cmd_update(args)

        assert called == {"branch": "main", "backup": True, "gateway": False}
        assert ua.pending_count() == 0


class TestUpdateApprovalCommands:
    def test_pending_and_reject_output(self, hermes_home):
        from hermes_cli.update_approval_commands import handle_pending_subcommand
        from tools import update_approval as ua

        rec = ua.stage_update({"branch": "main"}, summary="update main")
        pending = handle_pending_subcommand(["pending"]) or ""
        rejected = handle_pending_subcommand(["reject", rec["id"]]) or ""

        assert rec["id"] in pending
        assert "Rejected pending update" in rejected
        assert ua.pending_count() == 0
