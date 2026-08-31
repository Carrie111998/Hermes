"""The pre-update inventory must capture EXACT supervisor identity.

A dashboard/serve runtime can be supervised by a unit the update's
discovery globs (``hermes-gateway*``/``hermes-serve*``) never match — a
hand-written ``acme-dash.service``.  Today that identity is only learned
during late cleanup, after the process (and its cgroup) is already gone,
so the plan cannot promise a relaunch and reconciliation falls back to
matching the PROFILE name against restarted unit strings.  Profile
matching is both too loose (a restarted gateway "accounts for" an
untouched dashboard on the same profile) and too tight (a custom unit
never matches).

These tests pin identity capture and identity-based reconciliation.
"""

from __future__ import annotations

import pytest

from hermes_cli import update_inventory
from hermes_cli.update_inventory import (
    RuntimeRecord,
    UpdatePlan,
    match_runtime_outcomes,
)

CUSTOM_DASH_CGROUP = "/system.slice/acme-dash.service"
USER_GATEWAY_CGROUP = (
    "/user.slice/user-1000.slice/user@1000.service/hermes-gateway-zeus.service"
)


class TestSystemdIdentityCapture:
    def test_system_scope_unit_is_captured_exactly(self):
        identity = update_inventory.capture_supervisor_identity(
            4242, cgroup_of=lambda pid: CUSTOM_DASH_CGROUP
        )
        assert identity.unit == "acme-dash.service"
        assert identity.scope == "system"
        assert identity.cgroup == CUSTOM_DASH_CGROUP

    def test_user_scope_unit_is_captured_exactly(self):
        identity = update_inventory.capture_supervisor_identity(
            4243, cgroup_of=lambda pid: USER_GATEWAY_CGROUP
        )
        assert identity.unit == "hermes-gateway-zeus.service"
        assert identity.scope == "user"

    def test_a_plain_session_scope_yields_no_unit(self):
        identity = update_inventory.capture_supervisor_identity(
            4244,
            cgroup_of=lambda pid: "/user.slice/user-1000.slice/session-3.scope",
        )
        assert identity.unit == ""

    def test_probe_failure_is_not_fatal(self):
        def boom(pid):
            raise OSError("no /proc")

        identity = update_inventory.capture_supervisor_identity(4245, cgroup_of=boom)
        assert identity.unit == ""
        assert identity.cgroup == ""


class TestLaunchdIdentityCapture:
    LIST = (
        "PID\tStatus\tLabel\n"
        "-\t0\tcom.apple.something\n"
        "4242\t0\tai.hermes.gateway-zeus\n"
        "4300\t0\tcom.acme.dashboard\n"
    )

    def test_label_is_resolved_from_launchctl_list(self):
        labels = update_inventory.parse_launchctl_list_labels(self.LIST)
        assert labels[4242] == "ai.hermes.gateway-zeus"
        assert labels[4300] == "com.acme.dashboard"

    def test_unloaded_rows_are_skipped(self):
        labels = update_inventory.parse_launchctl_list_labels(self.LIST)
        assert all(isinstance(pid, int) for pid in labels)
        assert len(labels) == 2

    def test_garbage_does_not_raise(self):
        assert update_inventory.parse_launchctl_list_labels("") == {}
        assert update_inventory.parse_launchctl_list_labels("nonsense") == {}


class TestReconciliationUsesExactIdentity:
    def _plan(self, *runtimes):
        plan = UpdatePlan()
        plan.runtimes = list(runtimes)
        return plan

    def test_custom_unit_is_matched_by_its_exact_name(self):
        plan = self._plan(
            RuntimeRecord(
                kind="dashboard",
                profile="default",
                pid=4242,
                supervisor="systemd",
                restart_via="systemd",
                unit="acme-dash.service",
                unit_scope="system",
            )
        )
        outcomes = match_runtime_outcomes(
            plan,
            restarted_services=["acme-dash.service"],
            relaunched_profiles=[],
            externally_supervised_profiles=[],
            killed_pids=set(),
            failed_units=[],
        )
        assert outcomes[0]["outcome"] == "restarted"

    def test_a_restarted_gateway_does_not_account_for_a_custom_dashboard(self):
        """The bug profile-substring matching hides: only the gateway unit
        was restarted, yet the dashboard on the same profile was reported
        as covered."""
        plan = self._plan(
            RuntimeRecord(
                kind="dashboard",
                profile="default",
                pid=4242,
                supervisor="systemd",
                restart_via="systemd",
                unit="acme-dash.service",
                unit_scope="system",
            )
        )
        outcomes = match_runtime_outcomes(
            plan,
            restarted_services=["hermes-gateway.service"],
            relaunched_profiles=[],
            externally_supervised_profiles=[],
            killed_pids=set(),
            failed_units=[],
        )
        assert outcomes[0]["outcome"] == "unaccounted"

    def test_failed_custom_unit_is_reported_failed(self):
        plan = self._plan(
            RuntimeRecord(
                kind="serve",
                profile="zeus",
                pid=4300,
                supervisor="systemd",
                restart_via="systemd",
                unit="acme-serve.service",
                unit_scope="user",
            )
        )
        outcomes = match_runtime_outcomes(
            plan,
            restarted_services=[],
            relaunched_profiles=["zeus"],
            externally_supervised_profiles=[],
            killed_pids=set(),
            failed_units=["acme-serve.service"],
        )
        assert outcomes[0]["outcome"] == "failed"

    def test_records_without_a_unit_keep_profile_reconciliation(self):
        """Back-compat: a manual gateway has no unit, so the profile-level
        bookkeeping is still the only signal available for it."""
        plan = self._plan(
            RuntimeRecord(
                kind="gateway",
                profile="zeus",
                pid=4400,
                supervisor="manual",
                restart_via="manual",
            )
        )
        outcomes = match_runtime_outcomes(
            plan,
            restarted_services=[],
            relaunched_profiles=["zeus"],
            externally_supervised_profiles=[],
            killed_pids=set(),
            failed_units=[],
        )
        assert outcomes[0]["outcome"] == "restarted"


