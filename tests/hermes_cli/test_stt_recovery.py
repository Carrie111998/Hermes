from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import stat

import pytest

import hermes_cli.stt_recovery as stt_recovery
from hermes_cli.stt_recovery import RecoveryPolicy, SttRecoveryCache


def _policy(
    *,
    retention_seconds: float = 3600,
    max_entries: int = 5,
    max_total_bytes: int = 1024,
) -> RecoveryPolicy:
    return RecoveryPolicy(
        enabled=True,
        retention_seconds=retention_seconds,
        max_entries=max_entries,
        max_total_bytes=max_total_bytes,
    )


def test_policy_defaults_are_bounded_and_zero_disables_recovery():
    defaults = RecoveryPolicy.from_config({})
    assert defaults.enabled is True
    assert defaults.retention_hours == 24
    assert defaults.max_entries == 50
    assert defaults.max_total_bytes == 500 * 1024 * 1024

    disabled = RecoveryPolicy.from_config({"stt": {"recovery": {"retention_hours": 0}}})
    assert disabled.enabled is False

    bounded = RecoveryPolicy.from_config({
        "stt": {
            "recovery": {
                "retention_hours": 9999,
                "max_entries": 9999,
                "max_total_mb": 9999,
            }
        }
    })
    assert bounded.retention_hours == 168
    assert bounded.max_entries == 500
    assert bounded.max_total_bytes == 2048 * 1024 * 1024


