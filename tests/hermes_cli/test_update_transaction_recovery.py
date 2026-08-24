"""Failure compensation covers Git, dependencies, and dashboard mutations."""

from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture()
def recovery_harness(monkeypatch, tmp_path: Path):
    import hermes_cli.update_cmd as update_cmd
    import hermes_cli.update_rollout as rollout

    checkpoint = tmp_path / "checkpoint"
    project = tmp_path / "checkout"
    calls: list[tuple[str, dict]] = []

    monkeypatch.setattr(
        rollout,
        "read_checkpoint",
        lambda path: {"pre_sha": "a" * 40},
    )
    monkeypatch.setattr(
        update_cmd,
        "_capture_head_sha",
        lambda command, root: "a" * 40,
    )

    def restore(checkpoint_arg, plan, **kwargs):
        calls.append(("restore", kwargs))
        return {"attempted": True, "restored": True, "verified": True}

    def restart(plan, **kwargs):
        calls.append(("restart", kwargs))
        return {"verified": True, "restarted_profiles": ["canary"]}

    monkeypatch.setattr(rollout, "restore_and_verify_fleet", restore)
    monkeypatch.setattr(rollout, "restart_and_verify_fleet", restart)
    monkeypatch.setattr(
        rollout,
        "dependency_state_matches_checkpoint",
        lambda checkpoint, project: True,
    )
    monkeypatch.setattr(
        rollout,
        "web_dist_state_matches_checkpoint",
        lambda checkpoint, project: True,
    )

    return SimpleNamespace(
        update_cmd=update_cmd,
        rollout=rollout,
        checkpoint=checkpoint,
        project=project,
        calls=calls,
    )


def _recover(harness, **overrides):
    values = {
        "config": SimpleNamespace(enabled=True),
        "project_root": harness.project,
        "reason": "test failure",
        "apply_started": False,
        "fleet_quiesced": True,
    }
    values.update(overrides)
    return harness.update_cmd._recover_rollout_transaction(
        harness.checkpoint,
        SimpleNamespace(runtimes=[]),
        **values,
    )


def test_dependency_only_mutation_restores_even_when_head_is_unchanged(
    monkeypatch, recovery_harness
):
    monkeypatch.setattr(
        recovery_harness.rollout,
        "dependency_state_matches_checkpoint",
        lambda checkpoint, project: False,
    )

    result = _recover(recovery_harness)

    assert [name for name, _ in recovery_harness.calls] == ["restore"]
    assert recovery_harness.calls[0][1]["transaction_owned_reset"] is True
    assert result["disk_mutated"] is True
    assert result["recovery_only"] is False


def test_dashboard_only_mutation_restores_even_when_head_is_unchanged(
    monkeypatch, recovery_harness
):
    monkeypatch.setattr(
        recovery_harness.rollout,
        "web_dist_state_matches_checkpoint",
        lambda checkpoint, project: False,
    )

    result = _recover(recovery_harness)

    assert [name for name, _ in recovery_harness.calls] == ["restore"]
    assert recovery_harness.calls[0][1]["transaction_owned_reset"] is True
    assert result["disk_mutated"] is True
    assert result["recovery_only"] is False


def test_apply_started_restores_even_when_identity_still_matches(
    monkeypatch, recovery_harness
):
    monkeypatch.setattr(
        recovery_harness.rollout,
        "dependency_state_matches_checkpoint",
        lambda checkpoint, project: True,
    )

    result = _recover(recovery_harness, apply_started=True)

    assert [name for name, _ in recovery_harness.calls] == ["restore"]
    assert result["restored"] is True


def test_preapply_failure_restarts_quiesced_fleet_without_reset(
    monkeypatch, recovery_harness
):
    monkeypatch.setattr(
        recovery_harness.rollout,
        "dependency_state_matches_checkpoint",
        lambda checkpoint, project: True,
    )

    result = _recover(recovery_harness)

    assert [name for name, _ in recovery_harness.calls] == ["restart"]
    assert recovery_harness.calls[0][1]["expected_sha"] == "a" * 40
    assert result["recovery_only"] is True
    assert result["restored"] is False


