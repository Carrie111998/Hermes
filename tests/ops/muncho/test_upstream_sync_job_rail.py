from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[3]
MODULE = ROOT / "ops/muncho/runtime/upstream_sync_job_rail.py"
SPEC = importlib.util.spec_from_file_location("upstream_sync_job_rail_test", MODULE)
assert SPEC and SPEC.loader
rail = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = rail
SPEC.loader.exec_module(rail)
REVISION = "a" * 40


def _release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    releases = tmp_path / "releases"
    monkeypatch.setattr(rail, "RELEASES_ROOT", releases)
    release = releases / f"hermes-agent-{REVISION[:12]}"
    for relative in (
        rail.RAIL_RELATIVE,
        rail.MUNCHO_ROUTINE_RELATIVE,
        rail.HARDENING_RELATIVE,
        rail.SKYAI_ROUTINE_RELATIVE,
        rail.REPORTER_RELATIVE,
    ):
        target = release / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    (release / rail.SOURCE_MARKER_RELATIVE).write_text(
        REVISION + "\n",
        encoding="ascii",
    )
    interpreter = release / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"python-placeholder\n")
    interpreter.chmod(0o755)
    return release


def _package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    release = _release(tmp_path, monkeypatch)
    monkeypatch.setattr(rail, "validate_credential_metadata", lambda: None)
    monkeypatch.setattr(
        rail,
        "host_binary_fact",
        lambda path: "4" * 64 if path == rail.GH_PATH else "5" * 64,
    )
    return release, rail.build_package(REVISION, REVISION)


def test_package_has_exact_two_jobs_and_split_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release, package = _package(tmp_path, monkeypatch)
    manifest = json.loads(package.manifest_bytes)
    sync_service = package.artifacts[rail.SYNC_SERVICE_UNIT].decode()
    report_service = package.artifacts[rail.REPORT_SERVICE_UNIT].decode()
    sync_timer = package.artifacts[rail.SYNC_TIMER_UNIT].decode()
    report_timer = package.artifacts[rail.REPORT_TIMER_UNIT].decode()

    assert [job["job_id"] for job in manifest["jobs"]] == list(rail.JOB_IDS)
    assert [job["base_branch"] for job in manifest["jobs"]] == [
        "main",
        "codex/skyai-v2-hermes-plugin-bootstrap",
    ]
    assert all(job["argv"] == ["--execute"] for job in manifest["jobs"])
    assert all(
        job["upstream_repository_read_only"] == "NousResearch/hermes-agent"
        for job in manifest["jobs"]
    )
    assert all(job["auto_merge_or_deploy_enabled"] is False for job in manifest["jobs"])
    assert manifest["sync_service_model_or_provider_dependency"] is False
    assert manifest["sync_service_discord_dependency"] is False
    assert manifest["reporter_github_credential_dependency"] is False
    assert manifest["package_installs_or_starts_units"] is False
    assert (
        rail.validate_manifest(
            manifest,
            revision=REVISION,
            sender_revision=REVISION,
        )
        == manifest
    )

    assert "DynamicUser=yes" in sync_service
    assert "LoadCredential=github-token:" in sync_service
    assert "OPENAI_API_KEY" not in sync_service
    assert "DISCORD_BOT_TOKEN" not in sync_service
    assert "Environment=HERMES_HOME=" not in sync_service
    assert "AUTO_MERGE_DEPLOY_APPROVED" not in sync_service
    assert "muncho-auto-deploy-release" not in sync_service
    assert "IPAddressDeny=169.254.169.254/32" in sync_service
    assert (
        f"BindReadOnlyPaths={rail.SYSTEMD_STUB_RESOLV_CONF}:"
        f"{rail.SYSTEMD_UPLINK_RESOLV_CONF}"
        in sync_service
    )

    assert f"User={rail.REPORT_USER}" in report_service
    assert "LoadCredential=" not in report_service
    assert "GH_TOKEN" not in report_service
    assert str(rail.CREDENTIAL_SOURCE) not in report_service
    assert f"ReadOnlyPaths={rail.REPORT_VIEW_ROOT}" in report_service
    assert (
        f"BindReadOnlyPaths={rail.PRIVATE_PUBLIC_REPORT_ROOT}:"
        f"{rail.REPORT_VIEW_ROOT}"
        in report_service
    )
    assert f"--public-report-dir {rail.REPORT_VIEW_ROOT}" in report_service
    assert f"InaccessiblePaths=-{rail.STATE_ROOT}" in report_service
    assert str(release / rail.REPORTER_RELATIVE) in report_service
    assert (
        f"--sender-python-sha256 {manifest['sender_interpreter_sha256']}"
        in report_service
    )
    assert "IPAddressDeny=169.254.169.254/32" in report_service
    assert (
        f"BindReadOnlyPaths={rail.SYSTEMD_STUB_RESOLV_CONF}:"
        f"{rail.SYSTEMD_UPLINK_RESOLV_CONF}"
        in report_service
    )

    assert "OnActiveSec=30m" in sync_timer
    assert "OnUnitActiveSec=3h" in sync_timer
    assert "OnCalendar=" not in sync_timer
    assert "OnCalendar=*-*-* 08:00:00 Europe/Sofia" in report_timer
    assert "Persistent=true" in report_timer


