"""Structural restart-loop defenses that do not classify command prose."""

from argparse import Namespace

import pytest


class TestGatewaySelfTargetingGuard:
    """The explicit gateway lifecycle command refuses to target its host."""

    def test_stop_refuses_inside_gateway(self, monkeypatch):
        monkeypatch.setenv("_HERMES_GATEWAY", "1")
        from hermes_cli.gateway import gateway_command

        args = Namespace(gateway_command="stop", all=False, system=False)
        with pytest.raises(SystemExit) as exc_info:
            gateway_command(args)
        assert exc_info.value.code == 1

    def test_stop_allows_outside_gateway(self, monkeypatch):
        monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
        import hermes_cli.gateway as gateway

        class ReachedServiceManager(Exception):
            pass

        def reached(*_args, **_kwargs):
            raise ReachedServiceManager

        monkeypatch.setattr(
            gateway,
            "_dispatch_via_service_manager_if_s6",
            reached,
        )
        monkeypatch.setattr(
            gateway,
            "_dispatch_all_via_service_manager_if_s6",
            reached,
        )
        args = Namespace(gateway_command="stop", all=False, system=False)
        with pytest.raises(ReachedServiceManager):
            gateway.gateway_command(args)


class TestRestartLoopGuard:
    """Boot-count/window state breaks repeated restart-interrupted resumes."""

    @pytest.fixture(autouse=True)
    def _isolate_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        (tmp_path / ".hermes").mkdir(parents=True)
        import gateway.restart_loop_guard as restart_guard

        restart_guard.clear()

    def test_is_tripped_reads_without_recording(self):
        import gateway.restart_loop_guard as restart_guard

        restart_guard.record_restart_interrupted_boot(60, now=1000.0)
        restart_guard.record_restart_interrupted_boot(60, now=1001.0)
        assert restart_guard.is_restart_loop_tripped(3, 60, now=1002.0) is False
        restart_guard.record_restart_interrupted_boot(60, now=1002.0)
        assert restart_guard.is_restart_loop_tripped(3, 60, now=1003.0) is True

    def test_clear_resets(self):
        import gateway.restart_loop_guard as restart_guard

        restart_guard.check_and_record(3, 60, now=1000.0)
        restart_guard.check_and_record(3, 60, now=1001.0)
        restart_guard.clear()
        assert restart_guard.check_and_record(3, 60, now=1002.0) is False
