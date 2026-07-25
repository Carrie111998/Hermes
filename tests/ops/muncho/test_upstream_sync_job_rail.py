from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
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
    return release, rail.build_package(REVISION)


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
    assert rail.validate_manifest(manifest, revision=REVISION) == manifest

    assert "DynamicUser=yes" in sync_service
    assert "LoadCredential=github-token:" in sync_service
    assert "OPENAI_API_KEY" not in sync_service
    assert "DISCORD_BOT_TOKEN" not in sync_service
    assert "Environment=HERMES_HOME=" not in sync_service
    assert "AUTO_MERGE_DEPLOY_APPROVED" not in sync_service
    assert "muncho-auto-deploy-release" not in sync_service

    assert f"User={rail.REPORT_USER}" in report_service
    assert "LoadCredential=" not in report_service
    assert "GH_TOKEN" not in report_service
    assert str(rail.CREDENTIAL_SOURCE) not in report_service
    assert f"ReadOnlyPaths={rail.PUBLIC_REPORT_ROOT}" in report_service
    assert f"InaccessiblePaths=-{rail.STATE_ROOT}" in report_service
    assert str(release / rail.REPORTER_RELATIVE) in report_service

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

    assert rail.run_all(args) == 0
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
    assert all(item["content_recorded"] is False for item in receipt["children"])


def test_module_compiles_in_isolated_stdlib() -> None:
    result = __import__("subprocess").run(
        [sys.executable, "-I", "-S", "-B", str(MODULE), "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