def test_package_staging_is_byte_exact_and_inert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, package = _package(tmp_path, monkeypatch)
    staged = tmp_path / "staged"
    rail.stage_package(package, output_root=staged)
    rail.verify_package(package, output_root=staged)

    assert {path.name for path in staged.iterdir()} == {
        rail.SYNC_SERVICE_UNIT,
        rail.SYNC_TIMER_UNIT,
        rail.REPORT_SERVICE_UNIT,
        rail.REPORT_TIMER_UNIT,
        "manifest.json",
    }
    assert all(path.stat().st_mode & 0o777 == 0o444 for path in staged.iterdir())

    (staged / rail.SYNC_TIMER_UNIT).chmod(0o644)
    with pytest.raises(rail.DualSyncRailError, match="artifact_drifted"):
        rail.verify_package(package, output_root=staged)


def test_release_markers_are_exact_framed_revision_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = _release(tmp_path, monkeypatch)
    marker = release / rail.SOURCE_MARKER_RELATIVE
    monkeypatch.setattr(rail, "validate_credential_metadata", lambda: None)
    monkeypatch.setattr(rail, "host_binary_fact", lambda _path: "4" * 64)

    assert marker.read_bytes() == REVISION.encode("ascii") + b"\n"
    marker.write_bytes(b" " + REVISION.encode("ascii") + b"\n")
    with pytest.raises(rail.DualSyncRailError, match="release_marker_mismatch"):
        rail.build_package(REVISION, REVISION)

    marker.write_bytes(REVISION.encode("ascii") + b"\n\n")
    with pytest.raises(rail.DualSyncRailError, match="release_marker_mismatch"):
        rail.build_package(REVISION, REVISION)


def test_manifest_sender_interpreter_digest_rejects_type_lookalike(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, package = _package(tmp_path, monkeypatch)
    manifest = json.loads(package.manifest_bytes)
    manifest["sender_interpreter_sha256"] = int("4" * 64)

    with pytest.raises(rail.DualSyncRailError, match="manifest_invalid"):
        rail.validate_manifest(
            manifest,
            revision=REVISION,
            sender_revision=REVISION,
        )


def test_github_credential_preserves_exact_bytes_and_rejects_whitespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    credential = credentials / rail.CREDENTIAL_NAME
    token = "github_pat_" + "x" * 32
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))

    credential.write_text(token, encoding="ascii")
    assert rail.credential() == token

    credential.write_text(token + "\n", encoding="ascii")
    with pytest.raises(rail.DualSyncRailError, match="credential_invalid"):
        rail.credential()

    credential.write_text(f" {token}", encoding="ascii")
    with pytest.raises(rail.DualSyncRailError, match="credential_invalid"):
        rail.credential()


def test_package_hashes_final_sender_interpreter_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = _release(tmp_path, monkeypatch)
    interpreter = release / ".venv/bin/python"
    target = release / ".venv/bin/python-real"
    payload = b"resolved-python-placeholder\n"
    target.write_bytes(payload)
    target.chmod(0o755)
    interpreter.unlink()
    interpreter.symlink_to(target.name)
    monkeypatch.setattr(rail, "validate_credential_metadata", lambda: None)
    monkeypatch.setattr(rail, "host_binary_fact", lambda _path: "4" * 64)

    package = rail.build_package(REVISION, REVISION)
    manifest = json.loads(package.manifest_bytes)

    assert manifest["sender_interpreter_sha256"] == hashlib.sha256(
        payload
    ).hexdigest()


