"""`hermes update` must not touch git until the fleet is provably stopped.

This drives the real ``_cmd_update_impl`` with git mocked, and asserts on
the ORDER of what happened: every inventoried runtime is stopped, and the
updater's isolation is confirmed, before the first git mutation. A failed
stop or an unisolated updater must abort with nothing mutated.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import main as hermes_main
from hermes_cli import update_cmd, update_quiesce
from hermes_cli.update_inventory import RuntimeRecord, UpdatePlan

GIT_MUTATIONS = ("fetch", "merge", "reset", "pull", "checkout", "stash")


@pytest.fixture(autouse=True)
def _reset_authorization():
    update_quiesce.reset_mutation_authorization()
    update_quiesce.clear_restart_pending_state()
    yield
    update_quiesce.reset_mutation_authorization()
    update_quiesce.clear_restart_pending_state()


def _plan():
    plan = UpdatePlan()
    plan.expected_sha = "a" * 40
    plan.runtimes = [
        RuntimeRecord(
            kind="gateway",
            profile="default",
            pid=4242,
            supervisor="systemd",
            restart_via="systemd",
            unit="hermes-gateway.service",
            unit_scope="user",
        )
    ]
    return plan


def _patch_update_deps(monkeypatch, tmp_path, events, behind=3):
    heads = {"n": 0}

    def run_side_effect(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        if any(f" {verb}" in f" {joined}" for verb in GIT_MUTATIONS):
            events.append(f"git:{joined}")
        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return SimpleNamespace(returncode=0, stdout="main\n", stderr="")
        if "rev-list" in joined:
            return SimpleNamespace(returncode=0, stdout=f"{behind}\n", stderr="")
        if joined.endswith("rev-parse HEAD"):
            if behind == 0 or heads["n"] == 0:
                heads["n"] += 1
                return SimpleNamespace(returncode=0, stdout="a" * 40 + "\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="b" * 40 + "\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(hermes_main.subprocess, "run", run_side_effect)
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(hermes_main, "_resolve_update_branch", lambda args: "main")
    monkeypatch.setattr(hermes_main, "_is_windows", lambda: False)
    monkeypatch.setattr(
        hermes_main,
        "_get_origin_url",
        lambda *a, **k: "https://github.com/NousResearch/hermes-agent.git",
    )
    monkeypatch.setattr(hermes_main, "_is_fork", lambda *a, **k: False)
    monkeypatch.setattr(
        hermes_main, "_stash_local_changes_if_needed", lambda *a, **k: None
    )
    monkeypatch.setattr(hermes_main, "_clear_bytecode_cache", lambda *a, **k: 0)
    monkeypatch.setattr(
        hermes_main, "_record_bytecode_fingerprint", lambda *a, **k: None
    )
    monkeypatch.setattr(hermes_main, "_run_pre_update_backup", lambda *a, **k: None)
    monkeypatch.setattr(
        hermes_main, "_pause_windows_gateways_for_update", lambda: None
    )
    monkeypatch.setattr(
        hermes_main, "_resume_windows_gateways_after_update", lambda *a, **k: None
    )
    monkeypatch.setattr(hermes_main, "_write_update_incomplete_marker", lambda: None)
    monkeypatch.setattr(hermes_main, "_clear_update_incomplete_marker", lambda: None)
    monkeypatch.setattr(
        hermes_main, "_finish_dashboard_update_cleanup", lambda *a, **k: None
    )
    monkeypatch.setattr(hermes_main, "_build_web_ui", lambda *a, **k: None)
    monkeypatch.setattr(update_cmd, "_venv_core_imports_healthy", lambda: (True, ""))
    # Never let the update touch the real venv from a test: `.[all]` has no
    # dev extras, so a real install would delete pytest out from under the
    # runner. The dependency phase is covered by its own tests.
    installs: list = []
    monkeypatch.setattr(
        hermes_main,
        "_install_python_dependencies_with_optional_fallback",
        lambda *a, **k: installs.append(a),
    )
    monkeypatch.setattr(
        hermes_main, "_verify_core_dependencies_installed", lambda *a, **k: None
    )
    monkeypatch.setattr(update_cmd, "_editable_install_is_current", lambda *a, **k: True)
    monkeypatch.setattr(
        update_cmd, "_refresh_active_lazy_features", lambda *a, **k: None
    )
    monkeypatch.setattr(
        update_cmd, "_restore_active_tool_dependencies", lambda *a, **k: None
    )
    monkeypatch.setattr(
        update_cmd, "_refresh_active_memory_provider_dependencies", lambda *a, **k: None
    )
    monkeypatch.setattr("hermes_cli.managed_uv.ensure_uv", lambda *a, **k: None)
    monkeypatch.setattr("hermes_cli.managed_uv.update_managed_uv", lambda *a, **k: None)
    # No network, no desktop rebuild, no host mutation from a unit test.
    monkeypatch.setattr(
        update_cmd, "_refresh_bootstrap_cache_scripts", lambda *a, **k: None
    )
    monkeypatch.setattr(update_cmd, "_ensure_acp_launcher", lambda *a, **k: None)
    monkeypatch.setattr(update_cmd, "_ensure_fhs_path_guard", lambda *a, **k: None)
    monkeypatch.setattr(
        update_cmd, "_rebuild_desktop_after_update", lambda *a, **k: True
    )
    monkeypatch.setattr(
        "tools.skills_sync.sync_skills",
        lambda *a, **k: {"new": [], "updated": [], "skipped": []},
    )
    monkeypatch.setattr(update_cmd, "_update_node_dependencies", lambda: [])
    monkeypatch.setattr(update_cmd, "_discard_lockfile_churn", lambda *a, **k: None)
    monkeypatch.setattr(update_cmd, "_normalize_managed_eol", lambda *a, **k: None)

    import hermes_cli.gateway as hermes_gateway

    monkeypatch.setattr(
        hermes_gateway, "find_gateway_pids", lambda all_profiles=False: []
    )
    monkeypatch.setattr(hermes_gateway, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(
        hermes_gateway, "find_profile_gateway_processes", lambda *a, **k: []
    )
    monkeypatch.setattr(
        "hermes_cli.update_receipt.collect_fleet_versions", lambda **k: []
    )
    monkeypatch.setattr(
        "hermes_cli.update_inventory.collect_runtime_inventory", _plan
    )
    # The post-update fleet probe polls a real 30s settle window whenever the
    # restart phase reports work. There is no live gateway here to publish a
    # state stamp, so short-circuit the probe: these tests pin the ORDER of
    # stop-vs-mutate, not the fleet version matrix (which has its own suite).
    for _module in (update_cmd, hermes_main):
        monkeypatch.setattr(
            _module, "_fleet_probe_expected_runtimes", lambda *a, **k: False
        )


def _args():
    return SimpleNamespace(branch=None, yes=True, force=False, force_venv=False)


def _install_fleet(monkeypatch, events, *, stop_ok=True, isolated=True):
    alive = {4242}

    def _stop(runtime):
        events.append(f"stop:{runtime.pid}")
        if not stop_ok:
            return False
        alive.discard(runtime.pid)
        return True

    for module in (update_cmd, hermes_main):
        monkeypatch.setattr(module, "_stop_runtime_for_quiesce", _stop)
        monkeypatch.setattr(
            module, "_runtime_pid_alive", lambda pid: pid in alive
        )
    monkeypatch.setattr(
        update_quiesce,
        "assess_updater_isolation",
        lambda plan, **kw: update_quiesce.IsolationResult(
            isolated=isolated, reason="test" if isolated else "shares gateway cgroup"
        ),
    )
    return alive


def test_fleet_is_quiesced_before_any_git_mutation(monkeypatch, tmp_path, capsys):
    events: list[str] = []
    _patch_update_deps(monkeypatch, tmp_path, events)
    _install_fleet(monkeypatch, events)

    try:
        update_cmd._cmd_update_impl(_args(), gateway_mode=False)
    except SystemExit:
        pass

    assert "stop:4242" in events, events
    merge_events = [
        i for i, e in enumerate(events) if e.startswith("git:") and " merge" in e
    ]
    assert merge_events, "the test harness must observe the git merge"
    assert events.index("stop:4242") < merge_events[0], events


def test_failed_stop_aborts_before_git_mutation(monkeypatch, tmp_path, capsys):
    events: list[str] = []
    _patch_update_deps(monkeypatch, tmp_path, events)
    _install_fleet(monkeypatch, events, stop_ok=False)

    with pytest.raises(SystemExit) as excinfo:
        update_cmd._cmd_update_impl(_args(), gateway_mode=False)

    assert excinfo.value.code == 1
    assert "stop:4242" in events
    assert not [
        e for e in events if e.startswith("git:") and " merge" in e
    ], events


def test_unisolated_updater_aborts_before_any_stop_or_mutation(
    monkeypatch, tmp_path, capsys
):
    events: list[str] = []
    _patch_update_deps(monkeypatch, tmp_path, events)
    _install_fleet(monkeypatch, events, isolated=False)

    with pytest.raises(SystemExit) as excinfo:
        update_cmd._cmd_update_impl(_args(), gateway_mode=False)

    assert excinfo.value.code == 1
    assert not [e for e in events if e.startswith("stop:")], events
    assert not [
        e for e in events if e.startswith("git:") and " merge" in e
    ], events
    out = capsys.readouterr().out
    assert "/update" in out or "external shell" in out


def test_mutation_gate_refuses_without_a_confirmed_quiesce():
    """The gate is real: an unquiesced code path cannot mutate."""
    update_quiesce.reset_mutation_authorization()
    with pytest.raises(SystemExit):
        update_cmd._require_quiesced("git")


def test_update_that_changes_nothing_never_stops_the_fleet(monkeypatch, tmp_path):
    """A clean, already-up-to-date `hermes update` writes nothing, so it
    must not cost every gateway on the box a restart."""
    events: list[str] = []
    _patch_update_deps(monkeypatch, tmp_path, events, behind=0)
    _install_fleet(monkeypatch, events)

    try:
        update_cmd._cmd_update_impl(_args(), gateway_mode=False)
    except SystemExit:
        pass

    assert not [e for e in events if e.startswith("stop:")], events
    assert update_quiesce.read_restart_pending_state() is None


def test_real_update_leaves_a_relaunch_record_and_restores_the_fleet(
    monkeypatch, tmp_path
):
    """Once the fleet is stopped the update owes it a relaunch, and that
    obligation is durable."""
    events: list[str] = []
    _patch_update_deps(monkeypatch, tmp_path, events)
    _install_fleet(monkeypatch, events)

    try:
        update_cmd._cmd_update_impl(_args(), gateway_mode=False)
    except SystemExit:
        pass

    state = update_quiesce.read_restart_pending_state()
    assert state is not None, "a stopped fleet must leave a relaunch record"
    assert [r["unit"] for r in state["runtimes"]] == ["hermes-gateway.service"]

    supervisor_calls: list = []
    monkeypatch.setattr(
        update_cmd,
        "_run_supervisor_command",
        lambda argv: (supervisor_calls.append(list(argv)) or True),
    )
    monkeypatch.setattr(
        hermes_main,
        "_probe_relaunched_runtime_sha",
        lambda record, _new_pid=None: "b" * 40,
    )
    outcomes = update_cmd._relaunch_quiesced_runtimes("b" * 40)

    # Relaunched by the EXACT recorded unit, in its recorded scope.
    assert supervisor_calls == [
        [
            "systemctl",
            "--user",
            "--no-ask-password",
            "restart",
            "hermes-gateway.service",
        ]
    ]
    assert update_quiesce.relaunch_is_complete(outcomes) is True
    assert update_quiesce.read_restart_pending_state() is None


def test_command_boundary_relaunches_a_quiesced_fleet(monkeypatch, tmp_path):
    """`cmd_update`'s finally is the backstop: however the impl exits, a
    fleet we stopped must not be left down."""
    calls: list = []
    monkeypatch.setattr(
        hermes_main, "_relaunch_quiesced_runtimes", lambda *a, **k: calls.append(a) or []
    )
    monkeypatch.setattr(
        hermes_main, "_cmd_update_impl", lambda *a, **k: (_ for _ in ()).throw(SystemExit(3))
    )
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".git").mkdir()

    with pytest.raises(SystemExit):
        hermes_main.cmd_update(_args())

    assert calls, "the command boundary must attempt the relaunch"


# ---------------------------------------------------------------------------
# The restart phase must not discard what the quiesce relaunch achieved
# ---------------------------------------------------------------------------
#
# `_cmd_update_impl` initialises `failed_or_stale_units` / `killed_pids` /
# `relaunched_profiles` / `externally_supervised_profiles` once, fills them
# from the quiesced-runtime relaunch, and then — inside the platform restart
# block — used to re-initialise all four to empty. Everything the relaunch
# recorded was erased before the summary, the reconciliation and the receipt
# ever saw it: a runtime that WAS relaunched read as unaccounted, and one that
# FAILED to come back read as a clean update.
#
# These assert on the impl's own stdout rather than on a patched receipt
# function, because the restart phase runs after `_purge_stale_hermes_modules`
# — a monkeypatched `hermes_cli.update_receipt` attribute is dropped from
# sys.modules and the phase re-imports the real one.


def _mixed_plan():
    plan = UpdatePlan()
    plan.expected_sha = "a" * 40
    plan.runtimes = [
        RuntimeRecord(
            kind="gateway",
            profile="default",
            pid=4242,
            supervisor="systemd",
            restart_via="systemd",
            unit="acme-gateway.service",
            unit_scope="user",
        ),
        RuntimeRecord(
            kind="serve",
            profile="edge",
            pid=4343,
            supervisor="manual-serve",
            restart_via="respawn-argv",
            detail={
                "argv_list": ["hermes", "serve", "--port", "9119"],
                "argv": "hermes serve --port 9119",
                "start_time": 111.0,
            },
        ),
    ]
    return plan


def _run_impl_over_a_quiesced_fleet(
    monkeypatch, tmp_path, capsys, *, unit_restart_ok=True
):
    events: list[str] = []
    _patch_update_deps(monkeypatch, tmp_path, events)
    monkeypatch.setattr(
        "hermes_cli.update_inventory.collect_runtime_inventory", _mixed_plan
    )

    alive = {4242, 4343}

    def _stop(runtime):
        events.append(f"stop:{runtime.pid}")
        alive.discard(runtime.pid)
        return True

    for module in (update_cmd, hermes_main):
        monkeypatch.setattr(module, "_stop_runtime_for_quiesce", _stop)
        monkeypatch.setattr(module, "_runtime_pid_alive", lambda pid: pid in alive)
        monkeypatch.setattr(
            module, "_probe_relaunched_runtime_sha", lambda *a, **k: "b" * 40
        )
        monkeypatch.setattr(module, "_respawn_recorded_runtime", lambda *a, **k: 5151)
    monkeypatch.setattr(
        update_quiesce,
        "assess_updater_isolation",
        lambda plan, **kw: update_quiesce.IsolationResult(isolated=True, reason="t"),
    )
    monkeypatch.setattr(
        update_cmd, "_run_supervisor_command", lambda argv: unit_restart_ok
    )

    try:
        update_cmd._cmd_update_impl(_args(), gateway_mode=False)
    except SystemExit:
        pass
    return capsys.readouterr().out, events


def test_restart_phase_keeps_the_quiesce_relaunch_bookkeeping(
    monkeypatch, tmp_path, capsys
):
    """A successful relaunch survives to the summary and reconciliation."""
    out, events = _run_impl_over_a_quiesced_fleet(monkeypatch, tmp_path, capsys)

    assert "stop:4242" in events and "stop:4343" in events, events
    # relaunched_profiles survived: the serve came back on its recorded argv.
    assert "Restarting manual gateway profile(s): edge" in out, out
    assert "Restarted acme-gateway.service" in out, out
    # killed_pids survived, so the reconciliation can account for both rows
    # instead of reporting the fleet it just relaunched as never touched.
    assert "never touched" not in out, out
    assert "Stopped 2 manual gateway process(es)" not in out, out


def test_restart_phase_keeps_a_failed_relaunch_visible(
    monkeypatch, tmp_path, capsys
):
    """A unit that did NOT come back must not be erased into a clean run."""
    out, _events = _run_impl_over_a_quiesced_fleet(
        monkeypatch, tmp_path, capsys, unit_restart_ok=False
    )

    assert "Update incomplete" in out, out
    assert "acme-gateway.service" in out, out
