from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from session_bridge.characterize import (
    CharacterizationGateError,
    _cli_version,
    _read_report_safely,
    describe_characterization_gate,
    load_codex_characterization_origins,
    resolve_characterization_gate,
    run_live_characterization,
    write_characterization_report,
)
from session_bridge.cli import ConfigurationFailure, ProductionBackend, main
from session_bridge.config import BridgeConfig


_LIVE_ONLY = pytest.mark.skipif(
    os.environ.get("HERMES_SESSION_BRIDGE_LIVE_TESTS") != "1",
    reason="set HERMES_SESSION_BRIDGE_LIVE_TESTS=1 to create disposable native sessions",
)


def _report(
    characterization_id: str,
    *,
    versions: dict[str, str] | None = None,
    created_at: str = "2026-07-14T12:00:00+00:00",
    claude_passed: bool = True,
    codex_passed: bool = True,
    registration_turn: bool = False,
    codex_native_id: str | None = None,
) -> dict[str, object]:
    def provider(passed: bool, *, codex: bool) -> dict[str, object]:
        status: dict[str, object] = {
            "create": passed,
            "discover": passed,
            "read": passed,
            "resume": passed,
            "used_registration_turn": registration_turn if codex else False,
            "cleanup": "archived" if codex else "quarantined",
            "error_code": None if passed else "synthetic_characterization_failure",
        }
        if codex and codex_native_id is not None:
            status["native_id"] = codex_native_id
        return status

    return {
        "schema_version": 1,
        "characterization_id": characterization_id,
        "created_at": created_at,
        "automatic_mirroring_enabled": False,
        "versions": versions or {"claude": "1.2.3", "codex": "4.5.6"},
        "providers": {
            "claude": provider(claude_passed, codex=False),
            "codex": provider(codex_passed, codex=True),
        },
    }


def _write_report(
    root: Path,
    characterization_id: str,
    *,
    mtime_ns: int,
    **overrides: object,
) -> Path:
    report = _report(characterization_id, **overrides)
    path = write_characterization_report(
        report,
        report_root=root,
        characterization_id=characterization_id,
    )
    os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def test_characterization_gate_selects_newest_passing_current_report(
    tmp_path: Path,
) -> None:
    versions = {"claude": "1.2.3", "codex": "4.5.6"}
    _write_report(
        tmp_path,
        "11111111-1111-4111-8111-111111111111",
        mtime_ns=100,
        versions=versions,
    )
    newest = _write_report(
        tmp_path,
        "22222222-2222-4222-8222-222222222222",
        mtime_ns=200,
        versions=versions,
        registration_turn=True,
    )

    gate = resolve_characterization_gate(
        report_root=tmp_path,
        current_versions=versions,
    )

    assert gate.report_path == newest
    assert gate.characterization_id == newest.stem
    assert gate.codex_registration_turn_required is True


def test_characterization_defaults_follow_active_hermes_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hermes_home = tmp_path / "profile-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    characterization_id = "11111111-1111-4111-8111-111111111111"
    versions = {"claude": "1.2.3", "codex": "4.5.6"}

    report_path = write_characterization_report(
        _report(characterization_id, versions=versions),
        characterization_id=characterization_id,
    )
    gate = resolve_characterization_gate(current_versions=versions)

    expected_root = hermes_home / "session-bridge" / "characterization"
    assert report_path.parent == expected_root
    assert gate.report_path == report_path


def test_codex_characterization_origins_include_every_valid_report_native_id(
    tmp_path: Path,
) -> None:
    passing_id = "11111111-1111-4111-8111-111111111111"
    passing_native_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    failed_id = "22222222-2222-4222-8222-222222222222"
    failed_native_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    _write_report(
        tmp_path,
        passing_id,
        mtime_ns=100,
        codex_native_id=passing_native_id,
    )
    _write_report(
        tmp_path,
        failed_id,
        mtime_ns=200,
        codex_passed=False,
        codex_native_id=failed_native_id,
    )

    assert load_codex_characterization_origins(report_root=tmp_path) == {
        passing_native_id: f"characterization-{passing_id}-codex",
        failed_native_id: f"characterization-{failed_id}-codex",
    }


def test_codex_characterization_origins_allow_missing_report_root(
    tmp_path: Path,
) -> None:
    assert load_codex_characterization_origins(
        report_root=tmp_path / "not-created"
    ) == {}


