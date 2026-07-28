"""Gateway startup recovery seam coverage for #1478."""

import pytest

from gateway.config import GatewayConfig


def test_gateway_startup_recovery_delegates_to_the_profile_level_sweep(monkeypatch):
    from gateway.run import _recover_protected_final_results_at_gateway_startup

    monkeypatch.setattr(
        "cron.scheduler.recover_protected_final_result_repairs_for_home",
        lambda: ["durable observer replayed"],
    )

    assert _recover_protected_final_results_at_gateway_startup() == ["durable observer replayed"]


def test_gateway_startup_recovery_reports_when_the_sweep_fails(monkeypatch):
    from gateway.run import _recover_protected_final_results_at_gateway_startup

    monkeypatch.setattr(
        "cron.scheduler.recover_protected_final_result_repairs_for_home",
        lambda: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )

    assert _recover_protected_final_results_at_gateway_startup() == [
        "recovery_sweep_failed:default:unavailable"
    ]


def test_gateway_startup_recovery_sweeps_every_multiplex_profile(monkeypatch):
    from gateway.run import _recover_protected_final_results_at_gateway_startup

    homes = []
    monkeypatch.setattr(
        "cron.scheduler.recover_protected_final_result_repairs_for_home",
        lambda home: homes.append(home) or [],
    )

    assert _recover_protected_final_results_at_gateway_startup([("atlas", "/atlas"), ("yuange", "/yuange")]) == []
    assert homes == ["/atlas", "/yuange"]
    assert _recover_protected_final_results_at_gateway_startup([("atlas",)]) == [
        "recovery_profile_invalid:('atlas',)"
    ]


@pytest.mark.asyncio
async def test_gateway_start_invokes_protected_recovery_before_starting_the_scheduler(monkeypatch, tmp_path):
    import gateway.run as gateway_run

    calls = []

    class RunningRunner:
        def __init__(self, config):
            self.config = config
            self.adapters = {}
            self._running = True
            self.should_exit_cleanly = False
            self.should_exit_with_failure = False
            self.exit_reason = None
            self.exit_code = None
            self._restart_requested = False
            self._restart_via_service = False

        async def start(self):
            return True

        async def wait_for_shutdown(self):
            return None

    class Provider:
        def start(self, _stop, **_kwargs):
            calls.append("scheduler")

        def stop(self):
            return None

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr("gateway.status.acquire_gateway_runtime_lock", lambda: True)
    monkeypatch.setattr("gateway.status.write_pid_file", lambda: None)
    monkeypatch.setattr("gateway.status.remove_pid_file", lambda: None)
    monkeypatch.setattr("gateway.status.release_gateway_runtime_lock", lambda: None)
    monkeypatch.setattr("tools.skills_sync.sync_skills", lambda quiet=True: None)
    monkeypatch.setattr("hermes_logging.setup_logging", lambda hermes_home, mode: tmp_path)
    monkeypatch.setattr("hermes_logging._add_rotating_handler", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "GatewayRunner", RunningRunner)
    monkeypatch.setattr("cron.scheduler_provider.resolve_cron_scheduler", lambda: Provider())
    monkeypatch.setattr(gateway_run, "_recover_protected_final_results_at_gateway_startup", lambda: calls.append("recovery") or [])
    monkeypatch.setattr("tools.mcp_tool.shutdown_mcp_servers", lambda: None)

    assert await gateway_run.start_gateway(config=GatewayConfig(), replace=False, verbosity=0) is True
    assert calls[:2] == ["recovery", "scheduler"]


