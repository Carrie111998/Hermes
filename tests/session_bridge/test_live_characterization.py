from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import session_bridge.characterize
from session_bridge.characterize import (
    BRIDGE_REVISION_EXCLUDED_MODULES,
    BRIDGE_REVISION_MODULES,
    BRIDGE_REVISION_ROOT_MODULES,
    CharacterizationGateError,
    _cli_version,
    _read_report_safely,
    current_bridge_revisions,
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


# ---------------------------------------------------------------------------
# bridge_revision: what code was this proof made against?
# ---------------------------------------------------------------------------


def _fake_package_root(tmp_path: Path) -> Path:
    """A stand-in session_bridge directory holding every revision-tracked file.

    The digest only ever reads the manifest's modules, so a handful of stub
    files exercises it exactly as the real package does -- and lets a test
    mutate one module without touching the installed checkout.
    """

    root = tmp_path / "package"
    root.mkdir()
    tracked = {
        module for modules in BRIDGE_REVISION_MODULES.values() for module in modules
    }
    for module in sorted(tracked):
        (root / f"{module}.py").write_text(f"# {module}\n", encoding="utf-8")
    (root / "store.py").write_text("# untracked by the manifest\n", encoding="utf-8")
    return root


def test_bridge_revision_digests_are_per_provider_and_reproducible(
    tmp_path: Path,
) -> None:
    root = _fake_package_root(tmp_path)

    revisions = current_bridge_revisions(package_root=root)

    assert set(revisions) == {"claude", "codex"}
    assert all(value.startswith("sha256:") for value in revisions.values())
    assert revisions["claude"] != revisions["codex"]
    assert revisions == current_bridge_revisions(package_root=root)


def test_bridge_revision_moves_only_for_the_provider_whose_module_changed(
    tmp_path: Path,
) -> None:
    root = _fake_package_root(tmp_path)
    before = current_bridge_revisions(package_root=root)

    (root / "claude_registrar.py").write_text("# changed\n", encoding="utf-8")
    after = current_bridge_revisions(package_root=root)

    assert after["claude"] != before["claude"]
    assert after["codex"] == before["codex"]


def test_bridge_revision_moves_for_both_when_the_harness_changes(
    tmp_path: Path,
) -> None:
    root = _fake_package_root(tmp_path)
    before = current_bridge_revisions(package_root=root)

    (root / "characterize.py").write_text("# changed harness\n", encoding="utf-8")
    after = current_bridge_revisions(package_root=root)

    assert after["claude"] != before["claude"]
    assert after["codex"] != before["codex"]


def test_bridge_revision_ignores_modules_outside_the_manifest(tmp_path: Path) -> None:
    root = _fake_package_root(tmp_path)
    before = current_bridge_revisions(package_root=root)

    (root / "store.py").write_text("# catalog churn\n", encoding="utf-8")

    assert current_bridge_revisions(package_root=root) == before


def test_bridge_revision_ignores_line_ending_translation(tmp_path: Path) -> None:
    root = _fake_package_root(tmp_path)
    (root / "models.py").write_bytes(b"one\ntwo\n")
    before = current_bridge_revisions(package_root=root)

    (root / "models.py").write_bytes(b"one\r\ntwo\r\n")

    assert current_bridge_revisions(package_root=root) == before


def test_bridge_revision_fails_closed_when_a_tracked_module_is_missing(
    tmp_path: Path,
) -> None:
    root = _fake_package_root(tmp_path)
    (root / "models.py").unlink()

    with pytest.raises(CharacterizationGateError) as excinfo:
        current_bridge_revisions(package_root=root)

    assert excinfo.value.code == "invalid"


def test_bridge_revision_manifest_covers_the_characterization_import_closure() -> None:
    """The manifest must not silently fall behind a new intra-package import.

    Each provider's manifest is the transitive top-level ``session_bridge``
    import closure of that provider's adapter modules -- stopping at the
    documented catalog/mirroring exclusions -- plus the shared harness.  Adding
    an import to an adapter therefore fails this test until the new module is
    either tracked or deliberately excluded.
    """

    import ast

    package_root = Path(session_bridge.characterize.__file__).parent

    def top_level_imports(module: str) -> set[str]:
        tree = ast.parse(
            (package_root / f"{module}.py").read_text(encoding="utf-8"),
            filename=f"{module}.py",
        )
        return {
            node.module
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module
        }

    for provider, roots in BRIDGE_REVISION_ROOT_MODULES.items():
        seen: set[str] = set()
        pending = list(roots)
        while pending:
            module = pending.pop()
            if module in seen or module in BRIDGE_REVISION_EXCLUDED_MODULES:
                continue
            seen.add(module)
            pending.extend(
                candidate
                for candidate in top_level_imports(module)
                if (package_root / f"{candidate}.py").exists()
            )
        assert set(BRIDGE_REVISION_MODULES[provider]) == seen | {"characterize"}


# ---------------------------------------------------------------------------
# schema v2: provider-scoped reports carrying a bridge_revision
# ---------------------------------------------------------------------------


_CURRENT_REVISION = {
    "claude": "sha256:" + "a" * 64,
    "codex": "sha256:" + "b" * 64,
}
_STALE_REVISION = {
    "claude": "sha256:" + "c" * 64,
    "codex": "sha256:" + "d" * 64,
}


def _v2_report(
    characterization_id: str,
    *,
    providers: tuple[str, ...] = ("claude", "codex"),
    versions: dict[str, str] | None = None,
    bridge_revision: dict[str, str] | None = None,
    created_at: str = "2026-07-14T12:00:00+00:00",
    passed: bool = True,
    registration_turn: bool = False,
    codex_native_id: str | None = None,
) -> dict[str, object]:
    all_versions = {"claude": "1.2.3", "codex": "4.5.6"}
    if versions is not None:
        all_versions.update(versions)
    revisions = dict(_CURRENT_REVISION)
    if bridge_revision is not None:
        revisions.update(bridge_revision)

    def provider(name: str) -> dict[str, object]:
        status: dict[str, object] = {
            "create": passed,
            "discover": passed,
            "read": passed,
            "resume": passed,
            "used_registration_turn": (registration_turn if name == "codex" else False),
            "cleanup": "archived" if name == "codex" else "quarantined",
            "error_code": None if passed else "synthetic_characterization_failure",
        }
        if name == "codex" and codex_native_id is not None:
            status["native_id"] = codex_native_id
        return status

    return {
        "schema_version": 2,
        "characterization_id": characterization_id,
        "created_at": created_at,
        "automatic_mirroring_enabled": False,
        "versions": {name: all_versions[name] for name in providers},
        "bridge_revision": {name: revisions[name] for name in providers},
        "providers": {name: provider(name) for name in providers},
    }


def _write_v2(root: Path, characterization_id: str, **overrides: object) -> Path:
    return write_characterization_report(
        _v2_report(characterization_id, **overrides),
        report_root=root,
        characterization_id=characterization_id,
    )


_ID_A = "11111111-1111-4111-8111-111111111111"
_ID_B = "22222222-2222-4222-8222-222222222222"


def test_gate_accepts_a_version_2_report_recording_both_providers(
    tmp_path: Path,
) -> None:
    path = _write_v2(tmp_path, _ID_A)

    gate = resolve_characterization_gate(
        report_root=tmp_path,
        current_versions={"claude": "1.2.3", "codex": "4.5.6"},
        current_bridge_revision=_CURRENT_REVISION,
    )

    assert gate.report_path == path
    assert gate.provider_characterization_ids == {"claude": _ID_A, "codex": _ID_A}


def test_version_2_report_without_a_bridge_revision_is_malformed(
    tmp_path: Path,
) -> None:
    report = _v2_report(_ID_A)
    del report["bridge_revision"]
    write_characterization_report(
        report, report_root=tmp_path, characterization_id=_ID_A
    )

    with pytest.raises(CharacterizationGateError) as excinfo:
        resolve_characterization_gate(
            report_root=tmp_path,
            current_versions={"claude": "1.2.3", "codex": "4.5.6"},
            current_bridge_revision=_CURRENT_REVISION,
        )

    assert excinfo.value.code == "invalid"


def test_version_2_report_key_sets_must_agree_across_the_three_maps(
    tmp_path: Path,
) -> None:
    report = _v2_report(_ID_A, providers=("codex",))
    report["bridge_revision"] = dict(_CURRENT_REVISION)
    write_characterization_report(
        report, report_root=tmp_path, characterization_id=_ID_A
    )

    with pytest.raises(CharacterizationGateError) as excinfo:
        resolve_characterization_gate(
            report_root=tmp_path,
            current_versions={"claude": "1.2.3", "codex": "4.5.6"},
            current_bridge_revision=_CURRENT_REVISION,
        )

    assert excinfo.value.code == "invalid"


def test_version_2_report_rejects_an_empty_provider_set(tmp_path: Path) -> None:
    report = _v2_report(_ID_A, providers=())
    write_characterization_report(
        report, report_root=tmp_path, characterization_id=_ID_A
    )

    with pytest.raises(CharacterizationGateError) as excinfo:
        resolve_characterization_gate(
            report_root=tmp_path,
            current_versions={"claude": "1.2.3", "codex": "4.5.6"},
            current_bridge_revision=_CURRENT_REVISION,
        )

    assert excinfo.value.code == "invalid"


def test_version_1_report_may_not_record_a_single_provider(tmp_path: Path) -> None:
    report = _report(_ID_A)
    report["providers"] = {"codex": report["providers"]["codex"]}
    write_characterization_report(
        report, report_root=tmp_path, characterization_id=_ID_A
    )

    with pytest.raises(CharacterizationGateError) as excinfo:
        resolve_characterization_gate(
            report_root=tmp_path,
            current_versions={"claude": "1.2.3", "codex": "4.5.6"},
            current_bridge_revision=_CURRENT_REVISION,
        )

    assert excinfo.value.code == "invalid"


def test_version_1_report_may_not_carry_a_bridge_revision(tmp_path: Path) -> None:
    report = _report(_ID_A)
    report["bridge_revision"] = dict(_CURRENT_REVISION)
    write_characterization_report(
        report, report_root=tmp_path, characterization_id=_ID_A
    )

    with pytest.raises(CharacterizationGateError) as excinfo:
        resolve_characterization_gate(
            report_root=tmp_path,
            current_versions={"claude": "1.2.3", "codex": "4.5.6"},
            current_bridge_revision=_CURRENT_REVISION,
        )

    assert excinfo.value.code == "invalid"


def test_version_2_bridge_revision_values_must_be_non_empty_strings(
    tmp_path: Path,
) -> None:
    report = _v2_report(_ID_A)
    report["bridge_revision"]["claude"] = ""
    write_characterization_report(
        report, report_root=tmp_path, characterization_id=_ID_A
    )

    with pytest.raises(CharacterizationGateError) as excinfo:
        resolve_characterization_gate(
            report_root=tmp_path,
            current_versions={"claude": "1.2.3", "codex": "4.5.6"},
            current_bridge_revision=_CURRENT_REVISION,
        )

    assert excinfo.value.code == "invalid"


# ---------------------------------------------------------------------------
# per-provider gate resolution
# ---------------------------------------------------------------------------


def test_gate_resolves_each_provider_from_its_own_newest_report(
    tmp_path: Path,
) -> None:
    """A Codex-only refresh leaves the standing Claude proof in force."""

    _write_v2(tmp_path, _ID_A, created_at="2026-07-14T12:00:00+00:00")
    newest = _write_v2(
        tmp_path,
        _ID_B,
        providers=("codex",),
        versions={"codex": "4.5.7"},
        created_at="2026-08-19T12:00:00+00:00",
    )

    gate = resolve_characterization_gate(
        report_root=tmp_path,
        current_versions={"claude": "1.2.3", "codex": "4.5.7"},
        current_bridge_revision=_CURRENT_REVISION,
    )

    assert gate.provider_characterization_ids == {"claude": _ID_A, "codex": _ID_B}
    assert gate.report_path == newest


def test_reused_proof_must_match_the_installed_bridge_revision(
    tmp_path: Path,
) -> None:
    """The hole scoping opens: a standing proof made against older bridge code."""

    _write_v2(
        tmp_path,
        _ID_A,
        bridge_revision={"claude": _STALE_REVISION["claude"]},
        created_at="2026-07-14T12:00:00+00:00",
    )
    _write_v2(
        tmp_path,
        _ID_B,
        providers=("codex",),
        versions={"codex": "4.5.7"},
        created_at="2026-08-19T12:00:00+00:00",
    )

    with pytest.raises(CharacterizationGateError) as excinfo:
        resolve_characterization_gate(
            report_root=tmp_path,
            current_versions={"claude": "1.2.3", "codex": "4.5.7"},
            current_bridge_revision=_CURRENT_REVISION,
        )

    assert excinfo.value.code == "revision_drift"


def test_the_newest_report_is_not_held_to_the_installed_bridge_revision(
    tmp_path: Path,
) -> None:
    """Status quo preserved: a fresh pair proof ages with the bridge as before.

    Blocking this would demand a live run per bridge commit -- the
    characterization path took over a hundred commits in seven weeks -- and an
    unaffordable gate is a bypassed gate.
    """

    _write_v2(tmp_path, _ID_A, bridge_revision=_STALE_REVISION)

    gate = resolve_characterization_gate(
        report_root=tmp_path,
        current_versions={"claude": "1.2.3", "codex": "4.5.6"},
        current_bridge_revision=_CURRENT_REVISION,
    )

    assert gate.characterization_id == _ID_A


def test_a_version_1_proof_cannot_be_reused_under_scoping(tmp_path: Path) -> None:
    """A v1 report records no revision, so a scoped reuse of it fails closed."""

    _write_report(tmp_path, _ID_A, mtime_ns=100, created_at="2026-07-14T12:00:00+00:00")
    _write_v2(
        tmp_path,
        _ID_B,
        providers=("codex",),
        versions={"codex": "4.5.7"},
        created_at="2026-08-19T12:00:00+00:00",
    )

    with pytest.raises(CharacterizationGateError) as excinfo:
        resolve_characterization_gate(
            report_root=tmp_path,
            current_versions={"claude": "1.2.3", "codex": "4.5.7"},
            current_bridge_revision=_CURRENT_REVISION,
        )

    assert excinfo.value.code == "revision_drift"


def test_newest_report_recording_a_provider_blocks_it_despite_an_older_pass(
    tmp_path: Path,
) -> None:
    _write_v2(tmp_path, _ID_A, created_at="2026-07-14T12:00:00+00:00")
    _write_v2(
        tmp_path,
        _ID_B,
        providers=("codex",),
        passed=False,
        created_at="2026-08-19T12:00:00+00:00",
    )

    with pytest.raises(CharacterizationGateError) as excinfo:
        resolve_characterization_gate(
            report_root=tmp_path,
            current_versions={"claude": "1.2.3", "codex": "4.5.6"},
            current_bridge_revision=_CURRENT_REVISION,
        )

    assert excinfo.value.code == "failed"


def test_gate_is_missing_when_no_report_records_a_provider(tmp_path: Path) -> None:
    _write_v2(tmp_path, _ID_A, providers=("claude",))

    with pytest.raises(CharacterizationGateError) as excinfo:
        resolve_characterization_gate(
            report_root=tmp_path,
            current_versions={"claude": "1.2.3", "codex": "4.5.6"},
            current_bridge_revision=_CURRENT_REVISION,
        )

    assert excinfo.value.code == "missing"


def test_version_drift_is_evaluated_per_provider(tmp_path: Path) -> None:
    _write_v2(tmp_path, _ID_A, created_at="2026-07-14T12:00:00+00:00")
    _write_v2(
        tmp_path,
        _ID_B,
        providers=("codex",),
        versions={"codex": "4.5.7"},
        created_at="2026-08-19T12:00:00+00:00",
    )

    with pytest.raises(CharacterizationGateError) as excinfo:
        resolve_characterization_gate(
            report_root=tmp_path,
            current_versions={"claude": "1.2.3", "codex": "4.5.9"},
            current_bridge_revision=_CURRENT_REVISION,
        )

    assert excinfo.value.code == "version_drift"


def test_codex_registration_flag_comes_from_the_report_that_proved_codex(
    tmp_path: Path,
) -> None:
    _write_v2(
        tmp_path,
        _ID_A,
        registration_turn=False,
        created_at="2026-07-14T12:00:00+00:00",
    )
    _write_v2(
        tmp_path,
        _ID_B,
        providers=("codex",),
        registration_turn=True,
        created_at="2026-08-19T12:00:00+00:00",
    )

    gate = resolve_characterization_gate(
        report_root=tmp_path,
        current_versions={"claude": "1.2.3", "codex": "4.5.6"},
        current_bridge_revision=_CURRENT_REVISION,
    )

    assert gate.codex_registration_turn_required is True


# ---------------------------------------------------------------------------
# Codex origin provenance survives Claude-only reports
# ---------------------------------------------------------------------------


def test_codex_origins_count_reports_that_record_no_codex_block(
    tmp_path: Path,
) -> None:
    """A Claude-only report carries no native Codex ID -- and must not raise.

    Every valid report still counts: an ID recorded by any report must never be
    mistaken for native user work, and a scoped Claude refresh must not disturb
    that ledger.
    """

    native_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    _write_v2(
        tmp_path,
        _ID_A,
        codex_native_id=native_id,
        created_at="2026-07-14T12:00:00+00:00",
    )
    _write_v2(
        tmp_path,
        _ID_B,
        providers=("claude",),
        created_at="2026-08-19T12:00:00+00:00",
    )

    assert load_codex_characterization_origins(report_root=tmp_path) == {
        native_id: f"characterization-{_ID_A}-codex",
    }


def test_codex_origin_guard_naming_a_claude_only_report_fails_closed(
    tmp_path: Path,
) -> None:
    marker_secret = b"x" * 32
    _write_v2(tmp_path, _ID_A, providers=("claude",))
    session_bridge.characterize._prepare_codex_origin_guard(
        tmp_path,
        characterization_id=_ID_A,
        marker_secret=marker_secret,
    )

    with pytest.raises(
        CharacterizationGateError,
        match="characterization_codex_origin_unresolved",
    ):
        load_codex_characterization_origins(
            report_root=tmp_path,
            marker_secret=marker_secret,
        )


# ---------------------------------------------------------------------------
# operator diagnostics
# ---------------------------------------------------------------------------


def test_gate_description_names_the_provider_whose_bridge_revision_drifted(
    tmp_path: Path,
) -> None:
    _write_v2(
        tmp_path,
        _ID_A,
        bridge_revision={"claude": _STALE_REVISION["claude"]},
        created_at="2026-07-14T12:00:00+00:00",
    )
    _write_v2(
        tmp_path,
        _ID_B,
        providers=("codex",),
        versions={"codex": "4.5.7"},
        created_at="2026-08-19T12:00:00+00:00",
    )

    exit_code, message = describe_characterization_gate(
        report_root=tmp_path,
        current_versions={"claude": "1.2.3", "codex": "4.5.7"},
        current_bridge_revision=_CURRENT_REVISION,
    )

    assert exit_code == 1
    assert "revision_drift" in message
    assert "claude" in message
    assert "--provider all" in message


def test_gate_description_offers_a_scoped_refresh_for_one_drifted_provider(
    tmp_path: Path,
) -> None:
    _write_v2(tmp_path, _ID_A)

    exit_code, message = describe_characterization_gate(
        report_root=tmp_path,
        current_versions={"claude": "1.2.3", "codex": "4.5.7"},
        current_bridge_revision=_CURRENT_REVISION,
    )

    assert exit_code == 1
    assert "--provider codex" in message
    assert "claude: unchanged" in message


def test_gate_description_keeps_the_full_refresh_when_both_providers_drift(
    tmp_path: Path,
) -> None:
    _write_v2(tmp_path, _ID_A)

    _, message = describe_characterization_gate(
        report_root=tmp_path,
        current_versions={"claude": "1.2.4", "codex": "4.5.7"},
        current_bridge_revision=_CURRENT_REVISION,
    )

    assert "--provider all" in message
    assert "--provider codex" not in message


# ---------------------------------------------------------------------------
# provider-scoped live characterization
# ---------------------------------------------------------------------------


@pytest.fixture
def stubbed_characterization(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Drive run_live_characterization without spawning either real CLI.

    Every seam that would reach a provider is replaced, so this fixture can
    never create a quota-consuming session; the calls list records which
    provider paths were entered.
    """

    calls: dict[str, list[str]] = {"version": [], "characterized": []}

    def fake_resolve(executable: str, **kwargs: object) -> tuple[str, ...]:
        return (executable,)

    def fake_version(command: list[str]) -> str:
        name = command[0]
        calls["version"].append(name)
        return f"{name}-9.9.9"

    def fake_claude(status: dict[str, object], **kwargs: object) -> None:
        calls["characterized"].append("claude")
        status.update({
            "create": True,
            "discover": True,
            "read": True,
            "resume": True,
            "cleanup": "quarantined",
            "native_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        })

    def fake_codex(status: dict[str, object], **kwargs: object) -> None:
        calls["characterized"].append("codex")
        native_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        status.update({
            "create": True,
            "discover": True,
            "read": True,
            "resume": True,
            "cleanup": "archived",
            "native_id": native_id,
        })
        record = kwargs["record_native_id"]
        record(native_id)

    monkeypatch.setattr(
        "session_bridge.characterize.resolve_cli_executable", fake_resolve
    )
    monkeypatch.setattr("session_bridge.characterize._cli_version", fake_version)
    monkeypatch.setattr("session_bridge.characterize._characterize_claude", fake_claude)
    monkeypatch.setattr("session_bridge.characterize._characterize_codex", fake_codex)
    return calls


def test_scoped_run_records_only_the_requested_provider(
    tmp_path: Path,
    stubbed_characterization: dict[str, list[str]],
) -> None:
    report_path = run_live_characterization(
        report_root=tmp_path,
        cwd=tmp_path,
        provenance_secret=b"z" * 32,
        live_tests_enabled=True,
        providers=("codex",),
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == 2
    assert set(report["providers"]) == {"codex"}
    assert set(report["versions"]) == {"codex"}
    assert set(report["bridge_revision"]) == {"codex"}
    assert (
        report["bridge_revision"]["codex"]
        == current_bridge_revisions(providers=("codex",))["codex"]
    )
    assert stubbed_characterization["characterized"] == ["codex"]
    assert stubbed_characterization["version"] == ["codex"]


def test_scoped_claude_run_creates_no_codex_origin_guard(
    tmp_path: Path,
    stubbed_characterization: dict[str, list[str]],
) -> None:
    run_live_characterization(
        report_root=tmp_path,
        cwd=tmp_path,
        provenance_secret=b"z" * 32,
        live_tests_enabled=True,
        providers=("claude",),
    )

    assert stubbed_characterization["characterized"] == ["claude"]
    assert not (tmp_path / ".codex-origin-guards").exists()


def test_default_run_still_records_both_providers(
    tmp_path: Path,
    stubbed_characterization: dict[str, list[str]],
) -> None:
    report_path = run_live_characterization(
        report_root=tmp_path,
        cwd=tmp_path,
        provenance_secret=b"z" * 32,
        live_tests_enabled=True,
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert set(report["providers"]) == {"claude", "codex"}
    assert set(report["bridge_revision"]) == {"claude", "codex"}
    assert sorted(stubbed_characterization["characterized"]) == ["claude", "codex"]


def test_run_rejects_an_unknown_provider_selection(
    tmp_path: Path,
    stubbed_characterization: dict[str, list[str]],
) -> None:
    with pytest.raises(RuntimeError, match="characterization_provider_invalid"):
        run_live_characterization(
            report_root=tmp_path,
            cwd=tmp_path,
            provenance_secret=b"z" * 32,
            live_tests_enabled=True,
            providers=("gemini",),
        )

    assert stubbed_characterization["characterized"] == []


def test_run_rejects_an_empty_provider_selection(
    tmp_path: Path,
    stubbed_characterization: dict[str, list[str]],
) -> None:
    with pytest.raises(RuntimeError, match="characterization_provider_invalid"):
        run_live_characterization(
            report_root=tmp_path,
            cwd=tmp_path,
            provenance_secret=b"z" * 32,
            live_tests_enabled=True,
            providers=(),
        )

    assert stubbed_characterization["characterized"] == []


# ---------------------------------------------------------------------------
# CLI: a scoped refresh is reachable
# ---------------------------------------------------------------------------


def test_backend_characterize_threads_a_scoped_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seen: dict[str, object] = {}

    def fake_run(**kwargs: object) -> Path:
        seen.update(kwargs)
        return tmp_path / "report.json"

    monkeypatch.setattr("session_bridge.cli.run_live_characterization", fake_run)
    monkeypatch.setattr("session_bridge.cli.resolve_marker_key", lambda: b"k" * 32)
    monkeypatch.setattr(
        "session_bridge.cli.resolve_characterization_gate",
        lambda: SimpleNamespace(
            characterization_id="cid",
            codex_registration_turn_required=False,
        ),
    )

    payload = ProductionBackend(BridgeConfig()).characterize(provider="codex")

    assert seen["providers"] == ("codex",)
    assert payload["passed"] is True


def test_backend_characterize_still_rejects_an_unknown_provider() -> None:
    with pytest.raises(ConfigurationFailure, match="characterization_provider"):
        ProductionBackend(BridgeConfig()).characterize(provider="gemini")


def test_characterize_command_accepts_a_scoped_provider_argument(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: dict[str, object] = {}
    backend = SimpleNamespace(
        characterize=lambda *, provider: (
            seen.update(provider=provider) or {"passed": True}
        ),
        close=lambda: None,
    )

    exit_code = main(
        ["characterize", "--provider", "codex"],
        config_loader=lambda: BridgeConfig.load(path=tmp_path / "missing.toml"),
        backend_factory=lambda config: backend,
    )

    assert exit_code == 0
    assert seen["provider"] == "codex"
    assert json.loads(capsys.readouterr().out) == {"passed": True}


def test_scoped_refresh_is_not_offered_when_it_would_strand_the_other_proof(
    tmp_path: Path,
) -> None:
    """Suggesting a doomed scoped refresh costs a real session for nothing.

    Refreshing Codex alone makes this report non-newest, at which point Claude's
    half of it faces the bridge-revision check -- and fails it.  The operator
    would pay one session and still be told to pay two.
    """

    _write_v2(tmp_path, _ID_A, bridge_revision={"claude": _STALE_REVISION["claude"]})

    _, message = describe_characterization_gate(
        report_root=tmp_path,
        current_versions={"claude": "1.2.3", "codex": "4.5.7"},
        current_bridge_revision=_CURRENT_REVISION,
    )

    assert "--provider all" in message
    assert "--provider codex" not in message


def test_scoped_refresh_is_not_offered_against_a_version_1_report(
    tmp_path: Path,
) -> None:
    """A v1 proof records no revision, so it can never survive scoped reuse."""

    _write_report(
        tmp_path,
        _ID_A,
        mtime_ns=100,
        versions={"claude": "2.1.216", "codex": "codex-cli 0.146.0"},
    )

    _, message = describe_characterization_gate(
        report_root=tmp_path,
        current_versions={"claude": "2.1.216", "codex": "codex-cli 0.147.0"},
        current_bridge_revision=_CURRENT_REVISION,
    )

    assert "--provider all" in message
    assert "--provider codex" not in message
