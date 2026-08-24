"""Bot update markers reflect the command boundary, not an optimistic phase."""

from types import SimpleNamespace

import pytest


def _prepare(monkeypatch, impl):
    import hermes_cli.config as cli_config
    import hermes_cli.main as cli_main
    import hermes_cli.update_lock as update_lock

    markers: list[int] = []

    class FakeLock:
        holder = None

        def acquire(self):
            return True

        def release(self):
            return None

    monkeypatch.setattr(cli_config, "is_managed", lambda: False)
    monkeypatch.setattr(cli_config, "detect_install_method", lambda root=None: "git")
    monkeypatch.setattr(update_lock, "UpdateLock", FakeLock)
    monkeypatch.setattr(cli_main, "_install_hangup_protection", lambda gateway_mode: {})
    monkeypatch.setattr(cli_main, "_finalize_update_output", lambda state: None)
    monkeypatch.setattr(cli_main, "_cmd_update_impl", impl)
    monkeypatch.setattr(
        cli_main, "_write_gateway_update_status", markers.append, raising=False
    )
    monkeypatch.setattr(
        cli_main, "UPDATE_EXIT_INDEPENDENT_HANDOFF", 75, raising=False
    )
    return cli_main, markers


def _args():
    return SimpleNamespace(
        rollback=None,
        plan=False,
        check=False,
        gateway=True,
    )


def test_gateway_success_marker_is_written_at_command_boundary(monkeypatch):
    cli_main, markers = _prepare(monkeypatch, lambda args, gateway_mode: None)

    cli_main.cmd_update(_args())

    assert markers == [0]


def test_gateway_failure_overwrites_any_earlier_optimistic_marker(monkeypatch):
    cli_main = None

    def fail(args, gateway_mode):
        assert cli_main is not None
        cli_main._write_gateway_update_status(0)
        raise SystemExit(1)

    cli_main, markers = _prepare(monkeypatch, fail)

    with pytest.raises(SystemExit) as raised:
        cli_main.cmd_update(_args())

    assert raised.value.code == 1
    assert markers == [0, 1]


def test_independent_handoff_exit_is_nonterminal(monkeypatch):
    def handoff(args, gateway_mode):
        raise SystemExit(75)

    cli_main, markers = _prepare(monkeypatch, handoff)

    with pytest.raises(SystemExit) as raised:
        cli_main.cmd_update(_args())

    assert raised.value.code == 75
    assert markers == []


def test_gateway_normal_return_preserves_inner_partial_marker(
    monkeypatch, tmp_path
):
    import hermes_cli.config as cli_config
    import hermes_cli.main as cli_main
    import hermes_cli.update_cmd as update_cmd
    import hermes_cli.update_lock as update_lock

    class FakeLock:
        holder = None

        def acquire(self):
            return True

        def release(self):
            return None

    monkeypatch.setattr(cli_config, "is_managed", lambda: False)
    monkeypatch.setattr(cli_config, "detect_install_method", lambda root=None: "git")
    monkeypatch.setattr(update_lock, "UpdateLock", FakeLock)
    monkeypatch.setattr(cli_main, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(cli_main, "_install_hangup_protection", lambda gateway_mode: {})
    monkeypatch.setattr(cli_main, "_finalize_update_output", lambda state: None)
    correlation_id = "12345678-1234-5678-9234-567812345678"
    outcome = tmp_path / f".update_exit_code.{correlation_id}"
    def partial(args, gateway_mode):
        # The outer command began as an ordinary parent; model only the state
        # visible after its copied coordinator has entered the implementation.
        monkeypatch.setenv(
            "HERMES_UPDATE_COORDINATOR_SNAPSHOT", str(tmp_path / "copy")
        )
        monkeypatch.setenv("HERMES_UPDATE_CORRELATION_ID", correlation_id)
        monkeypatch.setenv("HERMES_UPDATE_TAURI_OUTCOME_PATH", str(outcome))
        cli_main._write_gateway_update_status(1)

    monkeypatch.setattr(cli_main, "_cmd_update_impl", partial)

    cli_main.cmd_update(_args())

    assert not (tmp_path / ".update_exit_code").exists()
    assert outcome.read_text(encoding="utf-8") == "1"
