"""Regression for #68523 — one systemctl timeout must not abort fleet restarts.

On hosts with many profile-backed ``hermes-gateway*.service`` units,
``hermes update`` used to wrap the entire per-scope unit loop in a single
``except subprocess.TimeoutExpired``. A timeout on unit N skipped units
N+1…, leaving later gateways on pre-update in-memory modules while the
checkout on disk was already new (mixed-generation crashes).
"""

from __future__ import annotations

import subprocess
from unittest.mock import Mock

import pytest

from hermes_cli.main import (
    _batch_systemd_gateway_restarts,
    _force_systemd_unit_restart,
    _for_each_systemd_gateway_unit,
    _resolve_systemd_manage_cmd,
    _service_unit_supports_graceful_sigusr1_restart,
    _warn_incomplete_gateway_fleet_restart,
)


def _list_units_stdout(names: list[str]) -> str:
    return "\n".join(f"{name}.service loaded active running" for name in names)


def test_systemd_fleet_signals_all_units_before_waiting():
    entries = [
        {
            "scope": "user",
            "svc_name": "hermes-gateway-a",
            "manage_cmd": ["systemctl", "--user"],
        },
        {
            "scope": "user",
            "svc_name": "hermes-gateway-b",
            "manage_cmd": ["systemctl", "--user"],
        },
    ]
    events: list[tuple[str, str]] = []

    _batch_systemd_gateway_restarts(
        entries,
        signal_unit=lambda entry: events.append(("signal", entry["svc_name"])),
        finish_unit=lambda entry: events.append(("wait", entry["svc_name"])),
    )

    assert events == [
        ("signal", "hermes-gateway-a"),
        ("signal", "hermes-gateway-b"),
        ("wait", "hermes-gateway-a"),
        ("wait", "hermes-gateway-b"),
    ]


def test_system_service_without_root_is_not_signalled_before_manual_fallback():
    entry = {
        "scope": "system",
        "svc_name": "hermes-gateway-work",
        "manage_cmd": None,
    }
    events: list[str] = []

    _batch_systemd_gateway_restarts(
        [entry],
        signal_unit=lambda _: events.append("signal"),
        finish_unit=lambda _: events.append("manual-fallback"),
    )

    assert events == ["manual-fallback"]


def test_system_unit_without_root_uses_manual_fallback(monkeypatch, capsys):
    run = Mock()
    monkeypatch.setattr("hermes_cli.update_cmd.subprocess.run", run)
    failed: list[str] = []

    _force_systemd_unit_restart(
        {
            "scope": "system",
            "scope_cmd": ["systemctl"],
            "svc_name": "hermes-gateway-work",
            "manage_cmd": None,
        },
        restarted_services=[],
        failed_units=failed,
    )

    run.assert_not_called()
    assert failed == ["hermes-gateway-work"]
    assert "sudo systemctl restart hermes-gateway-work" in capsys.readouterr().out


def test_production_manage_command_contract_user_root_sudo_and_manual(monkeypatch):
    user = _resolve_systemd_manage_cmd(
        "user", ["systemctl", "--user"], "hermes-gateway-work", cache={}
    )
    assert user == ["systemctl", "--user", "--no-ask-password"]

    monkeypatch.setattr("hermes_cli.update_cmd.os.geteuid", lambda: 0)
    root = _resolve_systemd_manage_cmd(
        "system", ["systemctl"], "hermes-gateway-work", cache={}
    )
    assert root == ["systemctl", "--no-ask-password"]

    monkeypatch.setattr("hermes_cli.update_cmd.os.geteuid", lambda: 1000)
    monkeypatch.setattr(
        "hermes_cli.update_cmd.subprocess.run",
        lambda *args, **kwargs: Mock(returncode=0),
    )
    sudo = _resolve_systemd_manage_cmd(
        "system", ["systemctl"], "hermes-gateway-work", cache={}
    )
    assert sudo == ["sudo", "-n", "systemctl", "--no-ask-password"]

    monkeypatch.setattr(
        "hermes_cli.update_cmd.subprocess.run",
        lambda *args, **kwargs: Mock(returncode=1),
    )
    manual = _resolve_systemd_manage_cmd(
        "system", ["systemctl"], "hermes-gateway-work", cache={}
    )
    assert manual is None