def test_no_mutation_and_no_quiesce_needs_no_compensation(
    monkeypatch, recovery_harness
):
    monkeypatch.setattr(
        recovery_harness.rollout,
        "dependency_state_matches_checkpoint",
        lambda checkpoint, project: True,
    )

    result = _recover(recovery_harness, fleet_quiesced=False)

    assert recovery_harness.calls == []
    assert result == {
        "attempted": False,
        "restore_attempted": False,
        "restored": False,
        "verified": True,
        "reason": "test failure",
        "recovery_only": False,
        "disk_mutated": False,
    }


def test_after_restore_hook_is_forwarded(monkeypatch, recovery_harness):
    monkeypatch.setattr(
        recovery_harness.rollout,
        "dependency_state_matches_checkpoint",
        lambda checkpoint, project: False,
    )
    hook = object()

    _recover(recovery_harness, after_restore=hook)

    assert recovery_harness.calls[0][1]["after_restore"] is hook


def test_canary_coordinator_refusal_precedes_checkpoint_and_apply(
    monkeypatch, tmp_path: Path
):
    import hermes_cli.config as cli_config
    import hermes_cli.update_cmd as update_cmd
    import hermes_cli.update_inventory as inventory
    import hermes_cli.update_receipt as receipt
    import hermes_cli.update_rollout as rollout

    plan = SimpleNamespace(runtimes=[], install_method="git", updatable_in_place=True)
    config = SimpleNamespace(
        enabled=True,
        canary_profile="canary",
        to_dict=lambda: {"canary_profile": "canary"},
    )
    mutations: list[str] = []

    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        update_cmd._m(), "_capture_active_lazy_features", lambda: []
    )
    monkeypatch.setattr(
        update_cmd._m(), "_capture_active_tool_dependencies", lambda: []
    )
    monkeypatch.setattr(update_cmd, "_read_project_version", lambda: "0.0.0")
    monkeypatch.setattr(
        update_cmd,
        "_new_update_context",
        lambda gateway_mode: ("correlation", {"surface": "terminal"}),
    )
    monkeypatch.setattr(cli_config, "load_config", lambda: {})
    monkeypatch.setattr(receipt, "begin_update_receipt", lambda: None)
    monkeypatch.setattr(receipt, "finalize_update_receipt", lambda *a, **k: None)
    monkeypatch.setattr(receipt, "record_canary", lambda **kwargs: None)
    monkeypatch.setattr(inventory, "collect_runtime_inventory", lambda: plan)
    monkeypatch.setattr(inventory, "record_plan_in_receipt", lambda plan: None)
    monkeypatch.setattr(rollout, "load_rollout_config", lambda data=None: config)
    monkeypatch.setattr(rollout, "validate_rollout_plan", lambda plan, cfg: {})
    monkeypatch.setattr(
        rollout,
        "validate_rollout_coordinator",
        lambda project: (_ for _ in ()).throw(rollout.RolloutError("external required")),
    )
    monkeypatch.setattr(
        rollout,
        "create_checkpoint",
        lambda *a, **k: mutations.append("checkpoint"),
    )

    with pytest.raises(SystemExit) as raised:
        update_cmd._cmd_update_impl(
            SimpleNamespace(
                yes=True,
                keep_stash=False,
                switch_branch=False,
                rollback=None,
            ),
            gateway_mode=False,
        )

    assert raised.value.code == 2
    assert mutations == []