def test_rail_preserves_every_worktree_name_without_exact_inventory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "worktrees"
    lookalike = root / "codex-upstream-sync-auto-20260725-0100"
    unrelated = root / "operator-investigation"
    outside = tmp_path / "outside"
    lookalike.mkdir(parents=True)
    unrelated.mkdir()
    outside.mkdir()
    (lookalike / "tracked.txt").write_text("lookalike", encoding="utf-8")
    (root / "codex-upstream-sync-auto-symlink").symlink_to(
        outside,
        target_is_directory=True,
    )

    result = rail.no_worktree_cleanup_receipt()

    assert result == {"removed": 0, "failed": 0}
    assert lookalike.is_dir()
    assert unrelated.is_dir()
    assert outside.is_dir()
    assert (root / "codex-upstream-sync-auto-symlink").is_symlink()


def test_run_all_executes_both_jobs_and_publishes_only_sanitized_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    runtime = tmp_path / "run"
    public = tmp_path / "public"
    paths = {
        "rail": tmp_path / "rail.py",
        "muncho_routine": tmp_path / "muncho.py",
        "hardening": tmp_path / "hardening.py",
        "skyai_routine": tmp_path / "skyai.py",
        "reporter": tmp_path / "reporter.py",
    }
    monkeypatch.setattr(rail, "STATE_ROOT", state)
    monkeypatch.setattr(rail, "RUNTIME_ROOT", runtime)
    monkeypatch.setattr(rail, "PUBLIC_REPORT_ROOT", public)
    monkeypatch.setattr(
        rail,
        "attest_release",
        lambda _args: (tmp_path / "release", paths),
    )
    monkeypatch.setattr(rail, "credential", lambda: "github_pat_" + "x" * 32)

    calls: list[tuple[str, dict[str, str]]] = []

    def fake_child(*, job_id, routine, environment, report_path):
        del routine, report_path
        calls.append((job_id, dict(environment)))
        if job_id == rail.MUNCHO_JOB_ID:
            report = {
                "status": "blocked_merge_conflicts",
                "blocked": True,
                "fresh_refs": {
                    "fork_main_ref": "a" * 40,
                    "upstream_main_ref": "b" * 40,
                    "ahead_by": 12,
                    "behind_by": 34,
                },
                "error": "secret-shaped raw child text must not escape",
            }
            return rail.ChildResult(
                job_id, 2, False, 10, "1" * 64, 20, "2" * 64, report
            )
        report = {
            "status": "PASS",
            "outcome": "candidate_pr_ready",
            "source_sha": "c" * 40,
            "upstream_sha": "d" * 40,
            "candidate_sha": "e" * 40,
            "head_ahead": 90,
            "head_behind": 3,
            "pr_url": "https://github.com/lomliev/hermes-agent/pull/178",
            "internal_error": "must not escape",
        }
        return rail.ChildResult(
            job_id, 0, False, 30, "3" * 64, 40, "4" * 64, report
        )

    monkeypatch.setattr(rail, "run_child", fake_child)
    args = argparse.Namespace(revision=REVISION)

    assert rail.run_all(args) == rail.EXIT_BLOCKED
    assert [job for job, _env in calls] == list(rail.JOB_IDS)
    assert all(env["GH_TOKEN"].startswith("github_pat_") for _job, env in calls)
    assert all("DISCORD_BOT_TOKEN" not in env for _job, env in calls)
    public_report = json.loads((public / "latest.json").read_text())
    encoded = json.dumps(public_report)

    assert public_report["status"] == "BLOCKED"
    assert public_report["muncho"]["blocker"] == "blocked_merge_conflicts"
    assert public_report["skyai"]["status"] == "PASS"
    assert public_report["skyai"]["pr_url"].endswith("/178")
    assert "secret-shaped" not in encoded
    assert "must not escape" not in encoded
    assert "github_pat_" not in encoded
    assert public_report["provider_or_model_invoked"] is False
    assert public_report["discord_delivery_attempted"] is False
    receipt = json.loads((state / "latest.json").read_text())
    assert receipt["secret_material_recorded"] is False
    assert receipt["inter_job_cleanup"] == {"removed": 0, "failed": 0}
    assert all(item["content_recorded"] is False for item in receipt["children"])


