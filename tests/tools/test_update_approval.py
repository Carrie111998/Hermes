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


class TestApprovalBypassThreadLocal:
    """Regression guard for the same bug class as GHSA-qg5c-hvr5-hjgr
    (already fixed for approval/sudo callbacks in tools/terminal_tool.py):
    the update-approval bypass must be thread-local, not process-global,
    so one thread's in-flight replay can't silently un-gate a concurrent
    thread/session."""

    def test_bypass_is_not_visible_on_a_different_thread(self):
        import threading

        from tools import update_approval as ua

        assert ua.approval_bypass_active() is False

        seen_during = {}
        entered = threading.Event()
        release = threading.Event()

        def _holder():
            with ua.approval_bypass():
                assert ua.approval_bypass_active() is True
                entered.set()
                release.wait(timeout=5)

        t = threading.Thread(target=_holder, daemon=True)
        t.start()
        assert entered.wait(timeout=5), "holder thread never entered approval_bypass()"

        # This is the actual race F8 exists to close: a concurrent thread
        # consulting the gate while another thread's replay is in flight
        # must see the gate as NOT bypassed.
        seen_during["main_thread_sees"] = ua.approval_bypass_active()

        release.set()
        t.join(timeout=5)

        assert seen_during["main_thread_sees"] is False
        # Cleared on the holder thread after its `with` block exits.
        assert ua.approval_bypass_active() is False

    def test_bypass_context_manager_clears_on_exception(self):
        from tools import update_approval as ua

        with pytest.raises(RuntimeError):
            with ua.approval_bypass():
                assert ua.approval_bypass_active() is True
                raise RuntimeError("boom")

        assert ua.approval_bypass_active() is False


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

        from hermes_cli.update_lock import UPDATE_EXIT_STAGED_FOR_APPROVAL

        with pytest.raises(SystemExit) as exc_info:
            cmd_update(args)
        assert exc_info.value.code == UPDATE_EXIT_STAGED_FOR_APPROVAL

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

    def test_cmd_update_approved_param_bypasses_gate_with_no_env_var(self, hermes_home, monkeypatch):
        """The explicit approved=True param must be sufficient on its own —
        no BYPASS_ENV, no thread-local approval_bypass() context. This is
        the actual replay-approval signal now; BYPASS_ENV is only a
        backward-compat fallback for external callers."""
        from hermes_cli.main import cmd_update
        from tools import update_approval as ua

        assert ua.BYPASS_ENV not in os.environ
        assert ua.approval_bypass_active() is False

        called = {}
        monkeypatch.setattr("hermes_cli.config.is_managed", lambda: False)
        monkeypatch.setattr("hermes_cli.config.detect_install_method", lambda _root: "git")
        monkeypatch.setattr("hermes_cli.main._install_hangup_protection", lambda gateway_mode=False: None)
        monkeypatch.setattr("hermes_cli.main._finalize_update_output", lambda _state: None)
        monkeypatch.setattr("hermes_cli.main._cmd_update_impl", lambda args, gateway_mode=False: called.update(ran=True))
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
            update_action=None,
            update_value=None,
        )

        # approved=True alone must skip the staging branch entirely — no
        # SystemExit(UPDATE_EXIT_STAGED_FOR_APPROVAL), straight through to
        # _cmd_update_impl.
        cmd_update(args, approved=True)

        assert called == {"ran": True}
        assert ua.pending_count() == 0
        # Still untouched afterward — approved=True never touches the env.
        assert ua.BYPASS_ENV not in os.environ

    def test_cmd_update_approve_warns_but_does_not_crash_when_cleanup_fails(self, hermes_home, monkeypatch, capsys):
        """The update itself already applied successfully by the time
        discard_pending runs — a cleanup failure must warn, not crash or
        get silently swallowed."""
        from hermes_cli.main import cmd_update
        from tools import update_approval as ua

        rec = ua.stage_update({"branch": "main", "backup": True}, summary="update main")

        def _boom(pending_id):
            raise RuntimeError("permission denied")

        monkeypatch.setattr("hermes_cli.config.is_managed", lambda: False)
        monkeypatch.setattr("hermes_cli.config.detect_install_method", lambda _root: "git")
        monkeypatch.setattr("hermes_cli.main._install_hangup_protection", lambda gateway_mode=False: None)
        monkeypatch.setattr("hermes_cli.main._finalize_update_output", lambda _state: None)
        monkeypatch.setattr("hermes_cli.main._cmd_update_impl", lambda args, gateway_mode=False: None)
        monkeypatch.setattr("hermes_cli.update_lock.UpdateLock.acquire", lambda self: True)
        monkeypatch.setattr("hermes_cli.update_lock.UpdateLock.release", lambda self: None)
        monkeypatch.setattr("tools.update_approval.discard_pending", _boom)

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

        # Must not raise — the update already succeeded, cleanup failure is
        # secondary and must degrade to a warning.
        cmd_update(args)

        out = capsys.readouterr().out
        assert "Warning: update applied" in out
        assert "could not clear pending record" in out
        # The stale record is still there since the unlink genuinely failed —
        # not silently dropped, not double-counted as success.
        assert ua.get_pending(rec["id"]) is not None


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


