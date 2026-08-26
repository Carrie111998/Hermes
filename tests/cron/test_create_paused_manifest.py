from __future__ import annotations

import argparse
import copy
import errno
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cron.jobs import create_job, load_jobs, use_cron_store
from cron.paused_manifest import CONTRACT, PausedManifestError, stage_paused_manifest
from cron.scheduler import create_job_with_scheduler_registration
from hermes_cli.cron import cron_command
from hermes_cli.subcommands.cron import build_cron_parser
from tools.cronjob_tools import CRONJOB_SCHEMA, cronjob


def test_create_paused_is_atomic_from_first_save(tmp_path, monkeypatch):
    import cron.jobs as jobs

    snapshots = []
    real_save = jobs.save_jobs

    def capture(records, **kwargs):
        snapshots.append(copy.deepcopy(records))
        return real_save(records, **kwargs)

    monkeypatch.setattr(jobs, "save_jobs", capture)
    with use_cron_store(tmp_path):
        job = create_job(
            prompt="held",
            schedule="every 5m",
            initial_paused=True,
            paused_reason="readiness hold",
        )
        stored = load_jobs()

    assert snapshots == [stored] and stored == [job]
    assert job["enabled"] is False
    assert job["state"] == "paused"
    assert job["paused_at"] == job["created_at"]
    assert job["paused_reason"] == "readiness hold"
    assert job["next_run_at"] is None


@pytest.mark.parametrize("value", [None, 0, 1, "false", [], {}])
def test_create_paused_requires_strict_bool(tmp_path, value):
    with use_cron_store(tmp_path):
        with pytest.raises(ValueError, match="initial_paused must be a boolean"):
            create_job(prompt="held", schedule="every 5m", initial_paused=value)
        assert load_jobs() == []


@pytest.mark.parametrize("reason", ["", "   "])
def test_unpaused_create_rejects_any_supplied_paused_reason(tmp_path, reason):
    with use_cron_store(tmp_path):
        with pytest.raises(
            ValueError, match="paused_reason requires initial_paused=True"
        ):
            create_job(
                prompt="held",
                schedule="every 5m",
                initial_paused=False,
                paused_reason=reason,
            )
        assert load_jobs() == []


def test_paused_create_does_not_arm_scheduler_provider(tmp_path, monkeypatch):
    provider = MagicMock()
    monkeypatch.setattr(
        "cron.scheduler_provider.resolve_cron_scheduler", lambda: provider
    )
    with use_cron_store(tmp_path):
        job = create_job_with_scheduler_registration(
            prompt="held", schedule="every 5m", initial_paused=True
        )
    provider.register_job.assert_not_called()
    assert job["state"] == "paused"


def test_cronjob_tool_exposes_and_validates_atomic_pause(tmp_path):
    prop = CRONJOB_SCHEMA["parameters"]["properties"]["initial_paused"]
    assert prop["type"] == "boolean"
    with use_cron_store(tmp_path):
        bad = json.loads(
            cronjob(
                action="create",
                prompt="held",
                schedule="every 5m",
                initial_paused="false",
            )
        )
        good = json.loads(
            cronjob(
                action="create",
                prompt="held",
                schedule="every 5m",
                initial_paused=True,
                paused_reason="operator hold",
            )
        )
    assert bad["success"] is False
    assert bad["error"] == "initial_paused must be a boolean"
    assert good["success"] is True
    assert good["state"] == "paused"
    assert good["next_run_at"] is None


def test_cli_parser_exposes_atomic_pause_flags():
    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    build_cron_parser(subparsers, cmd_cron=cron_command)
    args = parser.parse_args([
        "cron",
        "create",
        "every 5m",
        "held",
        "--paused",
        "--paused-reason",
        "operator hold",
    ])
    assert args.initial_paused is True
    assert args.paused_reason == "operator hold"


def _manifest(tmp_path: Path, *, count: int = 3) -> tuple[Path, dict]:
    specs = []
    for index in range(count):
        specs.append({
            "id": f"{index + 1:012x}",
            "name": f"generic-paused-job-{index + 1}",
            "schedule": f"{index} * * * *",
            "prompt": f"perform generic task {index + 1}",
            "repeat": None,
            "deliver": "local",
            "provider": "openai",
            "model": "gpt-5",
        })
    data = {
        "contract": CONTRACT,
        "schema_version": 1,
        "paused_reason": "staged pending explicit activation",
        "jobs": specs,
    }
    path = tmp_path / "paused-jobs.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path, data