def test_run_child_cannot_replay_a_stale_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = tmp_path / "state" / "latest.json"
    report.parent.mkdir()
    report.write_text(
        json.dumps({"status": "PASS", "outcome": "stale"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(rail, "RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(
        rail.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=1,
        ),
    )

    result = rail.run_child(
        job_id=rail.MUNCHO_JOB_ID,
        routine=tmp_path / "routine.py",
        environment={},
        report_path=report,
    )

    assert result.returncode == 1
    assert result.report is None
    assert not report.exists()


def test_muncho_component_requires_exact_blocked_boolean():
    base = dict(
        job_id=rail.MUNCHO_JOB_ID,
        returncode=0,
        timed_out=False,
        stdout_bytes=0,
        stdout_sha256="1" * 64,
        stderr_bytes=0,
        stderr_sha256="2" * 64,
    )
    lookalike = rail.ChildResult(
        **base,
        report={"status": "blocked_merge_conflicts", "blocked": False},
    )
    invalid = rail.ChildResult(
        **base,
        report={"status": "no_drift_no_action", "blocked": "false"},
    )
    exact = rail.ChildResult(
        **{**base, "returncode": 2},
        report={"status": "candidate_identity_mismatch", "blocked": True},
    )

    assert rail.muncho_component(lookalike)["status"] == "PARTIAL"
    assert rail.muncho_component(lookalike)["blocker"] is None
    assert rail.muncho_component(invalid)["status"] == "BLOCKED"
    assert (
        rail.muncho_component(invalid)["blocker"]
        == "invalid_blocked_field"
    )
    assert rail.muncho_component(exact)["status"] == "BLOCKED"
    assert (
        rail.muncho_component(exact)["blocker"]
        == "candidate_identity_mismatch"
    )


def test_muncho_component_rejects_report_exit_mismatch():
    result = rail.ChildResult(
        job_id=rail.MUNCHO_JOB_ID,
        returncode=0,
        timed_out=False,
        stdout_bytes=0,
        stdout_sha256="1" * 64,
        stderr_bytes=0,
        stderr_sha256="2" * 64,
        report={"status": "blocked_merge_conflicts", "blocked": True},
    )

    component = rail.muncho_component(result)

    assert component["status"] == "BLOCKED"
    assert component["blocker"] == "child_exit_status_mismatch"


def test_skyai_component_requires_exact_status_enum_without_coercion():
    base = dict(
        job_id=rail.SKYAI_JOB_ID,
        returncode=0,
        timed_out=False,
        stdout_bytes=0,
        stdout_sha256="1" * 64,
        stderr_bytes=0,
        stderr_sha256="2" * 64,
    )
    exact = rail.ChildResult(
        **base,
        report={"status": "PASS", "outcome": "up_to_date"},
    )
    case_lookalike = rail.ChildResult(
        **base,
        report={"status": "pass", "outcome": "up_to_date"},
    )
    non_string = rail.ChildResult(
        **base,
        report={"status": 1, "outcome": "up_to_date"},
    )

    assert rail.skyai_component(exact)["status"] == "PASS"
    assert rail.skyai_component(case_lookalike)["status"] == "BLOCKED"
    assert rail.skyai_component(case_lookalike)["blocker"] == "invalid_status"
    assert rail.skyai_component(non_string)["status"] == "BLOCKED"
    assert rail.skyai_component(non_string)["blocker"] == "invalid_status"


def test_skyai_component_rejects_report_exit_mismatch():
    result = rail.ChildResult(
        job_id=rail.SKYAI_JOB_ID,
        returncode=0,
        timed_out=False,
        stdout_bytes=0,
        stdout_sha256="1" * 64,
        stderr_bytes=0,
        stderr_sha256="2" * 64,
        report={"status": "PARTIAL", "outcome": "candidate_ci_pending"},
    )

    component = rail.skyai_component(result)

    assert component["status"] == "BLOCKED"
    assert component["blocker"] == "child_exit_status_mismatch"


def test_safe_protocol_fields_reject_non_string_lookalikes():
    assert rail.safe_code(123, "fallback") == "fallback"
    assert rail.safe_sha(1) is None
    assert rail.safe_pr(
        Path("https://github.com/lomliev/hermes-agent/pull/178")
    ) is None


def test_module_compiles_in_isolated_stdlib() -> None:
    result = __import__("subprocess").run(
        [sys.executable, "-I", "-S", "-B", str(MODULE), "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