def _patch_gateway_boot_dependencies(monkeypatch, tmp_path, runner_type):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr("gateway.status.acquire_gateway_runtime_lock", lambda: True)
    monkeypatch.setattr("gateway.status.write_pid_file", lambda: None)
    monkeypatch.setattr("gateway.status.remove_pid_file", lambda: None)
    monkeypatch.setattr("gateway.status.release_gateway_runtime_lock", lambda: None)
    monkeypatch.setattr("tools.skills_sync.sync_skills", lambda quiet=True: None)
    monkeypatch.setattr("hermes_logging.setup_logging", lambda hermes_home, mode: tmp_path)
    monkeypatch.setattr("hermes_logging._add_rotating_handler", lambda *args, **kwargs: None)
    monkeypatch.setattr("gateway.run.GatewayRunner", runner_type)
    monkeypatch.setattr("tools.mcp_tool.shutdown_mcp_servers", lambda: None)


@pytest.mark.asyncio
async def test_gateway_start_blocks_before_adapter_start_when_recovery_is_pending(monkeypatch, tmp_path):
    import gateway.run as gateway_run

    calls = []

    class Runner:
        def __init__(self, config):
            self.config = config
            self.adapters = {}
            self._running = True
            self.should_exit_cleanly = False
            self.should_exit_with_failure = False
            self.exit_reason = None
            self.exit_code = None
            self._restart_requested = False
            self._restart_via_service = False

    _patch_gateway_boot_dependencies(monkeypatch, tmp_path, Runner)
    monkeypatch.setattr("tools.mcp_tool.discover_mcp_tools", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(gateway_run, "_recover_protected_final_results_at_gateway_startup", lambda: ["repair pending"])

    assert await gateway_run.start_gateway(config=GatewayConfig(), replace=False, verbosity=0) is False
    assert calls == []


@pytest.mark.asyncio
async def test_gateway_start_blocks_when_multiplex_profile_inventory_cannot_be_resolved(monkeypatch, tmp_path):
    import gateway.run as gateway_run

    calls = []

    class Runner:
        def __init__(self, config):
            self.config = config
            self.adapters = {}
            self._running = True
            self.should_exit_cleanly = False
            self.should_exit_with_failure = False
            self.exit_reason = None
            self.exit_code = None
            self._restart_requested = False
            self._restart_via_service = False

    _patch_gateway_boot_dependencies(monkeypatch, tmp_path, Runner)
    monkeypatch.setattr("hermes_cli.profiles.profiles_to_serve", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("inventory unavailable")))

    assert await gateway_run.start_gateway(config=GatewayConfig(multiplex_profiles=True), replace=False, verbosity=0) is False
    assert calls == []


@pytest.mark.asyncio
async def test_gateway_start_passes_the_recovered_multiplex_profiles_to_inprocess_scheduler(monkeypatch, tmp_path):
    import gateway.run as gateway_run
    from cron.scheduler_provider import InProcessCronScheduler

    calls = []

    class Runner:
        def __init__(self, config):
            self.config = config
            self.adapters = {}
            self._running = True
            self.should_exit_cleanly = False
            self.should_exit_with_failure = False
            self.exit_reason = None
            self.exit_code = None
            self._restart_requested = False
            self._restart_via_service = False

        async def start(self):
            calls.append("adapter_start")
            return True

        async def wait_for_shutdown(self):
            return None

    _patch_gateway_boot_dependencies(monkeypatch, tmp_path, Runner)
    monkeypatch.setattr("hermes_cli.profiles.profiles_to_serve", lambda **_kwargs: [("atlas", "/atlas"), ("yuange", "/yuange")])
    monkeypatch.setattr(gateway_run, "_recover_protected_final_results_at_gateway_startup", lambda homes: calls.append(("recovery", homes)) or [])
    monkeypatch.setattr("cron.scheduler_provider.resolve_cron_scheduler", lambda: InProcessCronScheduler())
    monkeypatch.setattr(InProcessCronScheduler, "start", lambda _self, _stop, **kwargs: calls.append(("scheduler", kwargs.get("profile_homes"))))

    assert await gateway_run.start_gateway(config=GatewayConfig(multiplex_profiles=True), replace=False, verbosity=0) is True
    assert ("recovery", [("atlas", "/atlas"), ("yuange", "/yuange")]) in calls
    assert ("scheduler", [("atlas", "/atlas"), ("yuange", "/yuange")]) in calls