def test_codex_characterization_origins_reject_malformed_report_root(
    tmp_path: Path,
) -> None:
    (tmp_path / "not-a-report.json").write_text("{}", encoding="utf-8")

    with pytest.raises(
        CharacterizationGateError,
        match="characterization_report_malformed",
    ):
        load_codex_characterization_origins(report_root=tmp_path)


def test_codex_characterization_origins_reject_native_identity_reuse(
    tmp_path: Path,
) -> None:
    native_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    _write_report(
        tmp_path,
        "11111111-1111-4111-8111-111111111111",
        mtime_ns=100,
        codex_native_id=native_id,
    )
    _write_report(
        tmp_path,
        "22222222-2222-4222-8222-222222222222",
        mtime_ns=200,
        codex_passed=False,
        codex_native_id=native_id,
    )

    with pytest.raises(
        CharacterizationGateError,
        match="characterization_native_identity_conflict",
    ):
        load_codex_characterization_origins(report_root=tmp_path)


def test_codex_characterization_origins_reject_redirected_guard_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard_root = tmp_path / ".codex-origin-guards"
    monkeypatch.setattr(
        "session_bridge.characterize._path_is_redirect",
        lambda path: Path(path) == guard_root,
    )

    with pytest.raises(
        CharacterizationGateError,
        match="characterization_codex_origin_guard_invalid",
    ):
        load_codex_characterization_origins(
            report_root=tmp_path,
            marker_secret=b"trusted-key",
        )


@pytest.mark.parametrize("gate_value", (None, "", "0", "true"))
def test_claude_visibility_live_characterization_uses_existing_gate(
    monkeypatch: pytest.MonkeyPatch,
    gate_value: str | None,
) -> None:
    if gate_value is None:
        monkeypatch.delenv("HERMES_SESSION_BRIDGE_LIVE_TESTS", raising=False)
    else:
        monkeypatch.setenv("HERMES_SESSION_BRIDGE_LIVE_TESTS", gate_value)
    monkeypatch.setattr(
        "session_bridge.cli.resolve_cli_executable",
        lambda _name: (_ for _ in ()).throw(
            AssertionError("blocked characterization must not launch Claude")
        ),
    )
    backend = ProductionBackend(BridgeConfig())

    with pytest.raises(ConfigurationFailure, match="live_characterization_not_enabled"):
        backend.characterize_claude_visibility()


def test_live_characterization_gate_survives_cli_config_composition_without_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"session_bridge": {}})
    calls: list[str] = []
    loaded: list[BridgeConfig] = []

    backend = SimpleNamespace(
        characterize_claude_visibility=lambda: (
            calls.append("characterize") or {"status": "composed"}
        ),
        close=lambda: None,
    )

    exit_code = main(
        ["characterize-claude-visibility", "--json"],
        config_loader=lambda: BridgeConfig.load(path=tmp_path / "missing.toml"),
        backend_factory=lambda config: loaded.append(config) or backend,
    )

    assert exit_code == 0
    assert calls == ["characterize"]
    assert loaded == [BridgeConfig()]
    assert json.loads(capsys.readouterr().out) == {"status": "composed"}


def test_characterization_gate_latest_failure_blocks_older_pass(tmp_path: Path) -> None:
    versions = {"claude": "1.2.3", "codex": "4.5.6"}
    _write_report(
        tmp_path,
        "11111111-1111-4111-8111-111111111111",
        mtime_ns=100,
        versions=versions,
    )
    _write_report(
        tmp_path,
        "22222222-2222-4222-8222-222222222222",
        mtime_ns=200,
        versions=versions,
        codex_passed=False,
    )

    with pytest.raises(
        CharacterizationGateError,
        match="characterization_report_failed",
    ) as exc_info:
        resolve_characterization_gate(
            report_root=tmp_path,
            current_versions=versions,
        )
    assert exc_info.value.code == "failed"


def test_characterization_gate_uses_created_at_not_touch_time(tmp_path: Path) -> None:
    versions = {"claude": "1.2.3", "codex": "4.5.6"}
    touched_older_pass = _write_report(
        tmp_path,
        "11111111-1111-4111-8111-111111111111",
        mtime_ns=300,
        versions=versions,
        created_at="2026-07-14T12:00:00+00:00",
    )
    _write_report(
        tmp_path,
        "22222222-2222-4222-8222-222222222222",
        mtime_ns=100,
        versions=versions,
        created_at="2026-07-14T13:00:00+00:00",
        codex_passed=False,
    )

    os.utime(touched_older_pass, ns=(400, 400))

    with pytest.raises(CharacterizationGateError) as exc_info:
        resolve_characterization_gate(
            report_root=tmp_path,
            current_versions=versions,
        )
    assert exc_info.value.code == "failed"