class TestPendingStoreWriteFailures:
    """Pin down the false-success bug: a write failure must raise, not be
    swallowed into a record that looks identical to a real success."""

    def test_stage_update_raises_on_write_failure(self, hermes_home, monkeypatch):
        from pathlib import Path
        from tools import update_approval as ua

        def _boom(self, *a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(Path, "write_text", _boom)

        with pytest.raises(RuntimeError, match="Could not write pending update record"):
            ua.stage_update({"branch": "main"}, summary="update main")

        assert ua.pending_count() == 0

    def test_stage_update_raises_when_write_silently_does_not_land(self, hermes_home, monkeypatch):
        """os.replace() that doesn't raise but also doesn't actually move the
        file must still be caught, by the post-write existence check rather
        than exception handling."""
        from tools import update_approval as ua

        monkeypatch.setattr(ua.os, "replace", lambda *a, **kw: None)

        with pytest.raises(RuntimeError, match="does not exist"):
            ua.stage_update({"branch": "main"}, summary="update main")

        assert ua.pending_count() == 0

    def test_discard_pending_returns_false_for_unknown_id(self, hermes_home):
        from tools import update_approval as ua

        assert ua.discard_pending("does-not-exist") is False

    def test_discard_pending_raises_on_unlink_failure(self, hermes_home, monkeypatch):
        from pathlib import Path
        from tools import update_approval as ua

        rec = ua.stage_update({"branch": "main"}, summary="update main")

        def _boom(self, *a, **kw):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "unlink", _boom)

        with pytest.raises(RuntimeError, match="Could not remove pending update record"):
            ua.discard_pending(rec["id"])

        # Genuine failure — the record must still be there, unlike the old
        # behavior where a failed unlink was indistinguishable from "gone".
        assert ua.get_pending(rec["id"]) is not None


class TestCmdUpdateStageFailure:
    def test_cmd_update_exits_nonzero_and_reports_failure(self, hermes_home, monkeypatch, capsys):
        from hermes_cli.main import cmd_update

        def _raise(*a, **kw):
            raise RuntimeError("disk full")

        monkeypatch.setattr("hermes_cli.config.is_managed", lambda: False)
        monkeypatch.setattr("hermes_cli.config.detect_install_method", lambda _root: "git")
        monkeypatch.setattr("tools.update_approval.stage_update", _raise)

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

        with pytest.raises(SystemExit) as exc_info:
            cmd_update(args)

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "Could not stage the update for approval" in out
        assert "No changes were applied yet." not in out

        from tools import update_approval as ua
        assert ua.pending_count() == 0

    def test_reject_distinguishes_not_found_from_failure(self, hermes_home, monkeypatch):
        from hermes_cli.update_approval_commands import handle_pending_subcommand
        from tools import update_approval as ua

        not_found = handle_pending_subcommand(["reject", "nope"]) or ""
        assert "No pending update with id 'nope'" in not_found

        rec = ua.stage_update({"branch": "main"}, summary="update main")

        def _boom(pending_id):
            raise RuntimeError("permission denied")

        monkeypatch.setattr("tools.update_approval.discard_pending", _boom)

        failed = handle_pending_subcommand(["reject", rec["id"]]) or ""
        assert "Could not reject pending update" in failed
        assert "No pending update with id" not in failed