def test_explicit_rollback_coordinator_refusal_precedes_quiesce_and_restore(
    monkeypatch, tmp_path: Path
):
    import hermes_cli.update_cmd as update_cmd
    import hermes_cli.update_inventory as inventory
    import hermes_cli.update_receipt as receipt
    import hermes_cli.update_rollout as rollout

    checkpoint = tmp_path / "checkpoint"
    plan = SimpleNamespace(runtimes=[])
    config = SimpleNamespace(enabled=True, canary_profile="canary")
    mutations: list[str] = []

    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        update_cmd,
        "_new_update_context",
        lambda gateway_mode: ("correlation", {"surface": "terminal"}),
    )
    for name in (
        "begin_update_receipt",
        "record_canary",
        "record_checkpoint",
        "record_gateway_restart",
        "record_rollback",
    ):
        monkeypatch.setattr(receipt, name, lambda *a, **k: None)
    monkeypatch.setattr(receipt, "finalize_update_receipt", lambda *a, **k: None)
    monkeypatch.setattr(rollout, "resolve_checkpoint", lambda ref, root: checkpoint)
    monkeypatch.setattr(
        rollout,
        "read_checkpoint",
        lambda path: {"id": "checkpoint", "pre_sha": "a" * 40, "rollout": {}},
    )
    monkeypatch.setattr(rollout, "load_rollout_config", lambda data=None: config)
    monkeypatch.setattr(rollout, "plan_from_checkpoint", lambda data, current: plan)
    monkeypatch.setattr(rollout, "validate_rollout_plan", lambda plan, cfg: {})
    monkeypatch.setattr(
        rollout,
        "validate_rollout_coordinator",
        lambda project: (_ for _ in ()).throw(rollout.RolloutError("external required")),
    )
    monkeypatch.setattr(
        rollout,
        "quiesce_rollout_fleet",
        lambda *a, **k: mutations.append("quiesce"),
    )
    monkeypatch.setattr(
        rollout,
        "restore_checkpoint",
        lambda *a, **k: mutations.append("restore"),
    )
    monkeypatch.setattr(inventory, "collect_runtime_inventory", lambda: plan)
    monkeypatch.setattr(
        update_cmd.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("git preflight ran")),
    )

    with pytest.raises(SystemExit) as raised:
        update_cmd._cmd_update_rollback(
            SimpleNamespace(rollback="checkpoint"), gateway_mode=False
        )

    assert raised.value.code == 2
    assert mutations == []


def test_explicit_rollback_refuses_live_dashboard_before_quiesce(
    monkeypatch, tmp_path: Path
):
    import hermes_cli.dashboard_procs as dashboard_procs
    import hermes_cli.update_cmd as update_cmd
    import hermes_cli.update_inventory as inventory
    import hermes_cli.update_receipt as receipt
    import hermes_cli.update_rollout as rollout

    checkpoint = tmp_path / "checkpoint"
    plan = SimpleNamespace(runtimes=[])
    config = SimpleNamespace(enabled=True, canary_profile="canary")
    mutations: list[str] = []

    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        update_cmd,
        "_new_update_context",
        lambda gateway_mode: ("correlation", {"surface": "terminal"}),
    )
    for name in (
        "begin_update_receipt",
        "record_canary",
        "record_checkpoint",
        "record_gateway_restart",
        "record_rollback",
        "finalize_update_receipt",
    ):
        monkeypatch.setattr(receipt, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(rollout, "resolve_checkpoint", lambda ref, root: checkpoint)
    monkeypatch.setattr(
        rollout,
        "read_checkpoint",
        lambda path: {"id": "checkpoint", "pre_sha": "a" * 40, "rollout": {}},
    )
    monkeypatch.setattr(rollout, "load_rollout_config", lambda data=None: config)
    monkeypatch.setattr(rollout, "plan_from_checkpoint", lambda data, current: plan)
    monkeypatch.setattr(rollout, "validate_rollout_plan", lambda plan, cfg: {})
    monkeypatch.setattr(rollout, "validate_rollout_coordinator", lambda project: None)
    monkeypatch.setattr(
        dashboard_procs,
        "_scan_dashboard_processes",
        lambda **kwargs: [(4321, "python -m hermes_cli.main dashboard")],
    )
    monkeypatch.setattr(
        rollout,
        "quiesce_rollout_fleet",
        lambda *args, **kwargs: mutations.append("quiesce"),
    )
    monkeypatch.setattr(
        rollout,
        "restore_checkpoint",
        lambda *args, **kwargs: mutations.append("restore"),
    )
    monkeypatch.setattr(inventory, "collect_runtime_inventory", lambda: plan)
    monkeypatch.setattr(
        update_cmd.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Git preflight ran")
        ),
    )

    with pytest.raises(SystemExit) as raised:
        update_cmd._cmd_update_rollback(
            SimpleNamespace(rollback="checkpoint"), gateway_mode=False
        )

    assert raised.value.code == 2
    assert mutations == []


