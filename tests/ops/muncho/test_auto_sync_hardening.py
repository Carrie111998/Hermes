from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


MODULE_PATH = (
    Path(__file__).parents[3]
    / "ops"
    / "muncho"
    / "runtime"
    / "auto_sync_hardening.py"
)
SPEC = importlib.util.spec_from_file_location("auto_sync_hardening", MODULE_PATH)
assert SPEC and SPEC.loader
hardening = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hardening)

OLD = "1" * 40
CURRENT = "2" * 40
HEAD = "3" * 40


def test_newer_upstream_keeps_exact_review_candidate_stable():
    assert hardening.classify_stale_candidate(
        head_already_in_fork_main=False,
        upstream_snapshot_sha=OLD,
        upstream_snapshot_in_fork_merge_base=False,
        current_upstream_sha=CURRENT,
        current_upstream_contains_snapshot=True,
    ) is None


def test_unrelated_upstream_also_keeps_exact_review_candidate_stable():
    assert hardening.classify_stale_candidate(
        head_already_in_fork_main=False,
        upstream_snapshot_sha=OLD,
        upstream_snapshot_in_fork_merge_base=False,
        current_upstream_sha=CURRENT,
        current_upstream_contains_snapshot=False,
    ) is None


def test_existing_stale_reasons_keep_precedence():
    assert hardening.classify_stale_candidate(
        head_already_in_fork_main=True,
        upstream_snapshot_sha=OLD,
        upstream_snapshot_in_fork_merge_base=True,
        current_upstream_sha=CURRENT,
        current_upstream_contains_snapshot=True,
    ) == "head_already_in_fork_main"


def _prepared_manifest() -> dict[str, object]:
    return hardening.build_prepared_candidate_manifest(
        candidate_id="9" * 64,
        fork_repository="lomliev/hermes-agent",
        upstream_repository="NousResearch/hermes-agent",
        base_ref="main",
        upstream_ref="main",
        branch="opaque exact branch",
        head_sha=HEAD,
        base_sha=OLD,
        upstream_sha=CURRENT,
        created_at_utc="2026-07-30T09:00:00Z",
    )


def test_candidate_manifest_two_phase_is_exact_digest_bound(tmp_path):
    path = tmp_path / "private" / "candidate.json"
    prepared = _prepared_manifest()
    hardening.write_candidate_manifest(path, prepared)

    assert hardening.load_candidate_manifest(path) == prepared
    assert prepared["phase"] == "prepared"
    assert prepared["pr_number"] is None

    published = hardening.publish_candidate_manifest(prepared, pr_number=481)
    hardening.write_candidate_manifest(path, published)

    assert hardening.load_candidate_manifest(path) == published
    assert published["phase"] == "published"
    assert published["pr_number"] == 481
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_candidate_manifest_rejects_tampering_and_phase_repair(tmp_path):
    path = tmp_path / "candidate.json"
    manifest = hardening.publish_candidate_manifest(
        _prepared_manifest(), pr_number=481
    )

    tampered = dict(manifest)
    tampered["branch"] = f"{manifest['branch']} "
    with pytest.raises(
        hardening.CandidateManifestError,
        match="candidate_manifest_digest_mismatch",
    ):
        hardening.write_candidate_manifest(path, tampered)

    repaired = dict(manifest)
    repaired["phase"] = "prepared"
    with pytest.raises(
        hardening.CandidateManifestError,
        match="prepared_pr_number_must_be_null",
    ):
        hardening.validate_candidate_manifest(repaired)


def test_private_state_parent_symlink_or_broad_mode_fails_closed(tmp_path):
    broad = tmp_path / "broad"
    broad.mkdir(mode=0o755)
    broad.chmod(0o755)
    with pytest.raises(RuntimeError, match="invalid private state directory"):
        hardening.write_candidate_manifest(
            broad / "candidate.json",
            _prepared_manifest(),
        )
    assert broad.stat().st_mode & 0o777 == 0o755

    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(RuntimeError, match="invalid private state directory"):
        hardening.write_candidate_manifest(
            linked / "candidate.json",
            _prepared_manifest(),
        )


def test_private_pointer_and_lock_require_exact_owner_and_mode(
    tmp_path,
    monkeypatch,
):
    pointer = tmp_path / "private" / "candidate.json"
    hardening.write_candidate_manifest(pointer, _prepared_manifest())
    pointer.chmod(0o644)
    with pytest.raises(
        hardening.CandidateManifestError,
        match="candidate_manifest_file_invalid",
    ):
        hardening.load_candidate_manifest(pointer)

    pointer.chmod(0o600)
    monkeypatch.setattr(hardening.os, "geteuid", lambda: pointer.stat().st_uid + 1)
    with pytest.raises(
        hardening.CandidateManifestError,
        match="candidate_manifest_file_invalid",
    ):
        hardening.load_candidate_manifest(pointer)

    monkeypatch.undo()
    lock = pointer.with_name(f".{pointer.name}.lock")
    lock.write_text("", encoding="utf-8")
    lock.chmod(0o644)
    with pytest.raises(RuntimeError, match="invalid blocker state lock"):
        with hardening.candidate_manifest_lock(pointer):
            raise AssertionError("invalid lock was accepted")
    assert lock.stat().st_mode & 0o777 == 0o644


