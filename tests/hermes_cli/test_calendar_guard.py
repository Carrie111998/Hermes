import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import calendar_guard
from hermes_cli.gateway_identity import (
    GatewayIdentityError,
    active_gateway_identity,
    identity_from_project,
    parse_systemd_properties,
)
from hermes_cli.release_markers import stamp_release_marker
from scripts.release_snapshot import build_snapshot
from scripts.install_calendar_guard import install_user_units


def _runner(stdout: str):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    return run


def _props(path: Path, pid: str = "123") -> str:
    return (
        "ActiveState=active\n"
        "SubState=running\n"
        f"MainPID={pid}\n"
        f"WorkingDirectory={path}\n"
    )


def test_release_marker_precedence_and_legacy_names(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    sha = "a" * 40
    (release / "RELEASE_SHA").write_text(sha + "\n")
    assert identity_from_project(release).fingerprint == "release:" + sha
    (release / ".hermes-release-sha").write_text(sha + "\n")
    assert identity_from_project(release).marker_name == ".hermes-release-sha"


def test_conflicting_markers_fail_closed(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    (release / ".hermes-release-sha").write_text("a" * 40 + "\n")
    (release / "RELEASE_SHA").write_text("b" * 40 + "\n")
    with pytest.raises(GatewayIdentityError, match="conflicting"):
        identity_from_project(release)


def test_unknown_release_marker_fails_closed(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    (release / "RELEASE_COMMIT").write_text("a" * 40 + "\n")
    (release / "RELEASE_SHA256").write_text("hash\n")
    with pytest.raises(GatewayIdentityError, match="unrecognized"):
        identity_from_project(release)


def test_markerless_release_tree_does_not_fall_back_to_git(tmp_path):
    home = tmp_path / ".hermes"
    release = home / "releases" / "v1"
    (release / ".git").mkdir(parents=True)
    with pytest.raises(GatewayIdentityError, match="release marker missing"):
        active_gateway_identity(
            home,
            project_root=tmp_path / "checkout",
            runner=_runner(_props(release)),
        )


def test_unrelated_release_notes_do_not_trigger_marker_failure(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    (release / ".hermes-release-sha").write_text("a" * 40 + "\n")
    (release / "RELEASE_NOTES.md").write_text("notes\n")

    assert identity_from_project(release).fingerprint == "release:" + "a" * 40


def test_pruned_release_directory_fails_closed_with_guard_warning(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    release = home / "releases" / "pruned"
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = calendar_guard.check_once(
        home=home,
        project_root=tmp_path / "checkout",
        now=100,
        runner=_runner(_props(release)),
    )

    assert "release directory unavailable" in result
    assert "BLOCKED" in result
    assert not (home / "gateway" / "calendar_guard_request.json").exists()


def test_service_down_is_blocked_without_recovery_request(tmp_path):
    home = tmp_path / ".hermes"
    release = home / "releases" / "v1"
    release.mkdir(parents=True)
    (release / ".hermes-release-sha").write_text("a" * 40 + "\n")
    (release / ".hermes-release-sha").write_text("a" * 40 + "\n")
    stopped = "ActiveState=inactive\nSubState=dead\nMainPID=0\nWorkingDirectory=" + str(release) + "\n"

    result = calendar_guard.check_once(
        home=home,
        project_root=tmp_path / "checkout",
        now=100,
        runner=_runner(stopped),
    )

    assert "BLOCKED" in result
    assert not (home / "gateway" / "calendar_guard_request.json").exists()


def test_missing_boot_record_is_blocked_without_recovery_request(tmp_path):
    home = tmp_path / ".hermes"
    release = home / "releases" / "v1"
    release.mkdir(parents=True)
    (release / ".hermes-release-sha").write_text("a" * 40 + "\n")

    result = calendar_guard.check_once(
        home=home,
        project_root=tmp_path / "checkout",
        now=100,
        runner=_runner(_props(release)),
    )

    assert "BLOCKED" in result
    assert "boot fingerprint" in result
    assert not (home / "gateway" / "calendar_guard_request.json").exists()


def test_missing_boot_record_blocked_notification_is_deduplicated(tmp_path):
    home = tmp_path / ".hermes"
    release = home / "releases" / "v1"
    release.mkdir(parents=True)
    (release / ".hermes-release-sha").write_text("a" * 40 + "\n")
    runner = _runner(_props(release))

    first = calendar_guard.check_once(
        home=home, project_root=tmp_path / "checkout", now=100, runner=runner
    )
    second = calendar_guard.check_once(
        home=home, project_root=tmp_path / "checkout", now=3700, runner=runner
    )

    assert "BLOCKED" in first
    assert second == ""


def test_systemd_properties_validate_required_fields_and_pid():
    parsed = parse_systemd_properties(
        "ActiveState=active\nSubState=running\nMainPID=9\nWorkingDirectory=/x\n"
    )
    assert parsed["MainPID"] == "9"
    with pytest.raises(GatewayIdentityError, match="MainPID"):
        parse_systemd_properties(
            "ActiveState=active\nSubState=running\nMainPID=nope\nWorkingDirectory=/x\n"
        )


def test_active_identity_uses_user_scope_and_release_marker(tmp_path):
    home = tmp_path / ".hermes"
    release = home / "releases" / "v1"
    release.mkdir(parents=True)
    sha = "a" * 40
    (release / ".hermes-release-sha").write_text(sha + "\n")
    identity = active_gateway_identity(
        home,
        project_root=tmp_path / "checkout",
        runner=_runner(_props(release)),
    )
    assert identity.fingerprint == "release:" + sha
    assert identity.service_properties["MainPID"] == "123"


def test_atomic_release_marker_writer(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    marker = stamp_release_marker(release, "A" * 40)
    assert marker.name == ".hermes-release-sha"
    assert marker.read_text() == ("a" * 40) + "\n"
    with pytest.raises(ValueError):
        stamp_release_marker(release, "not a sha")


def test_release_snapshot_builder_stamps_canonical_marker(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "sentinel.txt").write_text("ok")
    destination = tmp_path / "release"
    build_snapshot(source, destination, "a" * 40)
    assert (destination / "sentinel.txt").read_text() == "ok"
    assert (destination / ".hermes-release-sha").read_text() == ("a" * 40) + "\n"
    with pytest.raises(FileExistsError):
        build_snapshot(source, destination, "a" * 40)


def test_installer_renders_user_units_without_gateway_env(tmp_path):
    calls = []

    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    home = tmp_path / ".hermes"
    release = home / "releases" / "v1"
    release.mkdir(parents=True)
    (release / ".hermes-release-sha").write_text("a" * 40 + "\n")
    paths = install_user_units(
        home,
        release,
        Path("/opt/hermes/bin/python"),
        unit_dir=tmp_path / "systemd-user",
        runner=runner,
    )
    assert {path.name for path in paths} == {
        "hermes_calendar_guard.sh",
        "hermes-gateway-recovery.service",
        "hermes-gateway-recovery.timer",
    }
    service = (tmp_path / "systemd-user" / "hermes-gateway-recovery.service").read_text()
    assert "UnsetEnvironment=_HERMES_GATEWAY HERMES_GATEWAY_SESSION" in service
    assert "TimeoutStartSec=300" in service
    wrapper = (home / "scripts" / "hermes_calendar_guard.sh").read_text()
    assert str(release) in wrapper
    assert "@RELEASE_PATH@" not in wrapper
    assert "hermes_cli.calendar_guard --recover" in service
    assert calls[0][0] == ["systemctl", "--user", "daemon-reload"]


def test_check_queues_once_and_suppresses_duplicate(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    release = home / "releases" / "v1"
    release.mkdir(parents=True)
    (release / ".hermes-release-sha").write_text("a" * 40 + "\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        calendar_guard,
        "read_boot_record",
        lambda home=None: {"schema": 1, "fingerprint": "release:" + "b" * 40, "release_path": str(release)},
    )
    runner = _runner(_props(release))
    first = calendar_guard.check_once(
        home=home, project_root=tmp_path / "checkout", now=100, runner=runner
    )
    second = calendar_guard.check_once(
        home=home, project_root=tmp_path / "checkout", now=101, runner=runner
    )
    assert "recovery queued" in first
    assert second == ""
    request = json.loads((home / "gateway" / "calendar_guard_request.json").read_text())
    assert request["service"] == "hermes-gateway.service"


def test_exhausted_incident_stays_silent_across_hourly_checks(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    release = home / "releases" / "v1"
    release.mkdir(parents=True)
    (release / ".hermes-release-sha").write_text("a" * 40 + "\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        calendar_guard,
        "read_boot_record",
        lambda home=None: {"schema": 1, "fingerprint": "release:" + "b" * 40, "release_path": str(release)},
    )
    runner = _runner(_props(release))
    first = calendar_guard.check_once(
        home=home, project_root=tmp_path / "checkout", now=100, runner=runner
    )
    assert "recovery queued" in first
    state_path = home / "gateway" / "calendar_guard_state.json"
    state = json.loads(state_path.read_text())
    state.update({"attempts": calendar_guard.MAX_ATTEMPTS, "recovery_exhausted": True, "blocked_reported": False})
    state_path.write_text(json.dumps(state))

    assert "recovery exhausted" in calendar_guard.check_once(
        home=home, project_root=tmp_path / "checkout", now=3700, runner=runner
    )
    assert calendar_guard.check_once(
        home=home, project_root=tmp_path / "checkout", now=7300, runner=runner
    ) == ""


def test_check_is_silent_when_boot_identity_and_path_match(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    release = home / "releases" / "v1"
    release.mkdir(parents=True)
    (release / ".hermes-release-sha").write_text("a" * 40 + "\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        calendar_guard,
        "read_boot_record",
        lambda home=None: {"schema": 1, "fingerprint": "release:" + "a" * 40, "release_path": str(release)},
    )
    assert calendar_guard.check_once(
        home=home, project_root=tmp_path / "checkout", now=100, runner=_runner(_props(release))
    ) == ""


def test_legacy_boot_record_is_not_treated_as_release_skew(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    release = home / "releases" / "v1"
    release.mkdir(parents=True)
    (release / ".hermes-release-sha").write_text("a" * 40 + "\n")
    monkeypatch.setattr(
        calendar_guard,
        "read_boot_record",
        lambda home=None: {"schema": 0, "fingerprint": "git:legacy", "release_path": None},
    )

    assert calendar_guard.check_once(
        home=home, project_root=tmp_path / "checkout", now=100, runner=_runner(_props(release))
    ) == ""
    assert not (home / "gateway" / "calendar_guard_request.json").exists()


def test_recovery_removes_gateway_marker_from_restart_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(calendar_guard.subprocess, "run", fake_run)
    calendar_guard._restart_user_service("hermes-gateway.service")
    assert captured["args"] == ["systemctl", "--user", "restart", "hermes-gateway.service"]
    assert "_HERMES_GATEWAY" not in captured["env"]


def test_recovery_requires_new_pid_and_matching_boot_record(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    release = home / "releases" / "v1"
    release.mkdir(parents=True)
    (release / ".hermes-release-sha").write_text("a" * 40 + "\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "gateway").mkdir()
    (home / "gateway" / "calendar_guard_request.json").write_text(
        '{"schema": 1, "incident_key": "abc", "service": "hermes-gateway.service"}'
    )
    identities = iter(
        [
            active_gateway_identity(home, project_root=tmp_path / "checkout", runner=_runner(_props(release, "1"))),
            active_gateway_identity(home, project_root=tmp_path / "checkout", runner=_runner(_props(release, "2"))),
        ]
    )
    monkeypatch.setattr(calendar_guard, "active_gateway_identity", lambda *args, **kwargs: next(identities))
    monkeypatch.setattr(
        calendar_guard,
        "read_boot_record",
        lambda home=None: {"schema": 1, "fingerprint": "release:" + "a" * 40, "release_path": str(release)},
    )
    result = calendar_guard.recover_once(
        home=home,
        project_root=tmp_path / "checkout",
        now=100,
        restart=lambda service: None,
        sleep=lambda seconds: None,
    )
    assert "recovery verified" in result
    assert not (home / "gateway" / "calendar_guard_request.json").exists()


def test_recovery_claims_attempt_before_restart_failure(tmp_path):
    home = tmp_path / ".hermes"
    release = home / "releases" / "v1"
    release.mkdir(parents=True)
    (release / ".hermes-release-sha").write_text("a" * 40 + "\n")
    (home / "gateway").mkdir()
    (home / "gateway" / "calendar_guard_request.json").write_text(
        '{"schema": 1, "incident_key": "abc", "service": "hermes-gateway.service"}'
    )

    result = calendar_guard.recover_once(
        home=home,
        project_root=tmp_path / "checkout",
        now=100,
        runner=_runner(_props(release, "1")),
        restart=lambda service: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, ["systemctl"])
        ),
        sleep=lambda seconds: None,
    )

    state = json.loads((home / "gateway" / "calendar_guard_state.json").read_text())
    assert "recovery BLOCKED" in result
    assert state["attempts"] == 1
    assert state["next_attempt_at"] > 100


def test_recovery_exhaustion_removes_request(tmp_path):
    home = tmp_path / ".hermes"
    release = home / "releases" / "v1"
    release.mkdir(parents=True)
    (release / ".hermes-release-sha").write_text("a" * 40 + "\n")
    (home / "gateway").mkdir()
    request = home / "gateway" / "calendar_guard_request.json"
    request.write_text(
        '{"schema": 1, "incident_key": "abc", "service": "hermes-gateway.service"}'
    )
    fail = lambda service: (_ for _ in ()).throw(
        subprocess.CalledProcessError(1, ["systemctl"])
    )

    for now in (100, 400, 1000):
        calendar_guard.recover_once(
            home=home,
            project_root=tmp_path / "checkout",
            now=now,
            runner=_runner(_props(release, "1")),
            restart=fail,
            sleep=lambda seconds: None,
        )

    state = json.loads((home / "gateway" / "calendar_guard_state.json").read_text())
    assert state["recovery_exhausted"] is True
    assert not request.exists()


def test_stale_running_recovery_claim_is_reclaimed(tmp_path):
    home = tmp_path / ".hermes"
    release = home / "releases" / "v1"
    release.mkdir(parents=True)
    (release / ".hermes-release-sha").write_text("a" * 40 + "\n")
    (home / "gateway").mkdir()
    (home / "gateway" / "calendar_guard_request.json").write_text(
        '{"schema": 1, "incident_key": "abc", "service": "hermes-gateway.service"}'
    )
    state_path = home / "gateway" / "calendar_guard_state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema": 1,
                "attempts": 1,
                "last_outcome": "RUNNING",
                "claimed_at": 100,
                "next_attempt_at": 400,
            }
        )
    )

    result = calendar_guard.recover_once(
        home=home,
        project_root=tmp_path / "checkout",
        now=100 + calendar_guard.RECOVERY_TIMEOUT_SECONDS + 1,
        runner=_runner(_props(release, "1")),
        restart=lambda service: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, ["systemctl"])
        ),
        sleep=lambda seconds: None,
    )
    state = json.loads(state_path.read_text())
    assert "recovery BLOCKED" in result
    assert state["attempts"] == 2
