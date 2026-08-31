"""The Windows ZIP update must prove its fleet came back (#99450 R3-1).

The ZIP path replaces the checkout wholesale under a fleet the pre-mutation
quiesce stopped — and then returned. Everything that brings the fleet back
lives on the git path's restart phase, which the ZIP branch never reaches,
so the relaunch fell through to ``cmd_update``'s command-boundary backstop.
That backstop exists to make sure a stopped fleet is not left down; it is
not a verification. It calls ``_relaunch_quiesced_runtimes()`` with NO
expected SHA, which makes every outcome's ``sha_matches`` false by
construction, and it discards the outcomes it gets back inside a
``try/except``. So a ZIP update printed its success summary, wrote a
zero gateway exit code and exited 0 over a fleet that might be down, might
be up on the pre-update tree, or might never have been asked.

The ZIP path now relaunches explicitly: paused Windows services resume
first (the git path's order — starting a service twice is an error, not a
relaunch), the post-overlay code identity becomes the expected SHA, and
each runtime has to come back, leave no old PID behind AND report that SHA
before the update calls itself successful. Anything short of that keeps the
durable restart-pending obligation on disk for the next pass and reports
failure.
"""

from __future__ import annotations

import pytest

from hermes_cli import update_cmd, update_quiesce


NEW_SHA = "b" * 40
OLD_SHA = "a" * 40


@pytest.fixture(autouse=True)
def _clean_pending():
    update_quiesce.clear_restart_pending_state()
    yield
    update_quiesce.clear_restart_pending_state()


@pytest.fixture()
def zip_fleet(monkeypatch):
    """One quiesced, supervised gateway owed a relaunch, and the knobs."""
    knobs = {
        "restarted": [],
        "resumed": [],
        "restart_ok": True,
        "probe_sha": NEW_SHA,
        "identity": {"sha": NEW_SHA, "version": "9.9.9"},
        "resume_error": None,
    }

    def _restart_unit(unit, scope, state=None):
        knobs["restarted"].append(unit)
        return knobs["restart_ok"]

    def _resume(token):
        knobs["resumed"].append(token)
        if knobs["resume_error"] is not None:
            raise knobs["resume_error"]

    for module in (update_cmd, _main()):
        monkeypatch.setattr(module, "_restart_supervised_unit", _restart_unit)
        monkeypatch.setattr(module, "_runtime_pid_alive", lambda pid: False)
        monkeypatch.setattr(
            module,
            "_probe_relaunched_runtime_sha",
            lambda record, new_pid=None, **kw: knobs["probe_sha"],
        )
        monkeypatch.setattr(module, "_resume_windows_gateways_after_update", _resume)
    monkeypatch.setattr(
        "hermes_cli.build_info.get_code_identity",
        lambda refresh=False: dict(knobs["identity"]),
    )
    return knobs


def _main():
    from hermes_cli import main as hermes_main

    return hermes_main


def _record_a_quiesced_gateway(profile="default", pid=4242):
    from hermes_cli.update_inventory import RuntimeRecord

    written = update_quiesce.write_restart_pending_state(
        [
            RuntimeRecord(
                kind="gateway",
                profile=profile,
                pid=pid,
                supervisor="systemd",
                restart_via="systemd",
                unit=f"hermes-gateway-{profile}.service",
                unit_scope="user",
                detail={"start_time": 1.0},
            )
        ],
        expected_sha=OLD_SHA,
    )
    assert written, "the test needs a durable pending record to act on"


def _still_owed() -> list:
    state = update_quiesce.read_restart_pending_state()
    return list((state or {}).get("runtimes") or [])


class TestTheZipPathProvesTheFleetCameBack:
    def test_a_fleet_that_was_never_stopped_is_a_clean_success(
        self, zip_fleet
    ):
        assert update_cmd._relaunch_fleet_after_zip_update(None) is True
        assert zip_fleet["restarted"] == []

    def test_every_runtime_back_on_the_new_sha_reports_success(self, zip_fleet):
        _record_a_quiesced_gateway()

        assert update_cmd._relaunch_fleet_after_zip_update(None) is True

        assert zip_fleet["restarted"] == ["hermes-gateway-default.service"]
        assert _still_owed() == [], "a discharged obligation must be cleared"

    def test_a_replacement_reporting_the_pre_update_sha_is_not_success(
        self, zip_fleet
    ):
        """The exact failure the missing expected-SHA hid: it came back, on
        the old tree."""
        _record_a_quiesced_gateway()
        zip_fleet["probe_sha"] = OLD_SHA

        assert update_cmd._relaunch_fleet_after_zip_update(None) is False
        assert _still_owed(), "the obligation must survive a failed proof"

    def test_a_runtime_that_never_came_back_is_not_success(self, zip_fleet):
        _record_a_quiesced_gateway()
        zip_fleet["restart_ok"] = False

        assert update_cmd._relaunch_fleet_after_zip_update(None) is False
        assert _still_owed()

    def test_an_unresolvable_post_update_sha_refuses_to_guess(self, zip_fleet):
        """No identity to check against means no proof — so no success.

        Relaunching anyway with an empty expected SHA is what the
        command-boundary backstop does, and it is why every ZIP update
        reported success: ``sha_matches`` against ``""`` is never true, and
        nobody looked.
        """
        _record_a_quiesced_gateway()
        zip_fleet["identity"] = {"sha": None, "version": None}

        assert update_cmd._relaunch_fleet_after_zip_update(None) is False
        assert zip_fleet["restarted"] == [], "it relaunched without a proof"
        assert _still_owed()

    def test_paused_windows_services_resume_before_the_relaunch(
        self, zip_fleet
    ):
        """Starting an already-started service is an error, not a relaunch —
        the git path resumes first for the same reason."""
        _record_a_quiesced_gateway()
        token = {"resume_needed": True}

        assert update_cmd._relaunch_fleet_after_zip_update(token) is True

        assert zip_fleet["resumed"] == [token]
        assert zip_fleet["restarted"], "the relaunch still has to happen"

    def test_a_failed_windows_resume_is_not_success(self, zip_fleet):
        _record_a_quiesced_gateway()
        zip_fleet["resume_error"] = RuntimeError(
            "Could not restart Windows gateway service(s): hermes-gateway"
        )

        assert update_cmd._relaunch_fleet_after_zip_update({"x": 1}) is False
        assert _still_owed()