def test_prepared_manifest_is_persistent_fail_closed_state(tmp_path):
    path = tmp_path / "candidate.json"
    hardening.write_candidate_manifest(path, _prepared_manifest())
    hardening.clear_candidate_manifest(path)
    assert not path.exists()


def test_candidate_ledger_recovers_missing_pointer_across_both_phases(tmp_path):
    pointer = tmp_path / "private" / "candidate.json"
    prepared = _prepared_manifest()
    hardening.append_candidate_manifest(pointer, prepared)
    pointer.unlink()

    assert hardening.recover_candidate_manifest(pointer) == prepared

    published = hardening.publish_candidate_manifest(prepared, pr_number=481)
    hardening.append_candidate_manifest(pointer, published)
    pointer.unlink()

    assert hardening.recover_candidate_manifest(pointer) == published
    ledger = hardening.candidate_ledger_path(pointer)
    assert ledger.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in ledger.iterdir())


def test_candidate_ledger_advances_exact_prepared_pointer_after_publish_crash(
    tmp_path,
):
    pointer = tmp_path / "private" / "candidate.json"
    prepared = _prepared_manifest()
    published = hardening.publish_candidate_manifest(prepared, pr_number=481)
    hardening.append_candidate_manifest(pointer, prepared)
    hardening.append_candidate_manifest(pointer, published)
    # Recreate the exact crash state: published ledger event is durable, but
    # replacement of the mutable pointer did not happen.
    hardening.write_candidate_manifest(pointer, prepared)

    assert hardening.recover_candidate_manifest(pointer) == published
    assert hardening.load_candidate_manifest(pointer) == published


def test_candidate_ledger_terminal_receipt_retires_exact_published_state(
    tmp_path,
):
    pointer = tmp_path / "private" / "candidate.json"
    prepared = _prepared_manifest()
    published = hardening.publish_candidate_manifest(prepared, pr_number=481)
    hardening.append_candidate_manifest(pointer, prepared)
    hardening.append_candidate_manifest(pointer, published)

    receipt = hardening.append_candidate_terminal_receipt(
        pointer,
        published,
        observed_base_sha="4" * 40,
        created_at_utc="2026-07-30T12:00:00Z",
    )

    assert receipt["terminal_state"] == "MERGED"
    assert not pointer.exists()
    assert hardening.recover_candidate_manifest(pointer) is None


def test_candidate_ledger_tamper_and_ambiguous_active_state_fail_closed(tmp_path):
    pointer = tmp_path / "private" / "candidate.json"
    first = _prepared_manifest()
    hardening.append_candidate_manifest(pointer, first)
    ledger = hardening.candidate_ledger_path(pointer)
    entry = next(ledger.iterdir())
    tampered = json.loads(entry.read_text(encoding="utf-8"))
    tampered["head_sha"] = "8" * 40
    entry.write_text(json.dumps(tampered), encoding="utf-8")
    entry.chmod(0o600)
    with pytest.raises(
        hardening.CandidateManifestError,
        match="digest_mismatch",
    ):
        hardening.recover_candidate_manifest(pointer)

    entry.unlink()
    hardening.clear_candidate_manifest(pointer)
    second = hardening.build_prepared_candidate_manifest(
        candidate_id="7" * 64,
        fork_repository="lomliev/hermes-agent",
        upstream_repository="NousResearch/hermes-agent",
        base_ref="main",
        upstream_ref="main",
        branch="second exact branch",
        head_sha="6" * 40,
        base_sha=OLD,
        upstream_sha=CURRENT,
        created_at_utc="2026-07-30T10:00:00Z",
    )
    hardening.append_candidate_manifest(pointer, first)
    hardening.append_candidate_manifest(pointer, second)
    with pytest.raises(
        hardening.CandidateManifestError,
        match="multiple_active",
    ):
        hardening.recover_candidate_manifest(pointer)


def test_blocker_fingerprint_is_order_independent():
    first = hardening.blocker_fingerprint(
        status="blocked_candidate_identity_state",
        pr_number=91,
        head_sha=HEAD,
        blockers=["checks_failed", "merge_state_UNSTABLE"],
        failed_checks=[
            {"name": "slice 5", "conclusion": "failure"},
            {"name": "required", "conclusion": "failure"},
        ],
    )
    second = hardening.blocker_fingerprint(
        status="blocked_candidate_identity_state",
        pr_number=91,
        head_sha=HEAD,
        blockers=["merge_state_UNSTABLE", "checks_failed"],
        failed_checks=[
            {"name": "required", "conclusion": "failure"},
            {"name": "slice 5", "conclusion": "failure"},
        ],
    )
    assert first == second

    case_lookalike = hardening.blocker_fingerprint(
        status="blocked_candidate_identity_state",
        pr_number=91,
        head_sha=HEAD,
        blockers=["checks_failed", "merge_state_UNSTABLE"],
        failed_checks=[
            {"name": "slice 5", "conclusion": "FAILURE"},
            {"name": "required", "conclusion": "FAILURE"},
        ],
    )
    assert case_lookalike != first


