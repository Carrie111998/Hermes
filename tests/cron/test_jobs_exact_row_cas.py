"""Hermetic contract tests for exact full-row scheduler containment CAS APIs."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
from datetime import datetime, timezone

import pytest

from cron import jobs


def _digest(rows):
    raw = json.dumps(
        rows,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _row(job_id, name, *, enabled=True, state="scheduled", **extra):
    return {
        "id": job_id,
        "name": name,
        "enabled": enabled,
        "state": state,
        "schedule": {"kind": "cron", "expr": "0 9 * * *"},
        "prompt": f"run {name}",
        "opaque": {"preserve": [name, 1]},
        **extra,
    }


def _sorted(rows):
    return sorted(copy.deepcopy(rows), key=lambda row: row["name"])


def _paused(row, *, at="2026-08-21T12:00:00+00:00", reason="containment"):
    result = copy.deepcopy(row)
    result.update(
        {
            "enabled": False,
            "state": "paused",
            # Parent protocol validator requires this canonical compatibility
            # marker in addition to the four lifecycle fields named by the API.
            "paused": True,
            "paused_at": at,
            "paused_reason": reason,
        }
    )
    return result


@pytest.fixture
def store(tmp_path):
    with jobs.use_cron_store(tmp_path):
        yield tmp_path


@pytest.fixture
def dispatch_barrier(tmp_path):
    from jobflow_dispatch.quarantine_control import QuarantineControlStore

    control = QuarantineControlStore(tmp_path / "control.db")
    with control.acquire_dispatch_barrier(reason="test exact scheduler CAS") as barrier:
        yield barrier


def _seed(rows):
    jobs.save_jobs(copy.deepcopy(rows))


def test_raw_pause_and_restore_refuse_without_retained_barrier(store):
    alpha = _row("a1", "alpha")
    paused = _paused(alpha)
    _seed([alpha])

    with pytest.raises(RuntimeError, match="DispatchBarrier"):
        jobs.pause_jobs_cas(
            ["alpha"],
            _digest([alpha]),
            reason="containment",
            dispatch_barrier=None,
            caller="test:containment",
        )

    _seed([paused])
    with pytest.raises(RuntimeError, match="DispatchBarrier"):
        jobs.restore_jobs_cas(
            expected_paused_rows=[paused],
            target_rows=[alpha],
            dependency_order=["alpha"],
            dispatch_barrier=None,
            caller="test:containment",
        )


def test_snapshot_jobs_by_name_returns_sorted_exact_durable_deep_copies(store):
    alpha = _row("a1", "alpha", unicode="São Paulo", nullable=None)
    zeta = _row("z9", "zeta", legacy_only={"x": True})
    unrelated = _row("u7", "unrelated")
    _seed([zeta, unrelated, alpha])

    observed = jobs.snapshot_jobs_by_name(("zeta", "alpha"))

    assert observed == [alpha, zeta]
    observed[0]["opaque"]["preserve"].append("caller mutation")
    assert jobs.snapshot_jobs_by_name(("alpha",))[0] == alpha


@pytest.mark.parametrize(
    ("requested", "stored"),
    [
        (("alpha", "alpha"), [_row("a1", "alpha")]),
        (("missing",), [_row("a1", "alpha")]),
        (("alpha",), [_row("a1", "alpha"), _row("a2", "alpha")]),
    ],
)
def test_snapshot_jobs_by_name_rejects_nonunique_missing_or_ambiguous_scope(
    store, requested, stored
):
    _seed(stored)
    with pytest.raises(ValueError):
        jobs.snapshot_jobs_by_name(requested)


def test_pause_jobs_cas_is_one_locked_save_and_returns_exact_digest_proof(
    store, dispatch_barrier, monkeypatch
):
    alpha = _row("a1", "alpha", paused_at=None, paused_reason=None)
    matcher = _row("m1", "jobflow-matcher", paused_at=None, paused_reason=None)
    disabled = _row(
        "d1", "disabled", enabled=False, state="disabled",
        paused_at="old", paused_reason="operator",
    )
    unrelated = _row("u1", "unrelated")
    _seed([matcher, unrelated, disabled, alpha])
    names = ["jobflow-matcher", "disabled", "alpha"]
    before = _sorted([matcher, disabled, alpha])

    enters = 0
    saves = 0
    real_lock = jobs._jobs_lock
    real_save = jobs._save_jobs_unlocked

    @contextlib.contextmanager
    def counted_lock():
        nonlocal enters
        enters += 1
        with real_lock():
            yield

    def counted_save(rows):
        nonlocal saves
        saves += 1
        real_save(rows)

    monkeypatch.setattr(jobs, "_jobs_lock", counted_lock)
    monkeypatch.setattr(jobs, "_save_jobs_unlocked", counted_save)
    monkeypatch.setattr(
        jobs, "_hermes_now", lambda: datetime(2026, 8, 21, 12, tzinfo=timezone.utc)
    )

    result = jobs.pause_jobs_cas(names, _digest(before), reason="containment", dispatch_barrier=dispatch_barrier, caller="test:containment")

    expected_after = _sorted([_paused(alpha), disabled, _paused(matcher)])
    assert enters == 1
    assert saves == 1
    assert result["schema_version"] == 1
    assert result["complete"] is True
    assert result["source"] == "cron.jobs"
    assert result["pause_reason"] == "containment"
    assert result["before_rows"] == before
    assert result["after_rows"] == expected_after
    assert result["changed_job_ids"] == ["a1", "m1"]
    assert isinstance(result["control_transaction_id"], str)
    assert result["control_transaction_id"]
    assert result["digest_proof"] == {
        "algorithm": "sha256",
        "expected_before": _digest(before),
        "observed_before": _digest(before),
        "after": _digest(expected_after),
        "durable_readback": _digest(expected_after),
    }
    assert jobs.snapshot_jobs_by_name(tuple(names)) == expected_after
    assert jobs.snapshot_jobs_by_name(("unrelated",)) == [unrelated]


def test_pause_jobs_cas_refuses_digest_or_resolution_drift_without_writing(
    store, dispatch_barrier, monkeypatch
):
    alpha = _row("a1", "alpha")
    _seed([alpha])
    saves = 0
    real_save = jobs._save_jobs_unlocked

    def counted_save(rows):
        nonlocal saves
        saves += 1
        real_save(rows)

    monkeypatch.setattr(jobs, "_save_jobs_unlocked", counted_save)
    with pytest.raises(ValueError, match="digest"):
        jobs.pause_jobs_cas(["alpha"], "0" * 64, reason="containment", dispatch_barrier=dispatch_barrier, caller="test:containment")
    with pytest.raises(ValueError):
        jobs.pause_jobs_cas(["alpha", "alpha"], _digest([alpha]), reason="containment", dispatch_barrier=dispatch_barrier, caller="test:containment")
    with pytest.raises(ValueError):
        jobs.pause_jobs_cas(["missing"], _digest([alpha]), reason="containment", dispatch_barrier=dispatch_barrier, caller="test:containment")
    assert saves == 0
    assert jobs.snapshot_jobs_by_name(("alpha",)) == [alpha]


def test_restore_jobs_cas_exactly_restores_changed_rows_in_dependency_order(
    store, dispatch_barrier, monkeypatch
):
    alpha = _row("a1", "alpha", paused_at=None, paused_reason=None)
    matcher = _row("m1", "jobflow-matcher", paused_at=None, paused_reason=None)
    disabled = _row("d1", "disabled", enabled=False, state="disabled")
    unrelated = _row("u1", "unrelated")
    expected_paused = _sorted([_paused(alpha), disabled, _paused(matcher)])
    _seed([expected_paused[2], unrelated, expected_paused[0], expected_paused[1]])

    enters = 0
    saves = 0
    real_lock = jobs._jobs_lock
    real_save = jobs._save_jobs_unlocked

    @contextlib.contextmanager
    def counted_lock():
        nonlocal enters
        enters += 1
        with real_lock():
            yield

    def counted_save(rows):
        nonlocal saves
        saves += 1
        real_save(rows)

    monkeypatch.setattr(jobs, "_jobs_lock", counted_lock)
    monkeypatch.setattr(jobs, "_save_jobs_unlocked", counted_save)

    result = jobs.restore_jobs_cas(
        expected_paused_rows=expected_paused,
        target_rows=[matcher, alpha],
        dependency_order=["alpha", "jobflow-matcher"],
        dispatch_barrier=dispatch_barrier,
        caller="test:containment",
    )

    expected_scope = _sorted([alpha, disabled, matcher])
    assert enters == 1
    assert saves == 1
    assert result["schema_version"] == 1
    assert result["complete"] is True
    assert result["source"] == "cron.jobs"
    assert result["restored_job_ids"] == ["a1", "m1"]
    assert result["before_rows"] == expected_paused
    assert result["after_rows"] == [alpha, matcher]
    assert result["durable_rows"] == expected_scope
    assert result["digest_proof"] == {
        "algorithm": "sha256",
        "expected_paused": _digest(expected_paused),
        "observed_before": _digest(expected_paused),
        "target": _digest(_sorted([alpha, matcher])),
        "durable_readback": _digest(expected_scope),
    }
    assert jobs.snapshot_jobs_by_name(
        ("alpha", "disabled", "jobflow-matcher")
    ) == expected_scope
    assert jobs.snapshot_jobs_by_name(("unrelated",)) == [unrelated]


@pytest.mark.parametrize(
    "mutation",
    [
        "current_drift", "wrong_id", "unauthorized_target_difference",
        "missing_dependency", "matcher_not_last", "duplicate_dependency",
    ],
)
def test_restore_jobs_cas_refuses_nonexact_cas_scope_or_order_without_writing(
    store, dispatch_barrier, monkeypatch, mutation
):
    alpha = _row("a1", "alpha", paused_at=None, paused_reason=None)
    matcher = _row("m1", "jobflow-matcher", paused_at=None, paused_reason=None)
    expected_paused = _sorted([_paused(alpha), _paused(matcher)])
    stored = copy.deepcopy(expected_paused)
    targets = [copy.deepcopy(alpha), copy.deepcopy(matcher)]
    order = ["alpha", "jobflow-matcher"]

    if mutation == "current_drift":
        stored[0]["last_status"] = "drift"
    elif mutation == "wrong_id":
        targets[0]["id"] = "other"
    elif mutation == "unauthorized_target_difference":
        targets[0]["prompt"] = "not the pre-containment full row"
    elif mutation == "missing_dependency":
        order = ["alpha"]
    elif mutation == "matcher_not_last":
        order = ["jobflow-matcher", "alpha"]
    elif mutation == "duplicate_dependency":
        order = ["alpha", "alpha", "jobflow-matcher"]

    _seed(stored)
    saves = 0
    real_save = jobs._save_jobs_unlocked

    def counted_save(rows):
        nonlocal saves
        saves += 1
        real_save(rows)

    monkeypatch.setattr(jobs, "_save_jobs_unlocked", counted_save)
    with pytest.raises(ValueError):
        jobs.restore_jobs_cas(
            expected_paused_rows=expected_paused,
            target_rows=targets,
            dependency_order=order,
            dispatch_barrier=dispatch_barrier,
            caller="test:containment",
        )
    assert saves == 0
    assert jobs.snapshot_jobs_by_name(("alpha", "jobflow-matcher")) == _sorted(stored)