class TestTheZipUpdateReportsTheFleetVerdict:
    def test_a_failed_fleet_relaunch_exits_non_zero(self, tmp_path):
        with pytest.raises(SystemExit) as excinfo:
            update_cmd._finish_zip_update(
                desktop_build_ok=True, fleet_ok=False, gateway_mode=False
            )
        assert excinfo.value.code != 0

    def test_a_proven_fleet_returns_cleanly(self):
        update_cmd._finish_zip_update(
            desktop_build_ok=True, fleet_ok=True, gateway_mode=False
        )

    def test_the_gateway_exit_code_carries_the_fleet_verdict(self, monkeypatch):
        written: list = []
        monkeypatch.setattr(
            update_cmd, "_write_gateway_update_exit_code", written.append
        )
        with pytest.raises(SystemExit):
            update_cmd._finish_zip_update(
                desktop_build_ok=True, fleet_ok=False, gateway_mode=True
            )
        assert written == [False], (
            "a gateway-mode ZIP update reported success over an unproven fleet"
        )

    def test_a_failed_desktop_build_still_reports_through_the_gateway_file(
        self, monkeypatch
    ):
        written: list = []
        monkeypatch.setattr(
            update_cmd, "_write_gateway_update_exit_code", written.append
        )
        update_cmd._finish_zip_update(
            desktop_build_ok=False, fleet_ok=True, gateway_mode=True
        )
        assert written == [False]


class TestTheZipUpdateWiresTheRelaunchIn:
    def test_update_via_zip_relaunches_before_it_finalizes_the_receipt(
        self, monkeypatch, tmp_path
    ):
        """Order is the contract: the receipt must not say ``success``
        before the fleet has been proven back."""
        events: list = []

        def _relaunch():
            events.append("relaunch")
            return False

        def _finalize(outcome, *a, **k):
            events.append(f"receipt:{outcome}")

        monkeypatch.setattr(
            "hermes_cli.update_receipt.finalize_update_receipt", _finalize
        )
        update_cmd._finish_zip_update_reporting(
            relaunch_fleet=_relaunch,
            desktop_build_ok=True,
            node_failures=[],
        )
        assert events == ["relaunch", "receipt:partial"]

    def test_a_proven_fleet_finalizes_the_receipt_as_success(
        self, monkeypatch
    ):
        events: list = []
        monkeypatch.setattr(
            "hermes_cli.update_receipt.finalize_update_receipt",
            lambda outcome, *a, **k: events.append(outcome),
        )
        ok = update_cmd._finish_zip_update_reporting(
            relaunch_fleet=lambda: True,
            desktop_build_ok=True,
            node_failures=[],
        )
        assert ok is True
        assert events == ["success"]

    def test_a_relaunch_that_raises_is_a_failure_not_a_success(
        self, monkeypatch
    ):
        events: list = []
        monkeypatch.setattr(
            "hermes_cli.update_receipt.finalize_update_receipt",
            lambda outcome, *a, **k: events.append(outcome),
        )

        def _boom():
            raise RuntimeError("relaunch machinery is gone")

        ok = update_cmd._finish_zip_update_reporting(
            relaunch_fleet=_boom, desktop_build_ok=True, node_failures=[]
        )
        assert ok is False
        assert events == ["partial"]


class TestTheZipBranchIsWiredIn:
    def test_the_branch_relaunches_and_refuses_to_report_success(
        self, monkeypatch, tmp_path
    ):
        """End to end through ``_cmd_update_impl``'s ZIP fallback.

        The ZIP fallback is Windows-only and fires when there is no usable
        git checkout, so the platform and the missing ``.git`` are what
        select it here — nothing else about the branch is stubbed away.
        """
        from types import SimpleNamespace

        from tests.hermes_cli import test_update_quiesce_integration as harness

        events: list = []
        harness._patch_update_deps(monkeypatch, tmp_path, events)
        (tmp_path / ".git").rmdir()  # no git checkout -> the ZIP fallback
        monkeypatch.setattr(update_cmd.sys, "platform", "win32")
        monkeypatch.setattr(
            _main(), "_require_quiesced", lambda *a, **k: None
        )

        def _zip(args, *, had_desktop_app_before_update=False, relaunch_fleet=None):
            events.append("zip:overlay")
            events.append(f"zip:fleet={relaunch_fleet()}")
            return True

        monkeypatch.setattr(update_cmd, "_update_via_zip", _zip)
        monkeypatch.setattr(
            update_cmd,
            "_relaunch_fleet_after_zip_update",
            lambda token: events.append("fleet:relaunch") or False,
        )
        monkeypatch.setattr(
            _main(),
            "_relaunch_fleet_after_zip_update",
            lambda token: events.append("fleet:relaunch") or False,
            raising=False,
        )

        with pytest.raises(SystemExit) as excinfo:
            update_cmd._cmd_update_impl(
                SimpleNamespace(
                    branch=None, yes=True, force=False, force_venv=False
                ),
                gateway_mode=False,
            )

        assert "fleet:relaunch" in events, events
        assert "zip:fleet=False" in events, events
        assert excinfo.value.code != 0, "an unproven fleet reported success"
