from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import hermes_cli.gateway as gateway_cli
from hermes_cli.gateway import _runtime_health_lines


def _iso_age(seconds_ago: float) -> str:
    """ISO-8601 UTC timestamp ``seconds_ago`` in the past (drives _marker_is_stale)."""
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


_STALE_LINE_PREFIX = "⚠ Stale gateway_state.json:"


def _stale_lines(lines):
    return [ln for ln in lines if ln.startswith(_STALE_LINE_PREFIX)]


def test_runtime_health_lines_flags_stale_running_with_dead_pid(monkeypatch):
    """Stale updated_at + dead PID + 'running' -> contradiction line, no draining line."""
    from gateway import status as status_mod

    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "running",
            "pid": 4242,
            "start_time": 111,
            "updated_at": _iso_age(600),  # well past the 120s TTL -> stale
            "active_agents": 0,
        },
    )
    # Recorded PID is gone (ungraceful kill); no real process is touched.
    monkeypatch.setattr(status_mod, "_pid_exists", lambda pid: False)
    monkeypatch.setattr(status_mod, "_get_process_start_time", lambda pid: None)

    lines = _runtime_health_lines()

    stale = _stale_lines(lines)
    assert len(stale) == 1, lines
    assert "recorded state 'running'" in stale[0]
    assert "recorded process is gone" in stale[0]
    # The misleading live-state summary must be suppressed.
    assert not any("draining" in ln.lower() for ln in lines), lines


def test_runtime_health_lines_include_fatal_platform_and_startup_reason(monkeypatch):
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "startup_failed",
            "exit_reason": "telegram conflict",
            "platforms": {
                "telegram": {
                    "state": "fatal",
                    "error_message": "another poller is active",
                }
            },
        },
    )

    lines = _runtime_health_lines()

    assert "⚠ telegram: another poller is active" in lines
    assert "⚠ Last startup issue: telegram conflict" in lines


def test_launchd_status_renders_running_code_from_runtime_state(
    tmp_path, monkeypatch, capsys
):
    """The launchd-backed ``gateway status`` path must render runtime code details."""
    plist_path = tmp_path / "ai.hermes.gateway.plist"
    plist_path.write_text("<plist/>", encoding="utf-8")
    monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(gateway_cli, "launchd_plist_is_current", lambda: True)
    monkeypatch.setattr(
        gateway_cli.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        "gateway.status.get_running_pid", lambda cleanup_stale=False: None
    )
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "running",
            "git_commit": "abc1234",
            "kanban_dispatch_in_gateway": True,
        },
    )

    gateway_cli.launchd_status()

    assert (
        "Running code: abc1234 (kanban dispatch-in-gateway: enabled)"
        in capsys.readouterr().out
    )


def test_runtime_status_running_pid_validates_live_gateway_record(monkeypatch):
    from gateway import status as status_mod

    runtime = {
        "pid": 12345,
        "kind": "hermes-gateway",
        "argv": ["/opt/hermes/hermes_cli/main.py", "gateway", "run", "--replace"],
        "start_time": None,
        "gateway_state": "running",
    }
    monkeypatch.setattr(status_mod, "_pid_exists", lambda pid: pid == 12345)
    monkeypatch.setattr(status_mod, "_get_process_start_time", lambda pid: None)
    monkeypatch.setattr(status_mod, "_looks_like_gateway_process", lambda pid: False)

    assert status_mod.get_runtime_status_running_pid(runtime) == 12345