@pytest.mark.parametrize("count", [1, 3])
def test_generic_manifest_stages_any_nonzero_count_and_replays_exactly(
    tmp_path, monkeypatch, count
):
    import cron.paused_manifest as paused_manifest

    manifest_path, manifest = _manifest(tmp_path, count=count)
    save_calls = 0
    real_commit = paused_manifest._commit_registry

    def count_save(records):
        nonlocal save_calls
        save_calls += 1
        return real_commit(records)

    monkeypatch.setattr(paused_manifest, "_commit_registry", count_save)
    home = tmp_path / "home"
    with use_cron_store(home):
        dry = stage_paused_manifest(manifest_path, dry_run=True)
        assert not (home / "cron" / "jobs.json").exists()
        first = stage_paused_manifest(manifest_path)
        first_bytes = (home / "cron" / "jobs.json").read_bytes()
        stored = load_jobs()
        second = stage_paused_manifest(manifest_path)
        second_bytes = (home / "cron" / "jobs.json").read_bytes()

    assert dry["create_count"] == count and dry["mutated"] is False
    assert first["create_count"] == count and first["mutated"] is True
    assert second["create_count"] == 0 and second["mutated"] is False
    assert first["job_count"] == count
    assert save_calls == 1
    assert first_bytes == second_bytes
    assert {job["id"] for job in stored} == {item["id"] for item in manifest["jobs"]}
    assert all(job["enabled"] is False for job in stored)
    assert all(job["state"] == "paused" for job in stored)
    assert all(job["next_run_at"] is None for job in stored)
    assert all(job["paused_at"] == job["created_at"] for job in stored)


@pytest.mark.skipif(sys.platform == "win32", reason="symlink semantics require POSIX")
def test_manifest_stages_beside_resolved_symlink_target(tmp_path, monkeypatch):
    import cron.paused_manifest as paused_manifest

    manifest_path, _ = _manifest(tmp_path)
    home = tmp_path / "home"
    external = tmp_path / "external-store"
    external.mkdir()
    real_target = external / "jobs.json"
    real_target.write_text('{"jobs": [], "updated_at": "seed"}', encoding="utf-8")
    staged_dirs = []
    real_mkstemp = paused_manifest.tempfile.mkstemp

    def capture_mkstemp(*args, **kwargs):
        staged_dirs.append(Path(kwargs["dir"]))
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(paused_manifest.tempfile, "mkstemp", capture_mkstemp)
    with use_cron_store(home):
        registry = home / "cron" / "jobs.json"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.symlink_to(real_target)
        stage_paused_manifest(manifest_path)

    assert registry.is_symlink()
    assert staged_dirs == [real_target.parent]
    assert json.loads(real_target.read_text(encoding="utf-8"))["jobs"]


def test_manifest_replace_exdev_fails_without_copy_or_registry_mutation(
    tmp_path, monkeypatch
):
    import cron.paused_manifest as paused_manifest
    import shutil

    manifest_path, _ = _manifest(tmp_path)
    home = tmp_path / "home"
    with use_cron_store(home):
        create_job(prompt="unrelated", schedule="every 1h")
        registry = home / "cron" / "jobs.json"
        before = registry.read_bytes()

        def fail_replace(_source, _target):
            raise OSError(errno.EXDEV, "cross-device link")

        def forbidden_copy(*_args, **_kwargs):
            raise AssertionError("manifest commit must never copy over live registry")

        monkeypatch.setattr(paused_manifest.os, "replace", fail_replace)
        monkeypatch.setattr(shutil, "copyfile", forbidden_copy)
        with pytest.raises(PausedManifestError, match="jobs_registry_commit_failed"):
            stage_paused_manifest(manifest_path)

        assert registry.read_bytes() == before
        assert not list(registry.parent.glob(".jobs_manifest_*.tmp"))