def test_production_batch_dedupes_and_isolates_finish_timeout_for_later_units():
    entries = [
        {"scope": "user", "svc_name": "hermes-gateway-a", "manage_cmd": ["systemctl", "--user"]},
        {"scope": "user", "svc_name": "hermes-gateway-a", "manage_cmd": ["systemctl", "--user"]},
        {"scope": "user", "svc_name": "hermes-gateway-b", "manage_cmd": ["systemctl", "--user"]},
        {"scope": "user", "svc_name": "hermes-gateway-c", "manage_cmd": ["systemctl", "--user"]},
    ]
    events: list[str] = []
    failed: list[str] = []

    def finish(entry):
        events.append(f"wait:{entry['svc_name']}")
        if entry["svc_name"] == "hermes-gateway-b":
            raise subprocess.TimeoutExpired(
                ["systemctl", "restart", entry["svc_name"]], 15
            )

    _batch_systemd_gateway_restarts(
        entries,
        signal_unit=lambda entry: events.append(f"signal:{entry['svc_name']}"),
        finish_unit=finish,
        on_finish_timeout=lambda entry, exc: failed.append(entry["svc_name"]),
    )

    assert events == [
        "signal:hermes-gateway-a",
        "signal:hermes-gateway-b",
        "signal:hermes-gateway-c",
        "wait:hermes-gateway-a",
        "wait:hermes-gateway-b",
        "wait:hermes-gateway-c",
    ]
    assert failed == ["hermes-gateway-b"]


