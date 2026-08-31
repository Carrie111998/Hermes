"""Updater ownership must sit outside every affected runtime's cgroup/tree.

A detached updater is not enough: ``setsid`` leaves a process in the
gateway's systemd cgroup, so ``systemctl restart hermes-gateway`` (or a
process-group kill) takes the updater down mid-mutation.  Equally, an
updater still parented to a runtime dies with it.

These tests pin the pure assessment: given the fleet and probes for the
updater's cgroup and ancestry, is the updater independently owned?
"""

from __future__ import annotations

from hermes_cli import update_quiesce
from hermes_cli.update_inventory import RuntimeRecord, UpdatePlan


def _plan(*runtimes):
    plan = UpdatePlan()
    plan.runtimes = list(runtimes)
    return plan


def _runtime(pid, cgroup="", unit="hermes-gateway.service"):
    return RuntimeRecord(
        kind="gateway",
        profile="default",
        pid=pid,
        supervisor="systemd",
        unit=unit,
        unit_scope="user",
        detail={"cgroup": cgroup} if cgroup else {},
    )


GATEWAY_CGROUP = "/user.slice/user-1000.slice/user@1000.service/hermes-gateway.service"


def test_updater_in_its_own_scope_is_isolated():
    result = update_quiesce.assess_updater_isolation(
        _plan(_runtime(4242, GATEWAY_CGROUP)),
        updater_pid=9001,
        cgroup_of=lambda pid: (
            "/user.slice/user-1000.slice/user@1000.service/hermes-updater-9001.scope"
            if pid == 9001
            else GATEWAY_CGROUP
        ),
        ancestors_of=lambda pid: [1],
    )
    assert result.isolated is True


def test_updater_sharing_the_gateway_cgroup_is_not_isolated():
    result = update_quiesce.assess_updater_isolation(
        _plan(_runtime(4242, GATEWAY_CGROUP)),
        updater_pid=9001,
        cgroup_of=lambda pid: GATEWAY_CGROUP,
        ancestors_of=lambda pid: [1],
    )
    assert result.isolated is False
    assert "hermes-gateway.service" in result.reason


def test_updater_nested_below_the_gateway_cgroup_is_not_isolated():
    """A transient child scope inside the unit dies with the unit too."""
    result = update_quiesce.assess_updater_isolation(
        _plan(_runtime(4242, GATEWAY_CGROUP)),
        updater_pid=9001,
        cgroup_of=lambda pid: (
            GATEWAY_CGROUP + "/hermes-worker-abc.scope" if pid == 9001 else GATEWAY_CGROUP
        ),
        ancestors_of=lambda pid: [1],
    )
    assert result.isolated is False


def test_updater_parented_to_a_runtime_is_not_isolated():
    """No cgroups on this host (macOS/Windows) — ancestry still decides."""
    result = update_quiesce.assess_updater_isolation(
        _plan(_runtime(4242, unit="ai.hermes.gateway")),
        updater_pid=9001,
        cgroup_of=lambda pid: None,
        ancestors_of=lambda pid: [4242, 1],
    )
    assert result.isolated is False
    assert "4242" in result.reason


def test_no_cgroups_and_no_runtime_ancestor_is_isolated():
    result = update_quiesce.assess_updater_isolation(
        _plan(_runtime(4242, unit="ai.hermes.gateway")),
        updater_pid=9001,
        cgroup_of=lambda pid: None,
        ancestors_of=lambda pid: [500, 1],
    )
    assert result.isolated is True


def test_empty_fleet_is_isolated():
    result = update_quiesce.assess_updater_isolation(
        _plan(),
        updater_pid=9001,
        cgroup_of=lambda pid: GATEWAY_CGROUP,
        ancestors_of=lambda pid: [1],
    )
    assert result.isolated is True


def test_probe_failure_fails_closed():
    """An unreadable ancestry probe is not proof of independence."""

    def boom(pid):
        raise OSError("no /proc")

    result = update_quiesce.assess_updater_isolation(
        _plan(_runtime(4242, GATEWAY_CGROUP)),
        updater_pid=9001,
        cgroup_of=lambda pid: None,
        ancestors_of=boom,
    )
    assert result.isolated is False


def test_updater_pid_itself_is_never_treated_as_its_own_ancestor():
    """A runtime row that (wrongly) names the updater's PID must not
    make isolation unprovable forever — but it IS a real conflict, so
    fail closed rather than silently ignoring it."""
    result = update_quiesce.assess_updater_isolation(
        _plan(_runtime(9001, GATEWAY_CGROUP)),
        updater_pid=9001,
        cgroup_of=lambda pid: None,
        ancestors_of=lambda pid: [1],
    )
    assert result.isolated is False


SESSION_SCOPE = "/user.slice/user-1000.slice/session-3.scope"


def test_sharing_a_plain_login_session_scope_is_not_a_conflict():
    """Only a SUPERVISOR unit cgroup is lethal.

    A manually started gateway in the same login session is stopped by
    PID — nothing tears the shared session scope down, so the updater is
    not collateral damage. Treating this as a conflict would make every
    ``hermes update`` run from the same terminal that launched a manual
    gateway abort for no reason.
    """
    result = update_quiesce.assess_updater_isolation(
        _plan(_runtime(4242, SESSION_SCOPE, unit="")),
        updater_pid=9001,
        cgroup_of=lambda pid: SESSION_SCOPE,
        ancestors_of=lambda pid: [1],
    )
    assert result.isolated is True


def test_sharing_a_service_unit_cgroup_is_still_a_conflict():
    result = update_quiesce.assess_updater_isolation(
        _plan(_runtime(4242, GATEWAY_CGROUP)),
        updater_pid=9001,
        cgroup_of=lambda pid: GATEWAY_CGROUP,
        ancestors_of=lambda pid: [1],
    )
    assert result.isolated is False