@pytest.mark.parametrize("fault", ["after_replace", "directory_fsync"])
def test_manifest_landed_replace_failure_is_uncertain_and_exactly_replayable(
    tmp_path, monkeypatch, fault
):
    import cron.paused_manifest as paused_manifest

    manifest_path, _ = _manifest(tmp_path)
    home = tmp_path / "home"
    with use_cron_store(home):
        create_job(prompt="unrelated", schedule="every 1h")
        registry = home / "cron" / "jobs.json"
        before = registry.read_bytes()

        if fault == "after_replace":
            real_replace = paused_manifest.os.replace

            def replace_then_fail(source, target):
                real_replace(source, target)
                raise OSError("injected interruption after replace")

            monkeypatch.setattr(paused_manifest.os, "replace", replace_then_fail)
        else:

            def fail_directory_fsync(_target):
                raise OSError("injected directory fsync failure")

            monkeypatch.setattr(
                paused_manifest, "_fsync_parent_directory", fail_directory_fsync
            )

        with pytest.raises(
            PausedManifestError, match="^jobs_registry_commit_uncertain$"
        ):
            stage_paused_manifest(manifest_path)

        landed = registry.read_bytes()
        assert landed != before
        assert not list(registry.parent.glob(".jobs_manifest_*.tmp"))

        monkeypatch.undo()
        replay = stage_paused_manifest(manifest_path)
        assert replay["mutated"] is False
        assert replay["create_count"] == 0
        assert registry.read_bytes() == landed


def test_manifest_uncertain_commit_converges_via_replay_parent_fsync(
    tmp_path, monkeypatch
):
    import cron.jobs as jobs
    import cron.paused_manifest as paused_manifest

    manifest_path, _ = _manifest(tmp_path)
    home = tmp_path / "home"
    real_fsync_parent = paused_manifest._fsync_parent_directory
    fsync_targets = []
    lock_states = []

    def fail_first_parent_fsync(target):
        fsync_targets.append(target)
        lock_states.append(jobs._jobs_lock_state.cross_process_held)
        if len(fsync_targets) == 1:
            raise OSError("injected directory fsync failure")
        real_fsync_parent(target)

    monkeypatch.setattr(
        paused_manifest, "_fsync_parent_directory", fail_first_parent_fsync
    )
    with use_cron_store(home):
        with pytest.raises(
            PausedManifestError, match="^jobs_registry_commit_uncertain$"
        ):
            stage_paused_manifest(manifest_path)

        registry = home / "cron" / "jobs.json"
        landed = registry.read_bytes()
        replay = stage_paused_manifest(manifest_path)

        assert replay["mutated"] is False
        assert replay["create_count"] == 0
        assert registry.read_bytes() == landed
        assert fsync_targets == [registry, registry]
        assert lock_states == [True, True]


def test_manifest_exact_replay_parent_fsync_failure_is_uncertain(tmp_path, monkeypatch):
    import cron.paused_manifest as paused_manifest

    manifest_path, _ = _manifest(tmp_path)
    home = tmp_path / "home"
    with use_cron_store(home):
        stage_paused_manifest(manifest_path)
        registry = home / "cron" / "jobs.json"
        before = registry.read_bytes()

        def fail_parent_fsync(_target):
            raise OSError("injected replay directory fsync failure")

        monkeypatch.setattr(
            paused_manifest, "_fsync_parent_directory", fail_parent_fsync
        )
        with pytest.raises(
            PausedManifestError, match="^jobs_registry_commit_uncertain$"
        ):
            stage_paused_manifest(manifest_path)

        assert registry.read_bytes() == before


def test_manifest_exact_dry_run_replay_does_not_fsync(tmp_path, monkeypatch):
    import cron.paused_manifest as paused_manifest

    manifest_path, _ = _manifest(tmp_path)
    home = tmp_path / "home"
    with use_cron_store(home):
        stage_paused_manifest(manifest_path)
        registry = home / "cron" / "jobs.json"
        before = registry.read_bytes()

        def forbidden_parent_fsync(_target):
            raise AssertionError("dry-run must not fsync the registry directory")

        monkeypatch.setattr(
            paused_manifest, "_fsync_parent_directory", forbidden_parent_fsync
        )
        replay = stage_paused_manifest(manifest_path, dry_run=True)

        assert replay["mutated"] is False
        assert replay["create_count"] == 0
        assert registry.read_bytes() == before