def test_explicit_rollback_dashboard_scan_error_fails_closed(
    monkeypatch
):
    import hermes_cli.dashboard_procs as dashboard_procs
    import hermes_cli.update_cmd as update_cmd
    import hermes_cli.update_rollout as rollout

    monkeypatch.setattr(
        dashboard_procs,
        "_scan_dashboard_processes",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("ps timed out")),
    )

    with pytest.raises(rollout.RolloutError, match="could not prove"):
        update_cmd._assert_no_live_dashboard_for_rollback()


def test_explicit_rollback_fails_if_post_restore_dashboard_survives(
    monkeypatch,
):
    import hermes_cli.dashboard_procs as dashboard_procs
    import hermes_cli.update_cmd as update_cmd
    import hermes_cli.update_rollout as rollout

    live = [(4321, "python -m hermes_cli.main dashboard")]
    monkeypatch.setattr(
        dashboard_procs, "_scan_dashboard_processes", lambda **kwargs: live
    )
    monkeypatch.setattr(
        update_cmd._m(),
        "_kill_stale_dashboard_processes",
        lambda **kwargs: {
            "matched": [4321],
            "killed": [],
            "failed": [(4321, "access denied")],
            "unrecovered": [],
        },
    )

    with pytest.raises(rollout.RolloutError, match="remained live"):
        update_cmd._stop_dashboards_started_during_rollback()