# ---------------------------------------------------------------------------
# collect_runtime_inventory: identity must be captured PRE-mutation
# ---------------------------------------------------------------------------


def _ledger_entry(**over):
    entry = {
        "pid": 4321,
        "create_time": 111.0,
        "purpose": "dashboard",
        "install": "inst",
        "spawner_pid": None,
        "spawner_create": None,
        "registered_at": 222.0,
        "argv": "hermes dashboard --port 8765",
        "host": "",
        "port": 8765,
        "profile": "default",
    }
    entry.update(over)
    return entry


class TestInventoryCapturesIdentity:
    def test_custom_dashboard_unit_is_captured_before_any_mutation(self, monkeypatch):
        """The exact unit name is only readable while the process lives."""
        import sys
        from types import SimpleNamespace

        monkeypatch.setitem(
            sys.modules,
            "hermes_cli.process_identity",
            SimpleNamespace(
                ledger_entries=lambda **k: [_ledger_entry()],
                spawner_is_dead=lambda e: None,
            ),
        )
        monkeypatch.setattr(
            update_inventory,
            "_default_pid_cgroup",
            lambda pid: CUSTOM_DASH_CGROUP if pid == 4321 else None,
        )

        plan = update_inventory.collect_runtime_inventory()
        rows = [r for r in plan.runtimes if r.kind == "dashboard"]
        assert rows, "the dashboard must be inventoried"
        row = rows[0]
        assert row.unit == "acme-dash.service"
        assert row.unit_scope == "system"
        assert row.supervisor == "systemd"
        assert row.restart_via == "systemd"
        assert row.detail["cgroup"] == CUSTOM_DASH_CGROUP
        assert row.detail["argv"] == "hermes dashboard --port 8765"

    def test_manual_dashboard_without_a_unit_keeps_argv_relaunch(self, monkeypatch):
        import sys
        from types import SimpleNamespace

        monkeypatch.setitem(
            sys.modules,
            "hermes_cli.process_identity",
            SimpleNamespace(
                ledger_entries=lambda **k: [_ledger_entry()],
                spawner_is_dead=lambda e: None,
            ),
        )
        monkeypatch.setattr(
            update_inventory,
            "_default_pid_cgroup",
            lambda pid: "/user.slice/user-1000.slice/session-9.scope",
        )

        plan = update_inventory.collect_runtime_inventory()
        row = [r for r in plan.runtimes if r.kind == "dashboard"][0]
        assert row.unit == ""
        assert row.supervisor == "manual-serve"
        assert row.restart_via == "respawn-argv"


class TestLaunchdPlistCapture:
    """Unloading a launchd job is the only stop a KeepAlive agent respects,
    and re-bootstrapping it needs the plist path — which must therefore be
    recorded while the runtime is still inventoried."""

    def test_plist_is_found_in_the_first_matching_search_dir(self, tmp_path):
        first = tmp_path / "a"
        second = tmp_path / "b"
        first.mkdir()
        second.mkdir()
        (second / "ai.hermes.gateway-zeus.plist").write_text("<x/>", encoding="utf-8")

        found = update_inventory.launchd_plist_for_label(
            "ai.hermes.gateway-zeus", search_dirs=[first, second]
        )
        assert found == str(second / "ai.hermes.gateway-zeus.plist")

    def test_missing_plist_returns_empty(self, tmp_path):
        assert (
            update_inventory.launchd_plist_for_label(
                "ai.hermes.gateway-zeus", search_dirs=[tmp_path]
            )
            == ""
        )

    def test_label_is_not_used_as_a_path(self, tmp_path):
        """A label can never escape the search directory."""
        assert (
            update_inventory.launchd_plist_for_label(
                "../../etc/passwd", search_dirs=[tmp_path]
            )
            == ""
        )