def test_manifest_required_fchown_failure_is_precommit_and_byte_identical(
    tmp_path, monkeypatch
):
    import cron.paused_manifest as paused_manifest

    manifest_path, _ = _manifest(tmp_path)
    home = tmp_path / "home"
    with use_cron_store(home):
        create_job(prompt="unrelated", schedule="every 1h")
        registry = home / "cron" / "jobs.json"
        before = registry.read_bytes()
        monkeypatch.setattr(
            paused_manifest,
            "_required_ownership",
            lambda _target, _exists: (os.getuid() + 1, os.getgid()),
        )

        def fail_fchown(_fd, _uid, _gid):
            raise OSError("injected fchown failure")

        monkeypatch.setattr(paused_manifest.os, "fchown", fail_fchown)
        with pytest.raises(
            PausedManifestError, match="^jobs_registry_ownership_failed$"
        ):
            stage_paused_manifest(manifest_path)

        assert registry.read_bytes() == before
        assert not list(registry.parent.glob(".jobs_manifest_*.tmp"))


def test_paused_oneshot_manifest_replays_after_creation_grace_expires(
    tmp_path, monkeypatch
):
    import cron.jobs as jobs
    import cron.paused_manifest as paused_manifest

    initial_now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    delayed_now = initial_now + timedelta(minutes=10)
    manifest_path, data = _manifest(tmp_path, count=1)
    data["jobs"][0]["schedule"] = (initial_now + timedelta(minutes=1)).isoformat()
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(jobs, "_hermes_now", lambda: initial_now)
    monkeypatch.setattr(paused_manifest, "_hermes_now", lambda: initial_now)

    home = tmp_path / "home"
    with use_cron_store(home):
        first = stage_paused_manifest(manifest_path)
        registry = home / "cron" / "jobs.json"
        created = registry.read_bytes()

        monkeypatch.setattr(jobs, "_hermes_now", lambda: delayed_now)
        monkeypatch.setattr(paused_manifest, "_hermes_now", lambda: delayed_now)
        replay = stage_paused_manifest(manifest_path)

        assert first["mutated"] is True
        assert replay["mutated"] is False
        assert replay["create_count"] == 0
        assert registry.read_bytes() == created


def test_relative_paused_oneshot_manifest_replays_from_immutable_created_at(
    tmp_path, monkeypatch
):
    import cron.jobs as jobs
    import cron.paused_manifest as paused_manifest

    initial_now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    delayed_now = initial_now + timedelta(hours=1)
    manifest_path, data = _manifest(tmp_path, count=1)
    data["jobs"][0]["schedule"] = "30m"
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    # The manifest's captured creation instant is the deterministic base even
    # if a later clock read has advanced while the transaction is constructed.
    monkeypatch.setattr(jobs, "_hermes_now", lambda: initial_now + timedelta(seconds=5))
    monkeypatch.setattr(paused_manifest, "_hermes_now", lambda: initial_now)

    home = tmp_path / "home"
    with use_cron_store(home):
        first = stage_paused_manifest(manifest_path)
        registry = home / "cron" / "jobs.json"
        created = registry.read_bytes()
        stored = load_jobs()[0]
        assert stored["created_at"] == initial_now.isoformat()
        assert (
            stored["schedule"]["run_at"]
            == (initial_now + timedelta(minutes=30)).isoformat()
        )

        monkeypatch.setattr(jobs, "_hermes_now", lambda: delayed_now)
        monkeypatch.setattr(paused_manifest, "_hermes_now", lambda: delayed_now)
        replay = stage_paused_manifest(manifest_path)

        assert first["mutated"] is True
        assert replay["mutated"] is False
        assert replay["create_count"] == 0
        assert registry.read_bytes() == created


def test_new_paused_oneshot_manifest_still_rejects_expired_schedule(
    tmp_path, monkeypatch
):
    import cron.jobs as jobs
    import cron.paused_manifest as paused_manifest

    current = datetime(2030, 1, 1, tzinfo=timezone.utc)
    manifest_path, data = _manifest(tmp_path, count=1)
    data["jobs"][0]["schedule"] = (current - timedelta(minutes=10)).isoformat()
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(jobs, "_hermes_now", lambda: current)
    monkeypatch.setattr(paused_manifest, "_hermes_now", lambda: current)

    home = tmp_path / "home"
    with use_cron_store(home):
        with pytest.raises(PausedManifestError, match="^job_spec_invalid$"):
            stage_paused_manifest(manifest_path)
        assert not (home / "cron" / "jobs.json").exists()