def test_characterization_gate_allows_newer_pass_after_valid_older_failure(
    tmp_path: Path,
) -> None:
    versions = {"claude": "1.2.3", "codex": "4.5.6"}
    _write_report(
        tmp_path,
        "11111111-1111-4111-8111-111111111111",
        mtime_ns=300,
        versions=versions,
        created_at="2026-07-14T12:00:00+00:00",
        codex_passed=False,
    )
    newest = _write_report(
        tmp_path,
        "22222222-2222-4222-8222-222222222222",
        mtime_ns=100,
        versions=versions,
        created_at="2026-07-14T13:00:00+00:00",
    )

    gate = resolve_characterization_gate(
        report_root=tmp_path,
        current_versions=versions,
    )

    assert gate.report_path == newest


def test_characterization_gate_latest_malformed_report_blocks_older_pass(
    tmp_path: Path,
) -> None:
    versions = {"claude": "1.2.3", "codex": "4.5.6"}
    _write_report(
        tmp_path,
        "11111111-1111-4111-8111-111111111111",
        mtime_ns=100,
        versions=versions,
    )
    malformed = tmp_path / "22222222-2222-4222-8222-222222222222.json"
    malformed.write_text("{not-json", encoding="utf-8")
    os.utime(malformed, ns=(200, 200))

    with pytest.raises(
        CharacterizationGateError,
        match="characterization_report_malformed",
    ) as exc_info:
        resolve_characterization_gate(
            report_root=tmp_path,
            current_versions=versions,
        )
    assert exc_info.value.code == "invalid"


def test_characterization_gate_any_malformed_candidate_blocks_newer_pass(
    tmp_path: Path,
) -> None:
    versions = {"claude": "1.2.3", "codex": "4.5.6"}
    malformed = tmp_path / "11111111-1111-4111-8111-111111111111.json"
    malformed.write_text("{not-json", encoding="utf-8")
    os.utime(malformed, ns=(100, 100))
    _write_report(
        tmp_path,
        "22222222-2222-4222-8222-222222222222",
        mtime_ns=300,
        versions=versions,
        created_at="2026-07-14T13:00:00+00:00",
    )

    with pytest.raises(CharacterizationGateError) as exc_info:
        resolve_characterization_gate(
            report_root=tmp_path,
            current_versions=versions,
        )
    assert exc_info.value.code == "invalid"


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    [
        (lambda report: report.update(schema_version=2), "malformed"),
        (
            lambda report: report["providers"]["claude"].update(read=False),
            "failed",
        ),
        (lambda report: report["providers"].pop("codex"), "malformed"),
        (lambda report: report.update(automatic_mirroring_enabled=True), "malformed"),
        (
            lambda report: report["providers"]["codex"].update(unexpected=True),
            "malformed",
        ),
    ],
)
def test_characterization_gate_requires_exact_schema_and_provider_pass(
    tmp_path: Path,
    mutation,
    error_code: str,
) -> None:
    versions = {"claude": "1.2.3", "codex": "4.5.6"}
    characterization_id = "11111111-1111-4111-8111-111111111111"
    report = _report(characterization_id, versions=versions)
    mutation(report)
    write_characterization_report(
        report,
        report_root=tmp_path,
        characterization_id=characterization_id,
    )

    with pytest.raises(
        CharacterizationGateError,
        match=f"characterization_report_{error_code}",
    ) as exc_info:
        resolve_characterization_gate(
            report_root=tmp_path,
            current_versions=versions,
        )
    assert exc_info.value.code == ("failed" if error_code == "failed" else "invalid")


def test_characterization_gate_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    versions = {"claude": "1.2.3", "codex": "4.5.6"}
    characterization_id = "11111111-1111-4111-8111-111111111111"
    payload = json.dumps(_report(characterization_id, versions=versions))
    payload = payload.replace(
        '"schema_version": 1,',
        '"schema_version": 1, "schema_version": 1,',
        1,
    )
    (tmp_path / f"{characterization_id}.json").write_text(payload, encoding="utf-8")

    with pytest.raises(CharacterizationGateError) as exc_info:
        resolve_characterization_gate(
            report_root=tmp_path,
            current_versions=versions,
        )
    assert exc_info.value.code == "invalid"