def test_unchanged_blocker_is_suppressed_until_repeat_window(tmp_path):
    state = tmp_path / "private" / "blocker.json"
    now = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)
    fingerprint = "a" * 64

    first = hardening.decide_blocker_delivery(
        state,
        fingerprint=fingerprint,
        now=now,
        previous_delivery_status="none",
    )
    selected_state = json.loads(state.read_text())
    repeated = hardening.decide_blocker_delivery(
        state,
        fingerprint=fingerprint,
        now=now + timedelta(hours=3),
        observed_previous_run_at="2026-07-11T15:00:00+00:00",
        previous_delivery_status="confirmed",
    )
    reminder = hardening.decide_blocker_delivery(
        state,
        fingerprint=fingerprint,
        now=now + timedelta(hours=27),
        observed_previous_run_at="2026-07-11T15:00:00+00:00",
        previous_delivery_status="confirmed",
    )

    assert first["emit"] is True
    assert selected_state["last_delivery_confirmed_at"] is None
    assert selected_state["pending_delivery"] is not None
    assert repeated["emit"] is False
    assert repeated["reason"] == "unchanged_delivered_blocker_suppressed"
    assert repeated["prior_delivery_reconciled"] is True
    assert repeated["delivery_confirmed_at"] is not None
    assert repeated["pending_delivery"] is False
    assert reminder["emit"] is True
    assert state.stat().st_mode & 0o777 == 0o600
    assert state.parent.stat().st_mode & 0o777 == 0o700


def test_failed_or_unconfirmed_delivery_retries_without_false_receipt(tmp_path):
    state = tmp_path / "private" / "blocker.json"
    now = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)
    fingerprint = "c" * 64

    first = hardening.decide_blocker_delivery(
        state,
        fingerprint=fingerprint,
        now=now,
        previous_delivery_status="none",
    )
    assert first["emit"] is True
    assert first["delivery_confirmed_at"] is None

    failed = hardening.decide_blocker_delivery(
        state,
        fingerprint=fingerprint,
        now=now + timedelta(hours=3),
        observed_previous_run_at="2026-07-11T15:00:00+00:00",
        previous_delivery_status="failed",
    )
    assert failed["emit"] is True
    assert failed["reason"] == "previous_delivery_failed_retry"
    assert failed["delivery_confirmed_at"] is None
    assert failed["pending_delivery"] is True

    unconfirmed = hardening.decide_blocker_delivery(
        state,
        fingerprint=fingerprint,
        now=now + timedelta(hours=6),
        observed_previous_run_at="2026-07-11T15:00:00+00:00",
        previous_delivery_status="confirmed",
    )
    assert unconfirmed["emit"] is True
    assert unconfirmed["reason"] == "previous_delivery_unconfirmed_retry"
    assert unconfirmed["delivery_confirmed_at"] is None


def test_changed_or_cleared_blocker_emits_again(tmp_path):
    state = tmp_path / "blocker.json"
    now = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)
    hardening.decide_blocker_delivery(state, fingerprint="a" * 64, now=now)

    changed = hardening.decide_blocker_delivery(
        state, fingerprint="b" * 64, now=now + timedelta(hours=3)
    )
    hardening.clear_blocker_delivery_state(state, now=now + timedelta(hours=4))
    recurrence = hardening.decide_blocker_delivery(
        state, fingerprint="b" * 64, now=now + timedelta(hours=5)
    )

    assert changed["emit"] is True
    assert recurrence["emit"] is True
    assert json.loads(state.read_text())["active"] is True


def test_malformed_state_is_never_treated_as_delivery_receipt(tmp_path):
    state = tmp_path / "blocker.json"
    state.write_text(
        json.dumps(
            {
                "schema": hardening.STATE_SCHEMA,
                "active": True,
                "fingerprint": "d" * 64,
                "last_seen_at": "2026-07-11T12:00:00Z",
                "last_selected_for_delivery_at": "2026-07-11T12:00:00Z",
                "last_delivery_confirmed_at": "not-a-timestamp",
                "pending_delivery": None,
                "suppressed_runs": 0,
            }
        )
    )

    decision = hardening.decide_blocker_delivery(
        state,
        fingerprint="d" * 64,
        now=datetime(2026, 7, 11, 13, tzinfo=timezone.utc),
        observed_previous_run_at="2026-07-11T12:00:00Z",
        previous_delivery_status="confirmed",
    )

    assert decision["emit"] is True
    assert decision["reason"] == "new_or_changed_blocker"
    assert decision["delivery_confirmed_at"] is None