def test_explicit_rollback_restores_dashboard_before_runtime_restart(
    monkeypatch, tmp_path: Path
):
    import hermes_cli.dashboard_procs as dashboard_procs
    import hermes_cli.update_cmd as update_cmd
    import hermes_cli.update_inventory as inventory
    import hermes_cli.update_receipt as receipt
    import hermes_cli.update_rollout as rollout

    checkpoint = tmp_path / "checkpoint"
    web_dist = tmp_path / "hermes_cli" / "web_dist"
    (web_dist / "assets").mkdir(parents=True)
    (web_dist / "index.html").write_text("candidate", encoding="utf-8")
    (web_dist / "assets" / "app.js").write_bytes(b"candidate-asset")
    plan = SimpleNamespace(runtimes=[])
    config = SimpleNamespace(enabled=True, canary_profile="canary")
    events: list[str] = []
    scans: list[str] = []
    dashboard_live = False

    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        update_cmd,
        "_new_update_context",
        lambda gateway_mode: ("correlation", {"surface": "terminal"}),
    )
    monkeypatch.setattr(update_cmd, "_purge_stale_hermes_modules", lambda: None)
    for name in (
        "begin_update_receipt",
        "record_canary",
        "record_checkpoint",
        "record_gateway_restart",
        "record_rollback",
    ):
        monkeypatch.setattr(receipt, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(
        receipt, "finalize_update_receipt", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(rollout, "resolve_checkpoint", lambda ref, root: checkpoint)
    monkeypatch.setattr(
        rollout,
        "read_checkpoint",
        lambda path: {"id": "checkpoint", "pre_sha": "a" * 40, "rollout": {}},
    )
    monkeypatch.setattr(rollout, "load_rollout_config", lambda data=None: config)
    monkeypatch.setattr(rollout, "plan_from_checkpoint", lambda data, current: plan)
    monkeypatch.setattr(rollout, "validate_rollout_plan", lambda plan, cfg: {})
    monkeypatch.setattr(rollout, "validate_rollout_coordinator", lambda project: None)
    def scan_dashboards(**kwargs):
        scans.append("scan")
        if dashboard_live:
            return [(4321, "python -m hermes_cli.main dashboard")]
        return []

    monkeypatch.setattr(dashboard_procs, "_scan_dashboard_processes", scan_dashboards)
    monkeypatch.setattr(
        rollout,
        "rollout_confirmation_context",
        lambda **kwargs: {"kind": "update_confirmation"},
    )
    monkeypatch.setattr(
        rollout,
        "quiesce_rollout_fleet",
        lambda *args, **kwargs: {
            "ok": True,
            "quiesced_profiles": ["canary"],
            "attempted_profiles": ["canary"],
            "errors": [],
        },
    )

    def restore(*args, **kwargs):
        nonlocal dashboard_live
        events.append("restore")
        (web_dist / "index.html").write_text("checkpoint", encoding="utf-8")
        (web_dist / "assets" / "app.js").write_bytes(b"checkpoint-asset")
        # A manual dashboard wins after the pre-restore scan but before the
        # staged venv/web generation has finished swapping into place.
        dashboard_live = True
        return {"restored": True, "verified": True}

    def stop_dashboards(*args, **kwargs):
        nonlocal dashboard_live
        events.append("stop-dashboard")
        assert kwargs["restart_managed"] is False
        dashboard_live = False
        return {
            "matched": [4321],
            "killed": [4321],
            "failed": [],
            "unrecovered": [4321],
        }

    def restart(*args, **kwargs):
        events.append("restart")
        assert (web_dist / "index.html").read_text(encoding="utf-8") == "checkpoint"
        assert (web_dist / "assets" / "app.js").read_bytes() == b"checkpoint-asset"
        return {
            "verified": True,
            "restarted_profiles": ["canary"],
            "errors": [],
        }

    monkeypatch.setattr(rollout, "restore_checkpoint", restore)
    monkeypatch.setattr(rollout, "restart_and_verify_fleet", restart)
    monkeypatch.setattr(
        update_cmd._m(), "_kill_stale_dashboard_processes", stop_dashboards
    )
    monkeypatch.setattr(inventory, "collect_runtime_inventory", lambda: plan)
    monkeypatch.setattr(
        update_cmd, "_capture_head_sha", lambda command, root: "b" * 40
    )
    monkeypatch.setattr(
        update_cmd.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="", stderr="", returncode=0
        ),
    )

    update_cmd._cmd_update_rollback(
        SimpleNamespace(rollback="checkpoint"), gateway_mode=False
    )

    assert events == ["restore", "stop-dashboard", "restart"]
    assert scans == ["scan", "scan", "scan", "scan"]


def test_explicit_rollback_restarts_profiles_stopped_by_failed_quiesce(
    monkeypatch, tmp_path: Path
):
    import hermes_cli.update_cmd as update_cmd
    import hermes_cli.update_inventory as inventory
    import hermes_cli.update_receipt as receipt
    import hermes_cli.update_rollout as rollout

    checkpoint = tmp_path / "checkpoint"
    plan = SimpleNamespace(runtimes=[])
    config = SimpleNamespace(enabled=True, canary_profile="canary")
    restarted: list[dict] = []
    rollback_records: list[dict] = []

    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        update_cmd,
        "_new_update_context",
        lambda gateway_mode: ("correlation", {"surface": "terminal"}),
    )
    for name in (
        "begin_update_receipt",
        "record_canary",
        "record_checkpoint",
        "record_gateway_restart",
    ):
        monkeypatch.setattr(receipt, name, lambda *a, **k: None)
    monkeypatch.setattr(
        receipt,
        "record_rollback",
        lambda **kwargs: rollback_records.append(kwargs),
    )
    monkeypatch.setattr(receipt, "finalize_update_receipt", lambda *a, **k: None)
    monkeypatch.setattr(rollout, "resolve_checkpoint", lambda ref, root: checkpoint)
    monkeypatch.setattr(
        rollout,
        "read_checkpoint",
        lambda path: {"id": "checkpoint", "pre_sha": "a" * 40, "rollout": {}},
    )
    monkeypatch.setattr(rollout, "load_rollout_config", lambda data=None: config)
    monkeypatch.setattr(rollout, "plan_from_checkpoint", lambda data, current: plan)
    monkeypatch.setattr(rollout, "validate_rollout_plan", lambda plan, cfg: {})
    monkeypatch.setattr(rollout, "validate_rollout_coordinator", lambda project: None)
    monkeypatch.setattr(
        rollout,
        "rollout_confirmation_context",
        lambda **kwargs: {"kind": "update_confirmation"},
    )
    monkeypatch.setattr(
        rollout,
        "quiesce_rollout_fleet",
        lambda *a, **k: {
            "ok": False,
            "quiesced_profiles": ["canary"],
            "attempted_profiles": ["canary", "later"],
            "errors": [{"profile": "later", "error": "stop failed"}],
        },
    )

    def restart(plan_arg, **kwargs):
        restarted.append(kwargs)
        return {
            "verified": True,
            "restarted_profiles": ["canary"],
            "errors": [],
        }

    monkeypatch.setattr(rollout, "restart_and_verify_fleet", restart)
    monkeypatch.setattr(
        rollout,
        "restore_checkpoint",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("restore ran")),
    )
    monkeypatch.setattr(inventory, "collect_runtime_inventory", lambda: plan)
    monkeypatch.setattr(
        update_cmd,
        "_capture_head_sha",
        lambda command, root: "b" * 40,
    )
    monkeypatch.setattr(
        update_cmd.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout="", stderr="", returncode=0),
    )

    with pytest.raises(SystemExit) as raised:
        update_cmd._cmd_update_rollback(
            SimpleNamespace(rollback="checkpoint"), gateway_mode=False
        )

    assert raised.value.code == 1
    assert restarted == [
        {
            "expected_sha": "b" * 40,
            "config": config,
            "project_root": tmp_path,
            "profiles": ["canary", "later"],
        }
    ]
    assert rollback_records[-1]["restarted_profiles"] == ["canary"]
    assert rollback_records[-1]["recovery_error"] is None