def test_characterization_gate_rejects_current_cli_version_drift(
    tmp_path: Path,
) -> None:
    _write_report(
        tmp_path,
        "11111111-1111-4111-8111-111111111111",
        mtime_ns=100,
    )

    with pytest.raises(
        CharacterizationGateError,
        match="characterization_version_mismatch",
    ) as exc_info:
        resolve_characterization_gate(
            report_root=tmp_path,
            current_versions={"claude": "1.2.4", "codex": "4.5.6"},
        )
    assert exc_info.value.code == "version_drift"


def test_gate_description_returns_zero_and_the_id_when_the_gate_passes(
    tmp_path: Path,
) -> None:
    versions = {"claude": "1.2.3", "codex": "4.5.6"}
    characterization_id = "11111111-1111-4111-8111-111111111111"
    _write_report(tmp_path, characterization_id, mtime_ns=100, versions=versions)

    code, message = describe_characterization_gate(
        report_root=tmp_path,
        current_versions=versions,
    )

    assert code == 0
    assert message == characterization_id


def test_gate_description_names_only_the_drifted_provider(tmp_path: Path) -> None:
    _write_report(
        tmp_path,
        "11111111-1111-4111-8111-111111111111",
        mtime_ns=100,
        versions={"claude": "2.1.216 (Claude Code)", "codex": "codex-cli 0.146.0"},
    )

    code, message = describe_characterization_gate(
        report_root=tmp_path,
        current_versions={
            "claude": "2.1.216 (Claude Code)",
            "codex": "codex-cli 0.147.0",
        },
    )

    assert code != 0
    assert "version_drift" in message
    # The drifted provider is named with both sides of the comparison, so the
    # operator can see what to refresh without re-running the gate by hand.
    codex_line = next(line for line in message.splitlines() if "codex" in line)
    assert "codex-cli 0.146.0" in codex_line
    assert "codex-cli 0.147.0" in codex_line
    # The unchanged provider must not be reported as drifted -- that misdirection
    # is exactly what made the 2026-08-19 recovery over-broad.
    claude_line = next(line for line in message.splitlines() if "claude" in line)
    assert "unchanged" in claude_line


def test_gate_description_names_both_providers_when_both_drift(
    tmp_path: Path,
) -> None:
    _write_report(
        tmp_path,
        "11111111-1111-4111-8111-111111111111",
        mtime_ns=100,
        versions={"claude": "2.1.216", "codex": "codex-cli 0.146.0"},
    )

    code, message = describe_characterization_gate(
        report_root=tmp_path,
        current_versions={"claude": "2.1.219", "codex": "codex-cli 0.147.0"},
    )

    assert code != 0
    claude_line = next(line for line in message.splitlines() if "claude" in line)
    assert "2.1.216" in claude_line and "2.1.219" in claude_line
    codex_line = next(line for line in message.splitlines() if "codex" in line)
    assert "codex-cli 0.146.0" in codex_line and "codex-cli 0.147.0" in codex_line


def test_gate_description_reports_non_drift_failures_by_code(tmp_path: Path) -> None:
    versions = {"claude": "1.2.3", "codex": "4.5.6"}
    _write_report(
        tmp_path,
        "11111111-1111-4111-8111-111111111111",
        mtime_ns=100,
        versions=versions,
        codex_passed=False,
    )

    code, message = describe_characterization_gate(
        report_root=tmp_path,
        current_versions=versions,
    )

    assert code != 0
    assert "failed" in message
    assert "characterization_report_failed" in message


def test_gate_description_reports_a_missing_report_root(tmp_path: Path) -> None:
    code, message = describe_characterization_gate(
        report_root=tmp_path / "absent",
        current_versions={"claude": "1.2.3", "codex": "4.5.6"},
    )

    assert code != 0
    assert "missing" in message