class TestFleetRestartTimeoutIsolation:
    def test_timeout_on_middle_unit_continues_remaining_units(self):
        units = [
            "hermes-gateway-xiaomo1",
            "hermes-gateway-xiaomo2",
            "hermes-gateway-xiaomo3",
            "hermes-gateway-xiaomo4",
            "hermes-gateway-xiaomo5",
            "hermes-gateway-xiaomo6",
            "hermes-gateway-xiaomo7",
            "hermes-gateway",
        ]
        restarted: list[str] = []
        failed: list[str] = []
        timeout_cmds: list = []

        def process_unit(svc_name: str) -> None:
            if svc_name == "hermes-gateway-xiaomo5":
                raise subprocess.TimeoutExpired(
                    cmd=["systemctl", "--user", "--no-ask-password", "restart", svc_name],
                    timeout=15,
                )
            restarted.append(svc_name)

        def on_unit_timeout(svc_name: str, exc: subprocess.TimeoutExpired) -> None:
            failed.append(svc_name)
            timeout_cmds.append(exc.cmd)

        _for_each_systemd_gateway_unit(
            _list_units_stdout(units),
            process_unit=process_unit,
            on_unit_timeout=on_unit_timeout,
        )

        assert failed == ["hermes-gateway-xiaomo5"]
        assert restarted == [
            "hermes-gateway-xiaomo1",
            "hermes-gateway-xiaomo2",
            "hermes-gateway-xiaomo3",
            "hermes-gateway-xiaomo4",
            "hermes-gateway-xiaomo6",
            "hermes-gateway-xiaomo7",
            "hermes-gateway",
        ]
        assert set(restarted) | set(failed) == set(units)
        assert timeout_cmds == [
            ["systemctl", "--user", "--no-ask-password", "restart", "hermes-gateway-xiaomo5"]
        ]

    def test_non_gateway_units_in_list_output_are_ignored(self):
        seen: list[str] = []

        _for_each_systemd_gateway_unit(
            "\n".join(
                [
                    "ssh.service loaded active running",
                    "hermes-gateway-coder.service loaded active running",
                    "not-a-service loaded active running",
                    "",
                ]
            ),
            process_unit=seen.append,
            on_unit_timeout=lambda *_: pytest.fail("unexpected timeout"),
        )

        assert seen == ["hermes-gateway-coder"]

    def test_duplicate_unit_rows_are_processed_once(self):
        seen: list[str] = []

        _for_each_systemd_gateway_unit(
            _list_units_stdout(
                ["hermes-gateway-work", "hermes-gateway-work"]
            ),
            process_unit=seen.append,
            on_unit_timeout=lambda *_: pytest.fail("unexpected timeout"),
        )

        assert seen == ["hermes-gateway-work"]

    def test_hermes_serve_units_are_included(self):
        # #83438 — hermes update restarted hermes-gateway* units but left
        # hermes-serve* (the Desktop app's backend) on stale pre-update code.
        seen: list[str] = []

        _for_each_systemd_gateway_unit(
            "\n".join(
                [
                    "ssh.service loaded active running",
                    "hermes-serve.service loaded active running",
                    "hermes-serve-work.service loaded active running",
                    "hermes-gateway.service loaded active running",
                    "",
                ]
            ),
            process_unit=seen.append,
            on_unit_timeout=lambda *_: pytest.fail("unexpected timeout"),
        )

        assert seen == ["hermes-serve", "hermes-serve-work", "hermes-gateway"]

    def test_hermes_server_near_prefix_is_rejected(self):
        # Review on #83595: a bare ``startswith("hermes-serve")`` gate also
        # accepts the unrelated ``hermes-server.service``. Only the exact
        # base unit or the hyphenated profile family should pass.
        seen: list[str] = []

        _for_each_systemd_gateway_unit(
            _list_units_stdout(["hermes-server"]),
            process_unit=seen.append,
            on_unit_timeout=lambda *_: pytest.fail("unexpected timeout"),
        )

        assert seen == []

    def test_hermes_gateway_near_prefix_is_rejected(self):
        # Same strict shape on the gateway side: profile units are
        # ``hermes-gateway-<profile>``, so a hypothetical
        # ``hermes-gatewayd.service`` must not enter the restart path.
        seen: list[str] = []

        _for_each_systemd_gateway_unit(
            _list_units_stdout(["hermes-gatewayd", "hermes-gateway-coder"]),
            process_unit=seen.append,
            on_unit_timeout=lambda *_: pytest.fail("unexpected timeout"),
        )

        assert seen == ["hermes-gateway-coder"]


class TestGracefulSigusr1Eligibility:
    def test_gateway_units_are_eligible(self):
        assert _service_unit_supports_graceful_sigusr1_restart("hermes-gateway")
        assert _service_unit_supports_graceful_sigusr1_restart(
            "hermes-gateway-work"
        )

    def test_serve_units_are_not_eligible(self):
        # hermes-serve doesn't run gateway/run.py, so it never installs the
        # SIGUSR1 handler — sending it the signal would just terminate the
        # process (the default action) instead of draining gracefully.
        assert not _service_unit_supports_graceful_sigusr1_restart("hermes-serve")
        assert not _service_unit_supports_graceful_sigusr1_restart(
            "hermes-serve-work"
        )

    def test_process_errors_other_than_timeout_still_propagate(self):
        def process_unit(_svc_name: str) -> None:
            raise RuntimeError("not a timeout")

        with pytest.raises(RuntimeError, match="not a timeout"):
            _for_each_systemd_gateway_unit(
                _list_units_stdout(["hermes-gateway"]),
                process_unit=process_unit,
                on_unit_timeout=lambda *_: pytest.fail("timeout handler must not run"),
            )


class TestIncompleteFleetRestartWarning:
    def test_warns_with_exact_unrestarted_units(self, capsys):
        _warn_incomplete_gateway_fleet_restart(
            ["hermes-gateway-xiaomo5", "hermes-gateway-xiaomo6", "hermes-gateway-xiaomo5"]
        )
        out = capsys.readouterr().out
        assert "Update incomplete" in out
        assert out.count("hermes-gateway-xiaomo5") == 1
        assert "hermes-gateway-xiaomo6" in out
        assert "pre-update code" in out