def test_stage_and_mark_failed_preserve_exact_private_original(tmp_path):
    cache = SttRecoveryCache(_policy(), hermes_home=tmp_path)
    original = b"\x1aE\xdf\xa3original-webm-bytes"

    staged = cache.stage_audio(
        original,
        suffix=".webm",
        mime_type="audio/webm;codecs=opus",
    )
    assert staged is not None
    failed = cache.mark_failed_attempt(
        staged.recovery_id,
        attempts=staged.attempts,
        failure_code="provider_error",
        provider="openai",
    )

    assert failed is not None
    assert failed.status == "failed"
    assert failed.audio_path.read_bytes() == original
    manifest = json.loads(
        (failed.directory / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["provider"] == "openai"
    assert manifest["failure_code"] == "provider_error"
    assert "path" not in manifest
    assert "error" not in manifest

    if os.name == "posix":
        assert stat.S_IMODE(cache.root.stat().st_mode) == 0o700
        assert stat.S_IMODE(failed.directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(failed.audio_path.stat().st_mode) == 0o600
        assert (
            stat.S_IMODE((failed.directory / "manifest.json").stat().st_mode) == 0o600
        )
        assert stat.S_IMODE((cache.root / ".lock").stat().st_mode) == 0o600


def test_discard_requires_failed_state_and_exact_attempt(tmp_path):
    cache = SttRecoveryCache(_policy(), hermes_home=tmp_path)
    staged = cache.stage_audio(b"voice", suffix=".wav", mime_type="audio/wav")
    assert staged is not None

    assert cache.discard_failed(staged.recovery_id) is False
    assert (
        cache.discard_attempt(
            staged.recovery_id,
            attempts=staged.attempts + 1,
            expected_status="transcribing",
        )
        is False
    )
    assert (
        cache.discard_attempt(
            staged.recovery_id,
            attempts=staged.attempts,
            expected_status="transcribing",
        )
        is True
    )
    assert not staged.directory.exists()
    assert cache.discard_failed("../outside") is False


def test_successful_attempt_retries_cleanup_without_becoming_retryable(
    tmp_path,
    monkeypatch,
):
    cache = SttRecoveryCache(_policy(), hermes_home=tmp_path)
    staged = cache.stage_audio(b"voice", suffix=".wav", mime_type="audio/wav")
    assert staged is not None
    remove_directory = cache._remove_directory
    monkeypatch.setattr(cache, "_remove_directory", lambda path: False)

    assert cache.complete_attempt(
        staged.recovery_id,
        attempts=staged.attempts,
        expected_status="transcribing",
    )
    assert cache.get_record(staged.recovery_id) is None
    assert cache.list_records() == []
    manifest = json.loads(
        (staged.directory / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "cleanup_pending"
    assert cache.claim_retry(staged.recovery_id) is None
    assert cache.discard_failed(staged.recovery_id) is False

    monkeypatch.setattr(cache, "_remove_directory", remove_directory)
    cache.prune()
    assert not staged.directory.exists()


def test_successful_attempt_keeps_lease_when_cleanup_commit_fails(
    tmp_path,
    monkeypatch,
):
    cache = SttRecoveryCache(_policy(), hermes_home=tmp_path)
    staged = cache.stage_audio(b"voice", suffix=".wav", mime_type="audio/wav")
    assert staged is not None

    real_atomic_json_write = stt_recovery.atomic_json_write
    monkeypatch.setattr(
        stt_recovery,
        "atomic_json_write",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    assert not cache.complete_attempt(
        staged.recovery_id,
        attempts=staged.attempts,
        expected_status="transcribing",
    )
    assert (staged.recovery_id, staged.attempts) in cache._attempt_leases
    retained = cache.get_record(staged.recovery_id)
    assert retained is not None
    assert retained.status == "transcribing"

    monkeypatch.setattr(stt_recovery, "atomic_json_write", real_atomic_json_write)
    failed = cache.mark_failed_attempt(
        staged.recovery_id,
        attempts=staged.attempts,
        failure_code="cleanup_error",
    )
    assert failed is not None
    assert failed.status == "failed"


def test_prune_enforces_entry_and_byte_caps_oldest_first(tmp_path):
    now = [1000.0]
    cache = SttRecoveryCache(
        _policy(max_entries=2, max_total_bytes=6),
        hermes_home=tmp_path,
        now=lambda: now[0],
    )

    first = cache.stage_audio(b"aaa", suffix=".wav", mime_type="audio/wav")
    assert first is not None
    cache.mark_failed_attempt(
        first.recovery_id,
        attempts=first.attempts,
        failure_code="provider_error",
    )
    now[0] += 1
    second = cache.stage_audio(b"bbb", suffix=".wav", mime_type="audio/wav")
    assert second is not None
    cache.mark_failed_attempt(
        second.recovery_id,
        attempts=second.attempts,
        failure_code="provider_error",
    )
    now[0] += 1
    third = cache.stage_audio(b"ccc", suffix=".wav", mime_type="audio/wav")
    assert third is not None
    cache.mark_failed_attempt(
        third.recovery_id,
        attempts=third.attempts,
        failure_code="provider_error",
    )

    ids = {record.recovery_id for record in cache.list_records()}
    assert ids == {second.recovery_id, third.recovery_id}
    assert not first.directory.exists()


def test_active_record_is_not_evicted_to_make_room(tmp_path):
    cache = SttRecoveryCache(
        _policy(max_entries=1, max_total_bytes=10),
        hermes_home=tmp_path,
    )
    active = cache.stage_audio(b"one", suffix=".wav", mime_type="audio/wav")
    assert active is not None

    assert cache.stage_audio(b"two", suffix=".wav", mime_type="audio/wav") is None
    assert active.audio_path.read_bytes() == b"one"


def test_expired_failure_and_crash_staging_are_pruned(tmp_path):
    now = [1000.0]
    cache = SttRecoveryCache(
        _policy(retention_seconds=10),
        hermes_home=tmp_path,
        now=lambda: now[0],
    )
    failed = cache.stage_audio(b"old", suffix=".wav", mime_type="audio/wav")
    assert failed is not None
    cache.mark_failed_attempt(
        failed.recovery_id,
        attempts=failed.attempts,
        failure_code="provider_error",
    )

    staging = cache.root / (".tmp-" + "a" * 32)
    staging.mkdir(mode=0o700)
    os.utime(staging, (now[0] - 7200, now[0] - 7200))
    now[0] += 11
    cache.prune()

    assert not failed.directory.exists()
    assert not staging.exists()


def test_expired_failure_is_hidden_before_physical_deletion(tmp_path, monkeypatch):
    now = [1000.0]
    cache = SttRecoveryCache(
        _policy(retention_seconds=10),
        hermes_home=tmp_path,
        now=lambda: now[0],
    )
    failed = cache.stage_audio(b"private", suffix=".wav", mime_type="audio/wav")
    assert failed is not None
    cache.mark_failed_attempt(
        failed.recovery_id,
        attempts=failed.attempts,
        failure_code="provider_error",
    )
    remove_directory = cache._remove_directory
    monkeypatch.setattr(cache, "_remove_directory", lambda path: False)

    now[0] += 11
    assert cache.get_record(failed.recovery_id) is None
    assert cache.list_records() == []
    assert cache.claim_retry(failed.recovery_id) is None
    assert cache.discard_failed(failed.recovery_id) is False
    manifest = json.loads(
        (failed.directory / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "cleanup_pending"

    monkeypatch.setattr(cache, "_remove_directory", remove_directory)
    cache.prune()
    assert not failed.directory.exists()


def test_expired_failure_is_hidden_when_tombstone_commit_fails(
    tmp_path,
    monkeypatch,
):
    now = [1000.0]
    cache = SttRecoveryCache(
        _policy(retention_seconds=10),
        hermes_home=tmp_path,
        now=lambda: now[0],
    )
    failed = cache.stage_audio(b"private", suffix=".wav", mime_type="audio/wav")
    assert failed is not None
    cache.mark_failed_attempt(
        failed.recovery_id,
        attempts=failed.attempts,
        failure_code="provider_error",
    )
    monkeypatch.setattr(cache, "_remove_directory", lambda path: False)
    monkeypatch.setattr(
        stt_recovery,
        "atomic_json_write",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read only")),
    )

    now[0] += 11
    assert cache.get_record(failed.recovery_id) is None
    assert cache.list_records() == []
    assert cache.claim_retry(failed.recovery_id) is None
    manifest = json.loads(
        (failed.directory / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "failed"


def test_capacity_delete_failure_preserves_existing_recoverable_records(
    tmp_path,
    monkeypatch,
):
    cache = SttRecoveryCache(
        _policy(max_entries=2, max_total_bytes=10),
        hermes_home=tmp_path,
    )
    first = cache.stage_audio(b"one", suffix=".wav", mime_type="audio/wav")
    assert first is not None
    cache.mark_failed_attempt(
        first.recovery_id,
        attempts=first.attempts,
        failure_code="provider_error",
    )
    second = cache.stage_audio(b"two", suffix=".wav", mime_type="audio/wav")
    assert second is not None
    cache.mark_failed_attempt(
        second.recovery_id,
        attempts=second.attempts,
        failure_code="provider_error",
    )
    monkeypatch.setattr(cache, "_remove_directory", lambda path: False)

    assert cache.stage_audio(b"new", suffix=".wav", mime_type="audio/wav") is None
    records = cache.list_records()
    assert {record.recovery_id for record in records} == {
        first.recovery_id,
        second.recovery_id,
    }
    assert all(record.status == "failed" for record in records)


def test_failure_retention_starts_when_transcription_fails(tmp_path):
    now = [1000.0]
    cache = SttRecoveryCache(
        _policy(retention_seconds=10),
        hermes_home=tmp_path,
        now=lambda: now[0],
    )
    staged = cache.stage_audio(b"slow", suffix=".wav", mime_type="audio/wav")
    assert staged is not None

    now[0] += 9
    failed = cache.mark_failed_attempt(
        staged.recovery_id,
        attempts=staged.attempts,
        failure_code="provider_error",
    )
    assert failed is not None
    assert failed.expires_at == 1019.0

    now[0] = 1011.0
    assert cache.get_record(staged.recovery_id) is not None
    now[0] = 1020.0
    assert cache.get_record(staged.recovery_id) is None
    assert cache.claim_retry(staged.recovery_id) is None


def test_failed_transition_reports_only_durable_retryable_state(
    tmp_path,
    monkeypatch,
):
    cache = SttRecoveryCache(_policy(), hermes_home=tmp_path)
    staged = cache.stage_audio(b"voice", suffix=".wav", mime_type="audio/wav")
    assert staged is not None

    monkeypatch.setattr(
        stt_recovery,
        "atomic_json_write",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    assert (
        cache.mark_failed_attempt(
            staged.recovery_id,
            attempts=staged.attempts,
            failure_code="provider_error",
        )
        is None
    )
    retained = cache.get_record(staged.recovery_id)
    assert retained is not None
    assert retained.status == "transcribing"


def test_abandoned_transcribing_record_becomes_retryable_immediately(tmp_path):
    now = [1000.0]
    cache = SttRecoveryCache(
        _policy(retention_seconds=7200),
        hermes_home=tmp_path,
        now=lambda: now[0],
    )
    staged = cache.stage_audio(b"voice", suffix=".ogg", mime_type="audio/ogg")
    assert staged is not None

    # Simulate a crashed worker: the OS releases this lease when its process
    # exits, while the published manifest remains active on disk.
    cache._release_attempt_lease(staged.recovery_id, staged.attempts)
    records = cache.list_records()

    assert len(records) == 1
    assert records[0].status == "failed"
    assert records[0].failure_code == "interrupted"
    claimed = cache.claim_retry(staged.recovery_id)
    assert claimed is not None
    assert claimed.status == "transcribing"
    assert claimed.attempts == 2


def test_abandoned_delivery_becomes_retryable_immediately(tmp_path):
    now = [1000.0]
    cache = SttRecoveryCache(
        _policy(retention_seconds=7200),
        hermes_home=tmp_path,
        now=lambda: now[0],
    )
    staged = cache.stage_audio(b"voice", suffix=".ogg", mime_type="audio/ogg")
    assert staged is not None
    delivering = cache.mark_delivering_attempt(
        staged.recovery_id,
        attempts=staged.attempts,
    )
    assert delivering is not None
    assert cache.claim_retry(staged.recovery_id) is None

    cache._release_attempt_lease(delivering.recovery_id, delivering.attempts)
    retained = cache.get_record(staged.recovery_id)

    assert retained is not None
    assert retained.status == "failed"
    assert retained.failure_code == "interrupted"
    assert retained.expires_at == now[0] + cache.policy.retention_seconds


def test_live_attempt_lease_prevents_time_only_reclassification(tmp_path):
    now = [1000.0]
    worker_cache = SttRecoveryCache(
        _policy(retention_seconds=7200),
        hermes_home=tmp_path,
        now=lambda: now[0],
    )
    staged = worker_cache.stage_audio(
        b"slow local model",
        suffix=".wav",
        mime_type="audio/wav",
    )
    assert staged is not None

    now[0] += 7200
    observer_cache = SttRecoveryCache(
        _policy(retention_seconds=7200),
        hermes_home=tmp_path,
        now=lambda: now[0],
    )
    observed = observer_cache.get_record(staged.recovery_id)

    assert observed is not None
    assert observed.status == "transcribing"
    assert observer_cache.claim_retry(staged.recovery_id) is None


def test_late_attempt_cannot_overwrite_or_delete_newer_retry(tmp_path):
    cache = SttRecoveryCache(_policy(), hermes_home=tmp_path)
    first = cache.stage_audio(b"voice", suffix=".wav", mime_type="audio/wav")
    assert first is not None
    failed = cache.mark_failed_attempt(
        first.recovery_id,
        attempts=first.attempts,
        failure_code="provider_error",
    )
    assert failed is not None
    second = cache.claim_retry(first.recovery_id)
    assert second is not None

    assert (
        cache.mark_failed_attempt(
            first.recovery_id,
            attempts=first.attempts,
            failure_code="late_callback",
        )
        is None
    )
    assert (
        cache.discard_attempt(
            first.recovery_id,
            attempts=first.attempts,
            expected_status="transcribing",
        )
        is False
    )
    current = cache.get_record(first.recovery_id)
    assert current is not None
    assert current.status == "transcribing"
    assert current.attempts == second.attempts


def test_corrupt_unknown_and_symlink_entries_are_never_followed(tmp_path):
    cache = SttRecoveryCache(_policy(), hermes_home=tmp_path)
    cache.root.mkdir(parents=True)

    unknown = cache.root / "keep-me"
    unknown.write_text("user data", encoding="utf-8")
    corrupt = cache.root / ("b" * 32)
    corrupt.mkdir()
    (corrupt / "manifest.json").write_text("not json", encoding="utf-8")

    outside = tmp_path / "outside"
    outside.mkdir()
    symlink = cache.root / ("c" * 32)
    try:
        symlink.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable")

    cache.prune()

    assert unknown.read_text(encoding="utf-8") == "user data"
    assert corrupt.exists()
    assert symlink.is_symlink()
    assert outside.exists()
    assert cache.get_record("../outside") is None


def test_recovery_root_symlink_is_fail_closed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"private")

    cache = SttRecoveryCache(_policy(), hermes_home=tmp_path / "profile")
    cache.root.parent.mkdir(parents=True)
    try:
        cache.root.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable")

    assert cache.stage_audio(b"voice", suffix=".wav", mime_type="audio/wav") is None
    assert cache.get_record("a" * 32) is None
    assert cache.claim_retry("a" * 32) is None
    assert cache.discard_failed("a" * 32) is False
    cache.prune()
    assert sentinel.read_bytes() == b"private"
    assert cache.root.is_symlink()


def test_corrupt_uuid_directories_are_bounded_and_count_toward_capacity(tmp_path):
    now = [10_000.0]
    cache = SttRecoveryCache(
        _policy(max_entries=2, max_total_bytes=5),
        hermes_home=tmp_path,
        now=lambda: now[0],
    )
    cache.root.mkdir(parents=True)

    recent = cache.root / ("d" * 32)
    recent.mkdir()
    (recent / "partial").write_bytes(b"12345")
    assert cache.stage_audio(b"x", suffix=".wav", mime_type="audio/wav") is None

    old = cache.root / ("e" * 32)
    old.mkdir()
    (old / "broken").write_bytes(b"x")
    os.utime(old, (now[0] - 3601, now[0] - 3601))
    cache.prune()
    assert recent.exists()
    assert not old.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("created_at", float("nan")),
        ("updated_at", float("inf")),
        ("expires_at", 1e300),
        ("updated_at", 1e9),
    ],
)
def test_corrupt_manifest_timestamps_are_rejected_and_eventually_pruned(
    tmp_path,
    field,
    value,
):
    now = [10_000.0]
    cache = SttRecoveryCache(
        _policy(),
        hermes_home=tmp_path,
        now=lambda: now[0],
    )
    staged = cache.stage_audio(b"voice", suffix=".wav", mime_type="audio/wav")
    assert staged is not None
    cache._release_attempt_lease(staged.recovery_id, staged.attempts)

    manifest_path = staged.directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert cache.get_record(staged.recovery_id) is None
    assert staged.directory.exists()

    old = now[0] - 3601
    os.utime(staged.directory, (old, old))
    cache.prune()
    assert not staged.directory.exists()


def test_invalid_suffix_and_oversized_record_are_not_published(tmp_path):
    cache = SttRecoveryCache(
        _policy(max_total_bytes=4),
        hermes_home=tmp_path,
    )

    with pytest.raises(ValueError):
        cache.stage_audio(b"abc", suffix="../../wav", mime_type="audio/wav")
    assert cache.stage_audio(b"abcde", suffix=".wav", mime_type="audio/wav") is None
    assert not cache.root.exists()


def test_concurrent_stage_and_prune_publish_only_complete_unique_records(tmp_path):
    cache = SttRecoveryCache(
        _policy(max_entries=10, max_total_bytes=50),
        hermes_home=tmp_path,
    )

    def stage(index: int):
        record = cache.stage_audio(
            bytes([index]) * 5,
            suffix=".wav",
            mime_type="audio/wav",
        )
        if record is not None:
            return cache.mark_failed_attempt(
                record.recovery_id,
                attempts=record.attempts,
                failure_code="provider_error",
            )
        return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        published = [record for record in pool.map(stage, range(20)) if record]

    records = cache.list_records()
    assert 1 <= len(records) <= 10
    assert len({record.recovery_id for record in published}) == len(published)
    assert len({record.recovery_id for record in records}) == len(records)
    assert sum(record.byte_size for record in records) <= 50
    for record in records:
        assert record.status == "failed"
        assert record.audio_path.is_file()
        assert (record.directory / "manifest.json").is_file()
    assert not any(child.name.startswith(".tmp-") for child in cache.root.iterdir())