def test_explicit_rollback_interrupt_during_quiesce_restarts_full_fleet(
    monkeypatch, tmp_path: Path
):
    import hermes_cli.update_cmd as update_cmd
    import hermes_cli.update_inventory as inventory
    import hermes_cli.update_receipt as receipt
    import hermes_cli.update_rollout as rollout

    class AbortQuiesce(BaseException):
        pass

    checkpoint = tmp_path / "checkpoint"
    plan = SimpleNamespace(runtimes=[])
    config = SimpleNamespace(enabled=True, canary_profile="canary")
    restarted: list[dict] = []

    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        update_cmd,
        "_new_update_context",
        lambda gateway_mode: ("correlation", {"surface": "terminal"}),
    )
    for name in (
        "begin_update_receipt",
        "record_canary",
        "record_checkpoint",
        "record_gateway_restart",
        "record_rollback",
        "finalize_update_receipt",
    ):
        monkeypatch.setattr(receipt, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(rollout, "resolve_checkpoint", lambda ref, root: checkpoint)
    monkeypatch.setattr(
        rollout,
        "read_checkpoint",
        lambda path: {"id": "checkpoint", "pre_sha": "a" * 40, "rollout": {}},
    )
    monkeypatch.setattr(rollout, "load_rollout_config", lambda data=None: config)
    monkeypatch.setattr(rollout, "plan_from_checkpoint", lambda data, current: plan)
    monkeypatch.setattr(rollout, "validate_rollout_plan", lambda plan, cfg: {})
    monkeypatch.setattr(rollout, "validate_rollout_coordinator", lambda project: None)
    monkeypatch.setattr(
        rollout,
        "rollout_confirmation_context",
        lambda **kwargs: {"kind": "update_confirmation"},
    )
    monkeypatch.setattr(
        rollout,
        "quiesce_rollout_fleet",
        lambda *args, **kwargs: (_ for _ in ()).throw(AbortQuiesce()),
    )

    def restart(plan_arg, **kwargs):
        restarted.append(kwargs)
        return {"verified": True, "restarted_profiles": ["canary"]}

    monkeypatch.setattr(rollout, "restart_and_verify_fleet", restart)
    monkeypatch.setattr(
        rollout,
        "restore_checkpoint",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("restore must not run")
        ),
    )
    monkeypatch.setattr(inventory, "collect_runtime_inventory", lambda: plan)
    monkeypatch.setattr(
        update_cmd,
        "_capture_head_sha",
        lambda command, root: "b" * 40,
    )
    monkeypatch.setattr(
        update_cmd.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="", stderr="", returncode=0
        ),
    )

    with pytest.raises(AbortQuiesce):
        update_cmd._cmd_update_rollback(
            SimpleNamespace(rollback="checkpoint"), gateway_mode=False
        )

    assert restarted == [
        {
            "expected_sha": "b" * 40,
            "config": config,
            "project_root": tmp_path,
        }
    ]