def test_gate_description_survives_unreadable_expected_versions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A diagnostic must never mask the rejection it is describing."""

    _write_report(
        tmp_path,
        "11111111-1111-4111-8111-111111111111",
        mtime_ns=100,
        versions={"claude": "1.2.3", "codex": "4.5.6"},
    )

    def explode(root: Path) -> None:
        raise RuntimeError("synthetic diagnostic failure")

    monkeypatch.setattr(
        "session_bridge.characterize._expected_gate_versions",
        explode,
    )

    code, message = describe_characterization_gate(
        report_root=tmp_path,
        current_versions={"claude": "1.2.3", "codex": "9.9.9"},
    )

    assert code != 0
    assert "version_drift" in message


def test_characterization_gate_rejects_redirected_newest_report(tmp_path: Path) -> None:
    versions = {"claude": "1.2.3", "codex": "4.5.6"}
    target_root = tmp_path / "target"
    target_root.mkdir()
    target = _write_report(
        target_root,
        "22222222-2222-4222-8222-222222222222",
        mtime_ns=200,
        versions=versions,
    )
    redirect = tmp_path / target.name
    try:
        redirect.symlink_to(target)
    except OSError:
        pytest.skip("file symlinks are unavailable on this platform")
    os.utime(redirect, ns=(300, 300), follow_symlinks=False)

    with pytest.raises(
        CharacterizationGateError,
        match="characterization_report_unsafe",
    ) as exc_info:
        resolve_characterization_gate(
            report_root=tmp_path,
            current_versions=versions,
        )
    assert exc_info.value.code == "invalid"


def test_characterization_report_read_rejects_final_path_identity_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    characterization_id = "11111111-1111-4111-8111-111111111111"
    path = tmp_path / f"{characterization_id}.json"
    path.write_text(json.dumps(_report(characterization_id)), encoding="utf-8")
    original_lstat = os.lstat
    file_lstat_calls = 0

    def changed_final_lstat(candidate: object):
        nonlocal file_lstat_calls
        info = original_lstat(candidate)
        if Path(candidate) != path:
            return info
        file_lstat_calls += 1
        if file_lstat_calls < 3:
            return info
        return SimpleNamespace(
            st_mode=info.st_mode,
            st_dev=info.st_dev,
            st_ino=info.st_ino + 1,
            st_size=info.st_size,
            st_mtime_ns=info.st_mtime_ns,
            st_file_attributes=getattr(info, "st_file_attributes", 0),
        )

    monkeypatch.setattr(os, "lstat", changed_final_lstat)

    with pytest.raises(
        CharacterizationGateError,
        match="characterization_report_unsafe",
    ) as exc_info:
        _read_report_safely(path)

    assert exc_info.value.code == "invalid"


def test_characterization_gate_reports_missing_with_stable_code(tmp_path: Path) -> None:
    with pytest.raises(
        CharacterizationGateError,
        match="characterization_report_missing",
    ) as exc_info:
        resolve_characterization_gate(
            report_root=tmp_path / "missing",
            current_versions={"claude": "1.2.3", "codex": "4.5.6"},
        )
    assert exc_info.value.code == "missing"


def test_cli_version_preserves_full_normalized_bounded_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs = iter((
        "codex-cli 1.2.3-beta.1+build.7\r\nrelease channel: preview\r\n",
        "x" * 4097,
    ))

    def run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout=next(outputs))

    # Stubbed on the helper, not subprocess.run: `claude`/`codex` are npm
    # installs, so resolve_cli_executable hands back a .cmd batch shim on
    # Windows — cmd.exe is the direct child and node.exe is already a
    # grandchild that would inherit and hold open a capture pipe. _cli_version
    # therefore runs through run_text_capture.
    import hermes_cli._subprocess_compat as _spc

    monkeypatch.setattr(_spc, "run_text_capture", run)

    assert _cli_version(["codex", "--version"]) == (
        "codex-cli 1.2.3-beta.1+build.7\nrelease channel: preview"
    )
    assert _cli_version(["codex", "--version"]) is None


@_LIVE_ONLY
@pytest.mark.timeout(600)
def test_real_claude_and_codex_create_discover_read_and_resume() -> None:
    report_path = run_live_characterization(live_tests_enabled=True)
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report_path.parent == (
        Path.home() / ".hermes" / "session-bridge" / "characterization"
    )
    assert report["automatic_mirroring_enabled"] is False
    for provider in ("claude", "codex"):
        assert report["providers"][provider]["create"] is True
        assert report["providers"][provider]["discover"] is True
        assert report["providers"][provider]["read"] is True
        assert report["providers"][provider]["resume"] is True

    print(f"Hermes Session Bridge characterization report: {report_path}")
    print(
        "Codex registration turn required: "
        f"{report['providers']['codex']['used_registration_turn']}"
    )