@pytest.mark.parametrize("schema_version", [True, 1.0, "1", None])
def test_manifest_requires_strict_integer_schema_version(tmp_path, schema_version):
    manifest_path, data = _manifest(tmp_path)
    data["schema_version"] = schema_version
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    home = tmp_path / "home"
    with use_cron_store(home):
        with pytest.raises(PausedManifestError, match="manifest_contract_invalid"):
            stage_paused_manifest(manifest_path)
        assert not (home / "cron" / "jobs.json").exists()


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda data: data["jobs"].clear(), "jobs_invalid"),
        (
            lambda data: data["jobs"].__setitem__(1, dict(data["jobs"][0])),
            "duplicate_job_id",
        ),
        (
            lambda data: data["jobs"][0].__setitem__("secret", "do-not-report"),
            "job_shape_invalid",
        ),
        (
            lambda data: data["jobs"][0].__setitem__("no_agent", 1),
            "job_no_agent_invalid",
        ),
        (
            lambda data: data["jobs"][0].pop("provider"),
            "job_provider_model_pins_required",
        ),
    ],
)
def test_manifest_validation_failure_leaves_registry_byte_identical(
    tmp_path, mutate, error
):
    manifest_path, data = _manifest(tmp_path)
    home = tmp_path / "home"
    with use_cron_store(home):
        seed = create_job(prompt="unrelated", schedule="every 1h")
        registry = home / "cron" / "jobs.json"
        before = registry.read_bytes()
        mutate(data)
        manifest_path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(PausedManifestError) as exc_info:
            stage_paused_manifest(manifest_path)
        after = registry.read_bytes()
    assert str(exc_info.value) == error
    assert "do-not-report" not in str(exc_info.value)
    assert before == after
    assert seed["id"] in before.decode()


def test_manifest_rejects_existing_drift_without_write(tmp_path):
    manifest_path, data = _manifest(tmp_path)
    first = data["jobs"][0]
    home = tmp_path / "home"
    with use_cron_store(home):
        create_job(
            prompt=first["prompt"],
            schedule=first["schedule"],
            name=first["name"],
            deliver=first["deliver"],
            provider=first["provider"],
            model=first["model"],
            initial_paused=True,
            paused_reason="different hold",
        )
        registry = home / "cron" / "jobs.json"
        payload = json.loads(registry.read_text(encoding="utf-8"))
        payload["jobs"][0]["id"] = first["id"]
        registry.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        before = registry.read_bytes()
        with pytest.raises(PausedManifestError, match="existing_job_drift"):
            stage_paused_manifest(manifest_path)
        assert registry.read_bytes() == before


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock contention proof")
def test_manifest_lock_contention_fails_without_registry_mutation(
    tmp_path, monkeypatch
):
    import cron.jobs as jobs

    manifest_path, _ = _manifest(tmp_path)
    home = tmp_path / "home"
    ready = tmp_path / "lock-ready"
    release = tmp_path / "lock-release"
    child = None
    with use_cron_store(home):
        create_job(prompt="unrelated", schedule="every 1h")
        registry = home / "cron" / "jobs.json"
        before = registry.read_bytes()
        lock_path = jobs._jobs_lock_file()
        child_code = f"""
import fcntl, pathlib, time
path = pathlib.Path({str(lock_path)!r})
path.parent.mkdir(parents=True, exist_ok=True)
with path.open('a+') as fd:
    fcntl.flock(fd, fcntl.LOCK_EX)
    pathlib.Path({str(ready)!r}).write_text('ready')
    deadline = time.monotonic() + 10
    while not pathlib.Path({str(release)!r}).exists() and time.monotonic() < deadline:
        time.sleep(0.01)
"""
        child = subprocess.Popen([sys.executable, "-c", child_code])
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready.exists(), "child did not acquire jobs lock"
            monkeypatch.setattr(jobs, "_JOBS_LOCK_TIMEOUT_SECONDS", 0.05)
            with pytest.raises(jobs.JobsLockError, match="cross_process_lock_timeout"):
                stage_paused_manifest(manifest_path)
            assert registry.read_bytes() == before
        finally:
            release.write_text("release")
            if child is not None:
                child.wait(timeout=15)
