from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import json
import sqlite3
import threading

import pytest

from hermes_state import SessionDB
from session_bridge.mirror import (
    BatchProgress,
    DiscoveryMode,
    EligibilityContext,
    MirrorCandidate,
    MirrorPolicy,
    claim_due_mirror_jobs,
    classify_mirror_eligibility,
    eligible_mirror_candidates,
    enqueue_mirror_job,
    load_continuous_watermark,
    mirror_idempotency_key,
    persist_continuous_watermark,
    provider_concurrency_limit,
    record_mirror_failure,
    retry_delay_seconds,
    select_creation_batch,
    should_halt_batch,
)
from session_bridge.models import (
    MirrorJobState,
    OriginKind,
    ProjectedMessage,
    Provider,
    Relation,
    SessionLink,
    SessionProjection,
    canonical_session_id,
)
from session_bridge.store import SessionBridgeStore


NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc).timestamp()
DAY = 24 * 60 * 60


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    yield database
    database.close()


def _message(
    *,
    content: str = "meaningful first message",
    timestamp: float = NOW - 10.0,
    role: str = "user",
) -> ProjectedMessage:
    return ProjectedMessage(
        native_event_id=f"event-{role}-{timestamp}-{content}",
        ordinal=0,
        role=role,
        content=content,
        timestamp=timestamp,
    )


def _projection(
    *,
    provider: Provider = Provider.CLAUDE,
    native_id: str = "source-1",
    started_at: float | None = None,
    last_active: float = NOW - 10.0,
    messages: tuple[ProjectedMessage, ...] | None = None,
    origin_kind: OriginKind = OriginKind.NATIVE,
    origin_bridge_id: str | None = None,
) -> SessionProjection:
    effective_started_at = last_active - 50.0 if started_at is None else started_at
    return SessionProjection(
        provider=provider,
        native_id=native_id,
        title="A real session",
        cwd="C:/workspace/project",
        started_at=effective_started_at,
        last_active=last_active,
        messages=(
            messages
            if messages is not None
            else (_message(timestamp=min(NOW - 10.0, last_active)),)
        ),
        native_cursor="cursor-1",
        native_hash="hash-1",
        origin_kind=origin_kind,
        origin_bridge_id=origin_bridge_id,
    )


def _context(
    *,
    now: float = NOW,
    mode: DiscoveryMode = DiscoveryMode.INITIAL_BACKFILL,
    watermark: float | None = None,
    mappings: frozenset[tuple[str, Provider]] = frozenset(),
    policy: MirrorPolicy = MirrorPolicy(),
) -> EligibilityContext:
    return EligibilityContext(
        now=now,
        discovery_mode=mode,
        continuous_watermark=watermark,
        existing_target_mappings=mappings,
        policy=policy,
    )


def _candidate(
    projection: SessionProjection,
    *,
    policy: MirrorPolicy,
) -> MirrorCandidate:
    candidates = eligible_mirror_candidates((projection,), _context(policy=policy))
    assert len(candidates) == 1
    return candidates[0]


def _job_rows(db: SessionDB) -> list[dict[str, object]]:
    with db._lock:
        connection = db._conn
        assert connection is not None
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM session_mirror_jobs ORDER BY id"
            ).fetchall()
        ]


def _authority_rows(db: SessionDB) -> list[dict[str, object]]:
    with db._lock:
        connection = db._conn
        assert connection is not None
        return [
            dict(row)
            for row in connection.execute(
                """SELECT * FROM session_bridge_state
                   WHERE key LIKE 'session-bridge:mirror-authority:%'
                   ORDER BY key"""
            ).fetchall()
        ]


def test_mirror_policy_has_exact_safe_defaults_and_is_frozen():
    policy = MirrorPolicy()

    assert policy == MirrorPolicy(
        generation=1,
        automatic_creation=False,
        backfill_days=30,
        debounce_seconds=5.0,
        claude_concurrency=1,
        codex_concurrency=2,
        creates_per_minute=6,
        max_attempts=5,
        stop_after_attempts=20,
        stop_error_rate=0.25,
    )
    with pytest.raises(FrozenInstanceError):
        policy.automatic_creation = True  # type: ignore[misc]


def test_exact_thirty_day_backfill_boundary_is_inclusive():
    projection = _projection(last_active=NOW - 30 * DAY)

    eligibility = classify_mirror_eligibility(projection, _context())

    assert eligibility.eligible is True
    assert eligibility.target_provider is Provider.CODEX
    assert eligibility.reason == "eligible"


def test_one_second_older_than_backfill_boundary_is_too_old():
    projection = _projection(last_active=NOW - 30 * DAY - 1)

    eligibility = classify_mirror_eligibility(projection, _context())

    assert eligibility.eligible is False
    assert eligibility.reason == "too_old"


@pytest.mark.parametrize(
    ("source", "target"),
    ((Provider.CLAUDE, Provider.CODEX), (Provider.CODEX, Provider.CLAUDE)),
)
def test_provider_is_inverted(source: Provider, target: Provider):
    eligibility = classify_mirror_eligibility(_projection(provider=source), _context())

    assert eligibility.target_provider is target


def test_empty_and_non_user_only_sessions_are_debounced():
    empty = classify_mirror_eligibility(_projection(messages=()), _context())
    assistant_only = classify_mirror_eligibility(
        _projection(messages=(_message(role="assistant"),)), _context()
    )
    whitespace_user = classify_mirror_eligibility(
        _projection(messages=(_message(content="   "),)), _context()
    )

    assert (empty.eligible, empty.reason) == (False, "empty")
    assert (assistant_only.eligible, assistant_only.reason) == (False, "empty")
    assert (whitespace_user.eligible, whitespace_user.reason) == (False, "empty")


def test_meaningful_first_message_must_be_stable_for_debounce_period():
    still_debouncing = classify_mirror_eligibility(
        _projection(
            last_active=NOW,
            messages=(_message(timestamp=NOW - 4.999),),
        ),
        _context(),
    )
    stable = classify_mirror_eligibility(
        _projection(
            last_active=NOW,
            messages=(_message(timestamp=NOW - 5.0),),
        ),
        _context(),
    )

    assert (still_debouncing.eligible, still_debouncing.reason) == (False, "empty")
    assert (stable.eligible, stable.reason) == (True, "eligible")


@pytest.mark.parametrize(
    "projection",
    (
        replace(_projection(), started_at=True),
        replace(_projection(), started_at=float("nan")),
        replace(_projection(), last_active=False),
        replace(_projection(), last_active=float("inf")),
        _projection(started_at=NOW - 5, last_active=NOW - 10),
        _projection(started_at=NOW + 1, last_active=NOW + 2),
        _projection(last_active=NOW + 1),
        _projection(messages=(_message(timestamp=True),)),
        _projection(
            started_at=NOW - 20,
            last_active=NOW - 10,
            messages=(_message(timestamp=NOW - 21),),
        ),
        _projection(
            started_at=NOW - 20,
            last_active=NOW - 10,
            messages=(_message(timestamp=NOW - 9),),
        ),
    ),
)
def test_invalid_projection_timeline_is_unstable_identity(projection):
    eligibility = classify_mirror_eligibility(projection, _context())

    assert (eligibility.eligible, eligibility.reason) == (
        False,
        "unstable_identity",
    )


def test_pre_start_message_cannot_bypass_debounce_for_just_created_session():
    projection = _projection(
        started_at=NOW - 1,
        last_active=NOW,
        messages=(_message(timestamp=NOW - 100),),
    )

    eligibility = classify_mirror_eligibility(projection, _context())

    assert (eligibility.eligible, eligibility.reason) == (
        False,
        "unstable_identity",
    )


@pytest.mark.parametrize("native_id", ("", "   "))
def test_empty_native_identity_is_unstable(native_id: str):
    eligibility = classify_mirror_eligibility(
        _projection(native_id=native_id), _context()
    )

    assert (eligibility.eligible, eligibility.reason) == (
        False,
        "unstable_identity",
    )


@pytest.mark.parametrize(
    "origin_kind",
    (OriginKind.BRIDGE_PLACEHOLDER, OriginKind.BRIDGE_CONTINUATION),
)
def test_bridge_origin_is_not_automatically_mirrored_back(origin_kind: OriginKind):
    projection = _projection(
        provider=Provider.CODEX,
        origin_kind=origin_kind,
        origin_bridge_id="bridge-1",
    )

    eligibility = classify_mirror_eligibility(projection, _context())

    assert eligibility.target_provider is Provider.CLAUDE
    assert (eligibility.eligible, eligibility.reason) == (False, "bridge_origin")


def test_exact_existing_target_mapping_suppresses_only_that_source_target_pair():
    projection = _projection(provider=Provider.CLAUDE, native_id="exact-source")
    source_id = canonical_session_id(Provider.CLAUDE, "exact-source")

    exact = classify_mirror_eligibility(
        projection,
        _context(mappings=frozenset({(source_id, Provider.CODEX)})),
    )
    unrelated = classify_mirror_eligibility(
        projection,
        _context(
            mappings=frozenset({
                (source_id, Provider.CLAUDE),
                ("claude:other", Provider.CODEX),
            })
        ),
    )

    assert (exact.eligible, exact.reason) == (False, "already_mapped")
    assert (unrelated.eligible, unrelated.reason) == (True, "eligible")


def test_continuous_discovery_requires_session_creation_after_watermark():
    watermark = NOW - 120.0
    at_watermark = classify_mirror_eligibility(
        _projection(started_at=watermark, last_active=NOW - 10),
        _context(mode=DiscoveryMode.CONTINUOUS, watermark=watermark),
    )
    after_watermark = classify_mirror_eligibility(
        _projection(started_at=watermark + 0.001, last_active=NOW - 10),
        _context(mode=DiscoveryMode.CONTINUOUS, watermark=watermark),
    )

    assert (at_watermark.eligible, at_watermark.reason) == (
        False,
        "before_watermark",
    )
    assert (after_watermark.eligible, after_watermark.reason) == (True, "eligible")


def test_missing_continuous_watermark_fails_closed():
    eligibility = classify_mirror_eligibility(
        _projection(),
        _context(mode=DiscoveryMode.CONTINUOUS, watermark=None),
    )

    assert (eligibility.eligible, eligibility.reason) == (
        False,
        "before_watermark",
    )


def test_continuous_watermark_is_durable_and_monotonic_across_store_restart(db):
    first = SessionBridgeStore(db, clock=lambda: NOW)
    persist_continuous_watermark(first, NOW - 120)
    persist_continuous_watermark(first, NOW - 60)

    restarted = SessionBridgeStore(db, clock=lambda: NOW + 1)
    persisted = load_continuous_watermark(restarted)
    eligibility = classify_mirror_eligibility(
        _projection(started_at=NOW - 90, last_active=NOW - 10),
        _context(mode=DiscoveryMode.CONTINUOUS, watermark=persisted),
    )

    assert persisted == NOW - 60
    assert (eligibility.eligible, eligibility.reason) == (
        False,
        "before_watermark",
    )
    with pytest.raises(ValueError, match="cannot move backwards"):
        persist_continuous_watermark(restarted, NOW - 61)


def test_concurrent_watermark_writers_cannot_commit_a_late_lower_value(
    tmp_path, monkeypatch
):
    path = tmp_path / "state.db"
    seed_db = SessionDB(path)
    persist_continuous_watermark(SessionBridgeStore(seed_db, clock=lambda: 1.0), 100.0)
    seed_db.close()

    lower_db = SessionDB(path)
    higher_db = SessionDB(path)
    lower_store = SessionBridgeStore(lower_db, clock=lambda: 2.0)
    higher_store = SessionBridgeStore(higher_db, clock=lambda: 3.0)
    lower_entered_write = threading.Event()
    higher_finished_write = threading.Event()
    original_lower_write = lower_db._execute_write
    original_higher_write = higher_db._execute_write

    def delay_lower_write(operation):
        lower_entered_write.set()
        assert higher_finished_write.wait(timeout=5.0)
        return original_lower_write(operation)

    def signal_higher_write(operation):
        try:
            return original_higher_write(operation)
        finally:
            higher_finished_write.set()

    monkeypatch.setattr(lower_db, "_execute_write", delay_lower_write)
    monkeypatch.setattr(higher_db, "_execute_write", signal_higher_write)
    errors: dict[str, BaseException] = {}

    def write(name: str, store: SessionBridgeStore, value: float) -> None:
        try:
            persist_continuous_watermark(store, value)
        except BaseException as exc:  # pragma: no branch - captured for the assertion
            errors[name] = exc

    lower = threading.Thread(target=write, args=("lower", lower_store, 150.0))
    higher = threading.Thread(target=write, args=("higher", higher_store, 200.0))
    lower.start()
    assert lower_entered_write.wait(timeout=5.0)
    higher.start()
    lower.join(timeout=5.0)
    higher.join(timeout=5.0)

    try:
        assert not lower.is_alive()
        assert not higher.is_alive()
        assert "higher" not in errors
        assert isinstance(errors.get("lower"), ValueError)
        assert "cannot move backwards" in str(errors["lower"])
        restarted = SessionBridgeStore(SessionDB(path), clock=lambda: 4.0)
        try:
            assert load_continuous_watermark(restarted) == 200.0
        finally:
            restarted.db.close()
    finally:
        lower_db.close()
        higher_db.close()


def test_malformed_continuous_watermark_state_fails_closed(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    persist_continuous_watermark(store, 100.0)

    def corrupt(connection):
        connection.execute(
            "UPDATE session_bridge_state SET value_json = ?",
            ('{"continuous_watermark":"200"}',),
        )

    db._execute_write(corrupt)

    with pytest.raises(ValueError, match="finite number"):
        load_continuous_watermark(store)
    with pytest.raises(ValueError, match="finite number"):
        persist_continuous_watermark(store, 300.0)


def test_initial_backfill_candidates_are_newest_first_with_stable_ties():
    projections = (
        _projection(native_id="older", last_active=NOW - 30),
        _projection(native_id="tie-z", last_active=NOW - 10),
        _projection(native_id="newest", last_active=NOW - 1),
        _projection(native_id="tie-a", last_active=NOW - 10),
    )

    candidates = eligible_mirror_candidates(projections, _context())

    assert [candidate.source_session_id for candidate in candidates] == [
        "claude:newest",
        "claude:tie-a",
        "claude:tie-z",
        "claude:older",
    ]


def test_mirror_candidate_rejects_inconsistent_or_nonfinite_identity_fields():
    projection = _projection()

    with pytest.raises(ValueError, match="source session identity"):
        MirrorCandidate(
            source_session_id="claude:other",
            target_provider=Provider.CODEX,
            last_active=projection.last_active,
            projection=projection,
        )
    with pytest.raises(ValueError, match="inverse"):
        MirrorCandidate(
            source_session_id="claude:source-1",
            target_provider=Provider.CLAUDE,
            last_active=projection.last_active,
            projection=projection,
        )
    with pytest.raises(ValueError, match="finite number"):
        MirrorCandidate(
            source_session_id="claude:source-1",
            target_provider=Provider.CODEX,
            last_active=float("nan"),
            projection=projection,
        )
    with pytest.raises(ValueError, match="last activity"):
        MirrorCandidate(
            source_session_id="claude:source-1",
            target_provider=Provider.CODEX,
            last_active=projection.last_active - 1,
            projection=projection,
        )


def test_mirror_candidate_rejects_bridge_origin_projection():
    projection = _projection(
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        origin_bridge_id="bridge-1",
    )

    with pytest.raises(ValueError, match="native projection"):
        MirrorCandidate(
            source_session_id="claude:source-1",
            target_provider=Provider.CODEX,
            last_active=projection.last_active,
            projection=projection,
        )


def test_mirror_idempotency_key_matches_store_unique_key(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    projection = _projection()
    store.upsert_projection(projection)

    first = enqueue_mirror_job(
        store,
        canonical_session_id(projection.provider, projection.native_id),
        Provider.CODEX,
        policy=MirrorPolicy(generation=7),
        manual_authorized=True,
    )
    replay = enqueue_mirror_job(
        store,
        canonical_session_id(projection.provider, projection.native_id),
        Provider.CODEX,
        policy=MirrorPolicy(generation=7),
        manual_authorized=True,
    )

    assert replay == first
    assert first["idempotency_key"] == mirror_idempotency_key(
        "claude:source-1", Provider.CODEX, 7
    )
    assert len(_job_rows(db)) == 1


def test_default_policy_cannot_enqueue_without_explicit_manual_authority(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    store.upsert_projection(_projection())

    with pytest.raises(PermissionError, match="authority"):
        enqueue_mirror_job(
            store,
            "claude:source-1",
            Provider.CODEX,
            policy=MirrorPolicy(),
        )

    assert _job_rows(db) == []


def test_enqueue_requires_exact_inverse_provider_and_canonical_external_source(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    store.upsert_projection(_projection(provider=Provider.CODEX))

    with pytest.raises(ValueError, match="inverse"):
        enqueue_mirror_job(
            store,
            "codex:source-1",
            Provider.CODEX,
            policy=MirrorPolicy(),
            manual_authorized=True,
        )
    for malformed in (
        "source-1",
        "hermes:source-1",
        "claude:",
        " claude:source-1",
    ):
        with pytest.raises(ValueError, match="canonical Claude or Codex"):
            enqueue_mirror_job(
                store,
                malformed,
                Provider.CODEX,
                policy=MirrorPolicy(),
                manual_authorized=True,
            )

    assert _job_rows(db) == []


def test_explicit_manual_and_automatic_authority_enqueue_only_inverse_jobs(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    store.upsert_projection(_projection(provider=Provider.CLAUDE, native_id="manual"))
    automatic_projection = _projection(provider=Provider.CODEX, native_id="auto")
    store.upsert_projection(automatic_projection)

    manual = enqueue_mirror_job(
        store,
        "claude:manual",
        Provider.CODEX,
        policy=MirrorPolicy(generation=2),
        manual_authorized=True,
    )
    automatic_policy = MirrorPolicy(generation=2, automatic_creation=True)
    automatic_context = _context(policy=automatic_policy)
    automatic_candidate = _candidate(
        automatic_projection,
        policy=automatic_policy,
    )
    automatic = enqueue_mirror_job(
        store,
        "codex:auto",
        Provider.CLAUDE,
        policy=automatic_policy,
        candidate=automatic_candidate,
        context=automatic_context,
    )

    assert manual["target_provider"] == "codex"
    assert automatic["target_provider"] == "claude"
    assert len(_job_rows(db)) == 2


@pytest.mark.parametrize("manual_authorized", ("true", 0, 1, None))
def test_manual_enqueue_authority_requires_an_exact_boolean(db, manual_authorized):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    store.upsert_projection(_projection())

    with pytest.raises(ValueError, match="manual_authorized"):
        enqueue_mirror_job(
            store,
            "claude:source-1",
            Provider.CODEX,
            policy=MirrorPolicy(),
            manual_authorized=manual_authorized,
        )


@pytest.mark.parametrize("require_unmapped", ("true", 0, 1, None))
def test_safe_manual_constraint_requires_an_exact_boolean(db, require_unmapped):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    store.upsert_projection(_projection())

    with pytest.raises(ValueError, match="require_unmapped"):
        enqueue_mirror_job(
            store,
            "claude:source-1",
            Provider.CODEX,
            policy=MirrorPolicy(),
            manual_authorized=True,
            require_unmapped=require_unmapped,
        )

    assert _job_rows(db) == []


@pytest.mark.parametrize("rollout_limited", ("true", 0, 1, None))
def test_rollout_limited_authority_requires_an_exact_boolean(db, rollout_limited):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    store.upsert_projection(_projection())

    with pytest.raises(ValueError, match="rollout_limited"):
        enqueue_mirror_job(
            store,
            "claude:source-1",
            Provider.CODEX,
            policy=MirrorPolicy(),
            manual_authorized=True,
            require_unmapped=True,
            rollout_limited=rollout_limited,
        )

    assert _job_rows(db) == []


def test_rollout_limited_authority_requires_safe_manual_constraint(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    store.upsert_projection(_projection())

    with pytest.raises(ValueError, match="require_unmapped"):
        enqueue_mirror_job(
            store,
            "claude:source-1",
            Provider.CODEX,
            policy=MirrorPolicy(),
            manual_authorized=True,
            rollout_limited=True,
        )

    assert _job_rows(db) == []


def test_rollout_limited_manual_authority_is_persisted_and_claimable_with_auto_off(
    db,
):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    store.upsert_projection(_projection())
    job = enqueue_mirror_job(
        store,
        "claude:source-1",
        Provider.CODEX,
        policy=MirrorPolicy(),
        manual_authorized=True,
        require_unmapped=True,
        rollout_limited=True,
    )

    authority_json = _authority_rows(db)[0]["value_json"]
    assert isinstance(authority_json, str)
    authority = json.loads(authority_json)
    claimed = claim_due_mirror_jobs(
        store,
        limit=1,
        policy=MirrorPolicy(automatic_creation=False),
        job_ids=[job["id"]],
    )

    assert authority["authority"] == "manual"
    assert authority["require_unmapped"] is True
    assert authority["rollout_limited"] is True
    assert [row["id"] for row in claimed] == [job["id"]]
    assert claimed[0]["claim_authority"] == "manual"
    assert claimed[0]["rollout_limited"] is True


def test_automatic_enqueue_rejects_source_that_became_bridge_origin(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    policy = MirrorPolicy(automatic_creation=True)
    projection = _projection(provider=Provider.CODEX)
    store.upsert_projection(projection)
    context = _context(policy=policy)
    candidate = _candidate(projection, policy=policy)
    store.upsert_projection(
        replace(
            projection,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id="bridge-1",
        )
    )

    with pytest.raises(PermissionError, match="durable source origin"):
        enqueue_mirror_job(
            store,
            "codex:source-1",
            Provider.CLAUDE,
            policy=policy,
            candidate=candidate,
            context=context,
        )

    assert _job_rows(db) == []


@pytest.mark.parametrize("relation", (Relation.MIRRORS, Relation.CONTINUES))
def test_automatic_enqueue_rejects_mapping_added_after_eligibility(db, relation):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    policy = MirrorPolicy(automatic_creation=True)
    projection = _projection()
    target = _projection(
        provider=Provider.CODEX,
        native_id="target-1",
    )
    store.upsert_projection(projection)
    store.upsert_projection(target)
    context = _context(policy=policy)
    candidate = _candidate(projection, policy=policy)
    store.create_link(
        SessionLink(
            id="link-1",
            from_session_id="claude:source-1",
            to_session_id="codex:target-1",
            relation=relation,
            bridge_id="bridge-1",
            source_cursor="cursor-1",
            source_hash="hash-1",
            created_at=NOW,
        )
    )

    with pytest.raises(PermissionError, match="already mapped"):
        enqueue_mirror_job(
            store,
            "claude:source-1",
            Provider.CODEX,
            policy=policy,
            candidate=candidate,
            context=context,
        )

    assert _job_rows(db) == []


def test_automatic_enqueue_revalidates_at_advanced_authoritative_store_time(db):
    current_time = [NOW]
    store = SessionBridgeStore(db, clock=lambda: current_time[0])
    policy = MirrorPolicy(automatic_creation=True)
    projection = _projection()
    store.upsert_projection(projection)
    context = _context(policy=policy)
    candidate = _candidate(projection, policy=policy)
    current_time[0] = NOW + 1

    job = enqueue_mirror_job(
        store,
        "claude:source-1",
        Provider.CODEX,
        policy=policy,
        candidate=candidate,
        context=context,
    )

    assert job["state"] == "queued"
    assert job["created_at"] == NOW + 1


def test_manual_enqueue_explicitly_overrides_durable_origin_and_mapping(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    source = _projection(
        provider=Provider.CODEX,
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        origin_bridge_id="bridge-1",
    )
    store.upsert_projection(source)

    job = enqueue_mirror_job(
        store,
        "codex:source-1",
        Provider.CLAUDE,
        policy=MirrorPolicy(),
        manual_authorized=True,
    )

    assert job["state"] == "queued"


def test_safe_manual_enqueue_atomically_rejects_an_existing_mapping(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    store.upsert_projection(_projection())
    store.upsert_projection(_projection(provider=Provider.CODEX, native_id="target"))
    store.create_link(
        SessionLink(
            id="link-existing",
            from_session_id="claude:source-1",
            to_session_id="codex:target",
            relation=Relation.MIRRORS,
            bridge_id="bridge-existing",
            source_cursor=None,
            source_hash=None,
            created_at=NOW,
        )
    )

    with pytest.raises(PermissionError, match="already mapped"):
        enqueue_mirror_job(
            store,
            "claude:source-1",
            Provider.CODEX,
            policy=MirrorPolicy(),
            manual_authorized=True,
            require_unmapped=True,
        )


def test_safe_manual_job_cannot_be_claimed_after_mapping_appears(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    store.upsert_projection(_projection())
    enqueue_mirror_job(
        store,
        "claude:source-1",
        Provider.CODEX,
        policy=MirrorPolicy(),
        manual_authorized=True,
        require_unmapped=True,
    )
    store.upsert_projection(_projection(provider=Provider.CODEX, native_id="target"))
    store.create_link(
        SessionLink(
            id="link-late-safe-manual",
            from_session_id="claude:source-1",
            to_session_id="codex:target",
            relation=Relation.MIRRORS,
            bridge_id="bridge-late-safe-manual",
            source_cursor=None,
            source_hash=None,
            created_at=NOW,
        )
    )

    assert claim_due_mirror_jobs(store, limit=1, policy=MirrorPolicy()) == []
    durable = _job_rows(db)[0]
    assert durable["state"] == "manual_failure"
    assert durable["error_code"] == "manual_authority_revoked"


def test_legacy_manual_authority_sidecar_remains_an_unrestricted_admin_override(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    store.upsert_projection(_projection())
    job = enqueue_mirror_job(
        store,
        "claude:source-1",
        Provider.CODEX,
        policy=MirrorPolicy(),
        manual_authorized=True,
    )

    def remove_new_field(conn):
        key = f"session-bridge:mirror-authority:{job['id']}"
        row = conn.execute(
            "SELECT value_json FROM session_bridge_state WHERE key = ?",
            (key,),
        ).fetchone()
        value = json.loads(row["value_json"])
        value.pop("require_unmapped")
        value.pop("rollout_limited")
        conn.execute(
            "UPDATE session_bridge_state SET value_json = ? WHERE key = ?",
            (json.dumps(value, sort_keys=True, separators=(",", ":")), key),
        )

    db._execute_write(remove_new_field)

    claimed = claim_due_mirror_jobs(store, limit=1, policy=MirrorPolicy())

    assert [row["id"] for row in claimed] == [job["id"]]
    assert claimed[0]["state"] == "running"


def test_automatic_enqueue_requires_matching_candidate_context_and_policy(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    policy = MirrorPolicy(automatic_creation=True)
    projection = _projection()
    store.upsert_projection(projection)
    candidate = _candidate(projection, policy=policy)

    with pytest.raises(ValueError, match="candidate and context"):
        enqueue_mirror_job(
            store,
            "claude:source-1",
            Provider.CODEX,
            policy=policy,
        )
    with pytest.raises(ValueError, match="policy"):
        enqueue_mirror_job(
            store,
            "claude:source-1",
            Provider.CODEX,
            policy=policy,
            candidate=candidate,
            context=_context(policy=replace(policy, generation=2)),
        )

    assert _job_rows(db) == []


def test_continuous_enqueue_rechecks_current_durable_watermark_atomically(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    policy = MirrorPolicy(automatic_creation=True)
    projection = _projection(
        started_at=NOW - 50,
        last_active=NOW - 10,
    )
    store.upsert_projection(projection)
    persist_continuous_watermark(store, NOW - 100)
    context = _context(
        mode=DiscoveryMode.CONTINUOUS,
        watermark=NOW - 100,
        policy=policy,
    )
    candidate = _candidate(projection, policy=policy)
    persist_continuous_watermark(store, NOW - 25)

    with pytest.raises(PermissionError, match="before_watermark"):
        enqueue_mirror_job(
            store,
            "claude:source-1",
            Provider.CODEX,
            policy=policy,
            candidate=candidate,
            context=context,
        )

    assert _job_rows(db) == []
    assert _authority_rows(db) == []


def test_cross_generation_active_job_blocks_manual_and_automatic_duplicates(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    projection = _projection()
    store.upsert_projection(projection)
    first = enqueue_mirror_job(
        store,
        "claude:source-1",
        Provider.CODEX,
        policy=MirrorPolicy(generation=1),
        manual_authorized=True,
    )
    assert (
        enqueue_mirror_job(
            store,
            "claude:source-1",
            Provider.CODEX,
            policy=MirrorPolicy(generation=1),
            manual_authorized=True,
        )
        == first
    )

    with pytest.raises(ValueError, match="different-generation"):
        enqueue_mirror_job(
            store,
            "claude:source-1",
            Provider.CODEX,
            policy=MirrorPolicy(generation=2),
            manual_authorized=True,
        )
    automatic_policy = MirrorPolicy(generation=2, automatic_creation=True)
    with pytest.raises(ValueError, match="different-generation"):
        enqueue_mirror_job(
            store,
            "claude:source-1",
            Provider.CODEX,
            policy=automatic_policy,
            candidate=_candidate(projection, policy=automatic_policy),
            context=_context(policy=automatic_policy),
        )

    assert len(_job_rows(db)) == 1
    assert len(_authority_rows(db)) == 1


def test_exact_automatic_replay_with_manual_authority_promotes_sidecar(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    policy = MirrorPolicy(automatic_creation=True)
    projection = _projection()
    store.upsert_projection(projection)
    automatic = enqueue_mirror_job(
        store,
        "claude:source-1",
        Provider.CODEX,
        policy=policy,
        candidate=_candidate(projection, policy=policy),
        context=_context(policy=policy),
    )
    store.upsert_projection(
        replace(
            projection,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id="bridge-revoked",
        )
    )

    replay = enqueue_mirror_job(
        store,
        "claude:source-1",
        Provider.CODEX,
        policy=policy,
        manual_authorized=True,
    )
    authority_json = _authority_rows(db)[0]["value_json"]
    assert isinstance(authority_json, str)
    authority = json.loads(authority_json)
    claimed = claim_due_mirror_jobs(store, limit=1, policy=MirrorPolicy())

    assert replay == automatic
    assert authority["authority"] == "manual"
    assert [job["id"] for job in claimed] == [automatic["id"]]


def test_exact_manual_failure_requires_flag_then_requeues_for_recovery(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    store.upsert_projection(_projection())
    policy = MirrorPolicy(max_attempts=1)
    original = enqueue_mirror_job(
        store,
        "claude:source-1",
        Provider.CODEX,
        policy=policy,
        manual_authorized=True,
    )
    claim = claim_due_mirror_jobs(store, limit=1, policy=MirrorPolicy())[0]
    record_mirror_failure(
        store,
        claim,
        policy=policy,
        now=NOW,
        code="target_down",
        detail="temporary",
    )

    with pytest.raises(ValueError, match="retry_failed"):
        enqueue_mirror_job(
            store,
            "claude:source-1",
            Provider.CODEX,
            policy=policy,
            manual_authorized=True,
        )
    recovered = enqueue_mirror_job(
        store,
        "claude:source-1",
        Provider.CODEX,
        policy=policy,
        manual_authorized=True,
        retry_failed=True,
    )
    reclaimed = claim_due_mirror_jobs(store, limit=1, policy=MirrorPolicy())[0]

    assert recovered["id"] == original["id"]
    assert recovered["state"] == "queued"
    assert recovered["attempts"] == 0
    assert recovered["error_code"] is None
    assert recovered["error_detail"] is None
    assert reclaimed["attempts"] == 1


def test_retry_failed_requires_explicit_manual_authority(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    projection = _projection()
    store.upsert_projection(projection)
    policy = MirrorPolicy(automatic_creation=True)

    with pytest.raises(ValueError, match="manual_authorized"):
        enqueue_mirror_job(
            store,
            "claude:source-1",
            Provider.CODEX,
            policy=policy,
            candidate=_candidate(projection, policy=policy),
            context=_context(policy=policy),
            retry_failed=True,
        )

    assert _job_rows(db) == []


def test_exact_manual_authority_is_never_downgraded_by_automatic_replay(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    projection = _projection()
    store.upsert_projection(projection)
    manual = enqueue_mirror_job(
        store,
        "claude:source-1",
        Provider.CODEX,
        policy=MirrorPolicy(),
        manual_authorized=True,
    )
    automatic_policy = MirrorPolicy(automatic_creation=True)

    replay = enqueue_mirror_job(
        store,
        "claude:source-1",
        Provider.CODEX,
        policy=automatic_policy,
        candidate=_candidate(projection, policy=automatic_policy),
        context=_context(policy=automatic_policy),
    )
    authority_json = _authority_rows(db)[0]["value_json"]
    assert isinstance(authority_json, str)
    authority = json.loads(authority_json)

    assert replay["id"] == manual["id"]
    assert authority["authority"] == "manual"


def test_exact_replay_with_tampered_authority_rolls_back_recovery(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    store.upsert_projection(_projection())
    job = enqueue_mirror_job(
        store,
        "claude:source-1",
        Provider.CODEX,
        policy=MirrorPolicy(max_attempts=1),
        manual_authorized=True,
    )
    claim = claim_due_mirror_jobs(store, limit=1, policy=MirrorPolicy())[0]
    record_mirror_failure(
        store,
        claim,
        policy=MirrorPolicy(max_attempts=1),
        now=NOW,
        code="target_down",
        detail="temporary",
    )
    authority_key = _authority_rows(db)[0]["key"]
    assert isinstance(authority_key, str)
    store.set_state(authority_key, {"authority": "tampered"})
    before = _job_rows(db)[0]

    with pytest.raises(ValueError, match="authority"):
        enqueue_mirror_job(
            store,
            "claude:source-1",
            Provider.CODEX,
            policy=MirrorPolicy(max_attempts=1),
            manual_authorized=True,
            retry_failed=True,
        )

    assert _job_rows(db)[0] == before


def test_new_generation_requires_explicit_retry_of_prior_manual_failure(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    store.upsert_projection(_projection())
    first_policy = MirrorPolicy(generation=1, max_attempts=1)
    enqueue_mirror_job(
        store,
        "claude:source-1",
        Provider.CODEX,
        policy=first_policy,
        manual_authorized=True,
    )
    claim = claim_due_mirror_jobs(store, limit=1, policy=MirrorPolicy())[0]
    record_mirror_failure(
        store,
        claim,
        policy=first_policy,
        now=NOW,
        code="target_down",
        detail="temporary",
    )

    with pytest.raises(ValueError, match="retry_failed"):
        enqueue_mirror_job(
            store,
            "claude:source-1",
            Provider.CODEX,
            policy=MirrorPolicy(generation=2),
            manual_authorized=True,
        )
    second = enqueue_mirror_job(
        store,
        "claude:source-1",
        Provider.CODEX,
        policy=MirrorPolicy(generation=2),
        manual_authorized=True,
        retry_failed=True,
    )

    assert second["state"] == "queued"
    assert len(_job_rows(db)) == 2


def test_prior_succeeded_job_without_mapping_fails_closed_across_generations(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    store.upsert_projection(_projection())
    first = enqueue_mirror_job(
        store,
        "claude:source-1",
        Provider.CODEX,
        policy=MirrorPolicy(generation=1),
        manual_authorized=True,
    )

    def mark_succeeded(connection):
        connection.execute(
            "UPDATE session_mirror_jobs SET state = 'succeeded' WHERE id = ?",
            (first["id"],),
        )

    db._execute_write(mark_succeeded)

    with pytest.raises(ValueError, match="succeeded"):
        enqueue_mirror_job(
            store,
            "claude:source-1",
            Provider.CODEX,
            policy=MirrorPolicy(generation=2),
            manual_authorized=True,
            retry_failed=True,
        )

    assert len(_job_rows(db)) == 1


def test_enqueue_rolls_back_job_when_authority_sidecar_write_fails(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    store.upsert_projection(_projection())

    def install_failure_trigger(connection):
        connection.execute(
            """CREATE TRIGGER reject_mirror_authority
               BEFORE INSERT ON session_bridge_state
               WHEN NEW.key LIKE 'session-bridge:mirror-authority:%'
               BEGIN
                   SELECT RAISE(ABORT, 'authority blocked');
               END"""
        )

    db._execute_write(install_failure_trigger)

    with pytest.raises(sqlite3.IntegrityError, match="authority blocked"):
        enqueue_mirror_job(
            store,
            "claude:source-1",
            Provider.CODEX,
            policy=MirrorPolicy(),
            manual_authorized=True,
        )

    assert _job_rows(db) == []
    assert _authority_rows(db) == []


def test_retry_delay_is_deterministic_bounded_exponential_with_keyed_jitter():
    key = mirror_idempotency_key("claude:source-1", Provider.CODEX, 1)

    delays = [retry_delay_seconds(key, attempts) for attempts in range(1, 11)]
    replay = [retry_delay_seconds(key, attempts) for attempts in range(1, 11)]
    other = [
        retry_delay_seconds("different-key", attempts) for attempts in range(1, 11)
    ]

    assert delays == replay
    assert delays != other
    for attempts, delay in enumerate(delays, start=1):
        base = min(300.0, 2.0 ** (attempts - 1))
        assert base * 0.8 <= delay <= base * 1.2
        assert delay <= 300.0


def test_failure_retries_then_moves_to_manual_failure_at_max_attempts(db):
    current_time = [NOW]
    store = SessionBridgeStore(db, clock=lambda: current_time[0])
    projection = _projection()
    store.upsert_projection(projection)
    policy = MirrorPolicy(max_attempts=2)
    job = enqueue_mirror_job(
        store,
        "claude:source-1",
        Provider.CODEX,
        policy=policy,
        manual_authorized=True,
    )
    first_claim = store.claim_due_jobs(now=NOW, limit=1, policy=policy)[0]

    first_state = record_mirror_failure(
        store,
        first_claim,
        policy=policy,
        now=NOW,
        code="target_down",
        detail="temporary",
    )
    delay = retry_delay_seconds(job["idempotency_key"], 1)
    current_time[0] = NOW + delay
    second_claim = store.claim_due_jobs(now=current_time[0], limit=1, policy=policy)[0]
    final_state = record_mirror_failure(
        store,
        second_claim,
        policy=policy,
        now=current_time[0],
        code="target_down",
        detail="temporary",
    )

    assert first_state is MirrorJobState.RETRY
    assert second_claim["attempts"] == 2
    assert final_state is MirrorJobState.MANUAL_FAILURE
    assert _job_rows(db)[0]["state"] == "manual_failure"


def test_failure_callback_rejects_stale_caller_time_and_uses_store_clock(db):
    current_time = [NOW]
    store = SessionBridgeStore(db, clock=lambda: current_time[0])
    store.upsert_projection(_projection())
    job = enqueue_mirror_job(
        store,
        "claude:source-1",
        Provider.CODEX,
        policy=MirrorPolicy(),
        manual_authorized=True,
    )
    claim = store.claim_due_jobs(now=NOW, limit=1, policy=MirrorPolicy())[0]

    with pytest.raises(ValueError, match="caller time"):
        record_mirror_failure(
            store,
            claim,
            policy=MirrorPolicy(),
            now=0.0,
            code="target_down",
            detail="temporary",
        )
    assert _job_rows(db)[0]["state"] == "running"

    current_time[0] = NOW + 10
    state = record_mirror_failure(
        store,
        claim,
        policy=MirrorPolicy(),
        now=NOW,
        code="target_down",
        detail="temporary",
    )
    durable = _job_rows(db)[0]

    assert state is MirrorJobState.RETRY
    assert durable["next_attempt_at"] == current_time[0] + retry_delay_seconds(
        job["idempotency_key"], 1
    )


def test_failure_callback_rejects_future_caller_time(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    store.upsert_projection(_projection())
    enqueue_mirror_job(
        store,
        "claude:source-1",
        Provider.CODEX,
        policy=MirrorPolicy(),
        manual_authorized=True,
    )
    claim = store.claim_due_jobs(now=NOW, limit=1, policy=MirrorPolicy())[0]

    with pytest.raises(ValueError, match="caller time"):
        record_mirror_failure(
            store,
            claim,
            policy=MirrorPolicy(),
            now=NOW + 1,
            code="target_down",
            detail="temporary",
        )

    assert _job_rows(db)[0]["state"] == "running"


def test_failure_retry_clock_is_sampled_after_write_lock_acquisition(db, monkeypatch):
    current_time = [NOW]
    store = SessionBridgeStore(db, clock=lambda: current_time[0])
    store.upsert_projection(_projection())
    job = enqueue_mirror_job(
        store,
        "claude:source-1",
        Provider.CODEX,
        policy=MirrorPolicy(),
        manual_authorized=True,
    )
    claim = claim_due_mirror_jobs(store, limit=1, policy=MirrorPolicy())[0]
    original_write = db._execute_write

    def advance_before_write(operation):
        current_time[0] = NOW + 100
        return original_write(operation)

    monkeypatch.setattr(db, "_execute_write", advance_before_write)

    state = record_mirror_failure(
        store,
        claim,
        policy=MirrorPolicy(),
        now=NOW,
        code="target_down",
        detail="temporary",
    )
    durable = _job_rows(db)[0]

    assert state is MirrorJobState.RETRY
    assert durable["updated_at"] == NOW + 100
    assert durable["next_attempt_at"] == NOW + 100 + retry_delay_seconds(
        job["idempotency_key"], 1
    )


@pytest.mark.parametrize(
    "forged_fields",
    (
        {"attempts": 5},
        {"idempotency_key": "forged-key"},
    ),
)
def test_failure_callback_rejects_forged_claim_snapshot_without_mutation(
    db, forged_fields
):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    store.upsert_projection(_projection())
    job = enqueue_mirror_job(
        store,
        "claude:source-1",
        Provider.CODEX,
        policy=MirrorPolicy(),
        manual_authorized=True,
    )
    claim = store.claim_due_jobs(now=NOW, limit=1, policy=MirrorPolicy())[0]
    forged = {**claim, **forged_fields}

    with pytest.raises(ValueError, match="stale mirror job claim"):
        record_mirror_failure(
            store,
            forged,
            policy=MirrorPolicy(),
            now=NOW,
            code="target_down",
            detail="temporary",
        )

    durable = _job_rows(db)[0]
    assert durable["state"] == "running"
    assert durable["attempts"] == 1
    assert durable["error_code"] is None


def test_stale_failure_callback_after_retry_is_rejected_without_mutation(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    store.upsert_projection(_projection())
    job = enqueue_mirror_job(
        store,
        "claude:source-1",
        Provider.CODEX,
        policy=MirrorPolicy(),
        manual_authorized=True,
    )
    claim = store.claim_due_jobs(now=NOW, limit=1, policy=MirrorPolicy())[0]
    record_mirror_failure(
        store,
        claim,
        policy=MirrorPolicy(),
        now=NOW,
        code="target_down",
        detail="temporary",
    )
    before = _job_rows(db)[0]

    with pytest.raises(ValueError, match="stale mirror job claim"):
        record_mirror_failure(
            store,
            claim,
            policy=MirrorPolicy(),
            now=NOW,
            code="second",
            detail="must not overwrite",
        )

    assert _job_rows(db)[0] == before


@pytest.mark.parametrize(
    ("code", "detail"),
    ((1, "detail"), ("code", 1), ("", "detail"), ("code", "")),
)
def test_failure_callback_requires_strict_nonempty_diagnostics(db, code, detail):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    store.upsert_projection(_projection())
    enqueue_mirror_job(
        store,
        "claude:source-1",
        Provider.CODEX,
        policy=MirrorPolicy(),
        manual_authorized=True,
    )
    claim = store.claim_due_jobs(now=NOW, limit=1, policy=MirrorPolicy())[0]

    with pytest.raises(ValueError, match="code and detail"):
        record_mirror_failure(
            store,
            claim,
            policy=MirrorPolicy(),
            now=NOW,
            code=code,
            detail=detail,
        )

    assert _job_rows(db)[0]["state"] == "running"


def test_automatic_job_cannot_be_claimed_after_durable_origin_changes(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    policy = MirrorPolicy(automatic_creation=True)
    projection = _projection()
    store.upsert_projection(projection)
    enqueue_mirror_job(
        store,
        "claude:source-1",
        Provider.CODEX,
        policy=policy,
        candidate=_candidate(projection, policy=policy),
        context=_context(policy=policy),
    )
    store.upsert_projection(
        replace(
            projection,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id="bridge-late",
        )
    )

    assert claim_due_mirror_jobs(store, limit=1, policy=policy) == []
    durable = _job_rows(db)[0]
    assert durable["state"] == "manual_failure"
    assert durable["attempts"] == 0
    assert durable["error_code"] == "automatic_authority_revoked"


def test_automatic_job_cannot_be_claimed_after_target_mapping_appears(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    policy = MirrorPolicy(automatic_creation=True)
    projection = _projection()
    store.upsert_projection(projection)
    store.upsert_projection(_projection(provider=Provider.CODEX, native_id="target"))
    enqueue_mirror_job(
        store,
        "claude:source-1",
        Provider.CODEX,
        policy=policy,
        candidate=_candidate(projection, policy=policy),
        context=_context(policy=policy),
    )
    store.create_link(
        SessionLink(
            id="link-late",
            from_session_id="claude:source-1",
            to_session_id="codex:target",
            relation=Relation.CONTINUES,
            bridge_id="bridge-late",
            source_cursor="cursor-1",
            source_hash="hash-1",
            created_at=NOW,
        )
    )

    assert claim_due_mirror_jobs(store, limit=1, policy=policy) == []
    durable = _job_rows(db)[0]
    assert durable["state"] == "manual_failure"
    assert durable["error_code"] == "automatic_authority_revoked"


def test_manual_job_authority_sidecar_intentionally_bypasses_origin_policy(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    store.upsert_projection(
        _projection(
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id="bridge-1",
        )
    )
    enqueue_mirror_job(
        store,
        "claude:source-1",
        Provider.CODEX,
        policy=MirrorPolicy(),
        manual_authorized=True,
    )

    claimed = claim_due_mirror_jobs(store, limit=1, policy=MirrorPolicy())

    assert len(claimed) == 1
    assert claimed[0]["state"] == "running"
    assert claimed[0]["attempts"] == 1
    assert claim_due_mirror_jobs(store, limit=1, policy=MirrorPolicy()) == []


def test_claim_fails_closed_when_authority_sidecar_is_missing(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    store.upsert_projection(_projection())
    store.enqueue_mirror_job("claude:source-1", Provider.CODEX, policy_generation=1)

    assert claim_due_mirror_jobs(store, limit=1, policy=MirrorPolicy()) == []
    durable = _job_rows(db)[0]
    assert durable["state"] == "manual_failure"
    assert durable["error_code"] == "authority_missing"


def test_claim_fails_closed_when_authority_sidecar_is_malformed(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    store.upsert_projection(_projection())
    enqueue_mirror_job(
        store,
        "claude:source-1",
        Provider.CODEX,
        policy=MirrorPolicy(),
        manual_authorized=True,
    )
    authority = _authority_rows(db)[0]
    authority_key = authority["key"]
    assert isinstance(authority_key, str)
    store.set_state(authority_key, {"authority": "bogus"})

    assert claim_due_mirror_jobs(store, limit=1, policy=MirrorPolicy()) == []
    durable = _job_rows(db)[0]
    assert durable["state"] == "manual_failure"
    assert durable["error_code"] == "authority_invalid"


def test_claim_rejects_rollout_limited_automatic_authority_sidecar(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    policy = MirrorPolicy(automatic_creation=True)
    projection = _projection()
    store.upsert_projection(projection)
    job = enqueue_mirror_job(
        store,
        "claude:source-1",
        Provider.CODEX,
        policy=policy,
        candidate=_candidate(projection, policy=policy),
        context=_context(policy=policy),
    )

    def forge_rollout_authority(conn):
        key = f"session-bridge:mirror-authority:{job['id']}"
        row = conn.execute(
            "SELECT value_json FROM session_bridge_state WHERE key = ?",
            (key,),
        ).fetchone()
        value = json.loads(row["value_json"])
        value["require_unmapped"] = True
        value["rollout_limited"] = True
        conn.execute(
            "UPDATE session_bridge_state SET value_json = ? WHERE key = ?",
            (json.dumps(value, sort_keys=True, separators=(",", ":")), key),
        )

    db._execute_write(forge_rollout_authority)

    assert claim_due_mirror_jobs(store, limit=1, policy=policy) == []
    durable = _job_rows(db)[0]
    assert durable["state"] == "manual_failure"
    assert durable["error_code"] == "authority_invalid"


def test_guarded_claim_requires_current_policy(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)

    with pytest.raises(TypeError, match="policy"):
        claim_due_mirror_jobs(store, limit=1)  # type: ignore[missing-argument]


def test_disabled_automatic_creation_pauses_auto_without_starving_manual(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    automatic_policy = MirrorPolicy(automatic_creation=True)
    automatic_ids: list[str] = []
    for index in range(12):
        projection = _projection(native_id=f"auto-{index:02d}")
        store.upsert_projection(projection)
        automatic = enqueue_mirror_job(
            store,
            f"claude:auto-{index:02d}",
            Provider.CODEX,
            policy=automatic_policy,
            candidate=_candidate(projection, policy=automatic_policy),
            context=_context(policy=automatic_policy),
        )
        automatic_ids.append(automatic["id"])
    manual_projection = _projection(native_id="manual-last")
    store.upsert_projection(manual_projection)
    manual = enqueue_mirror_job(
        store,
        "claude:manual-last",
        Provider.CODEX,
        policy=MirrorPolicy(),
        manual_authorized=True,
    )

    claimed = claim_due_mirror_jobs(store, limit=1, policy=MirrorPolicy())
    rows = {row["id"]: row for row in _job_rows(db)}

    assert [job["id"] for job in claimed] == [manual["id"]]
    assert all(rows[job_id]["state"] == "queued" for job_id in automatic_ids)


def test_enabled_automatic_creation_claims_valid_automatic_job(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    policy = MirrorPolicy(automatic_creation=True)
    projection = _projection()
    store.upsert_projection(projection)
    automatic = enqueue_mirror_job(
        store,
        "claude:source-1",
        Provider.CODEX,
        policy=policy,
        candidate=_candidate(projection, policy=policy),
        context=_context(policy=policy),
    )

    claimed = claim_due_mirror_jobs(store, limit=1, policy=policy)

    assert [job["id"] for job in claimed] == [automatic["id"]]


def test_provider_specific_concurrency_limits():
    policy = MirrorPolicy(claude_concurrency=1, codex_concurrency=2)

    assert provider_concurrency_limit(policy, Provider.CLAUDE) == 1
    assert provider_concurrency_limit(policy, Provider.CODEX) == 2
    with pytest.raises(ValueError, match="Claude or Codex"):
        provider_concurrency_limit(policy, Provider.HERMES)


def test_creation_batch_is_newest_first_and_respects_provider_concurrency():
    policy = MirrorPolicy(
        automatic_creation=True,
        claude_concurrency=1,
        codex_concurrency=2,
        creates_per_minute=10,
    )
    candidates = tuple(
        _candidate(projection, policy=policy)
        for projection in (
            _projection(
                provider=Provider.CLAUDE,
                native_id="claude-newest",
                last_active=NOW - 1,
            ),
            _projection(
                provider=Provider.CODEX,
                native_id="codex-newest",
                last_active=NOW - 2,
            ),
            _projection(
                provider=Provider.CLAUDE,
                native_id="claude-second",
                last_active=NOW - 3,
            ),
            _projection(
                provider=Provider.CODEX,
                native_id="codex-capacity-exhausted",
                last_active=NOW - 4,
            ),
            _projection(
                provider=Provider.CLAUDE,
                native_id="claude-capacity-exhausted",
                last_active=NOW - 5,
            ),
        )
    )

    selected = select_creation_batch(
        tuple(reversed(candidates)),
        policy=policy,
        context=_context(policy=policy),
        now=NOW,
        in_flight_by_provider={Provider.CLAUDE: 0, Provider.CODEX: 0},
        recent_creation_times=(),
        progress=BatchProgress(),
    )

    assert [candidate.source_session_id for candidate in selected] == [
        "claude:claude-newest",
        "codex:codex-newest",
        "claude:claude-second",
    ]


def test_creation_batch_deduplicates_repeated_candidate_mapping():
    policy = MirrorPolicy(automatic_creation=True)
    candidate = _candidate(_projection(), policy=policy)

    selected = select_creation_batch(
        (candidate, candidate),
        policy=policy,
        context=_context(policy=policy),
        now=NOW,
        in_flight_by_provider={},
        recent_creation_times=(),
        progress=BatchProgress(),
    )

    assert selected == (candidate,)


def test_creation_batch_obeys_rolling_creates_per_minute_limit():
    policy = MirrorPolicy(
        automatic_creation=True,
        creates_per_minute=2,
        codex_concurrency=5,
    )
    candidates = tuple(
        _candidate(
            _projection(native_id=f"source-{index}", last_active=NOW - index),
            policy=policy,
        )
        for index in range(1, 5)
    )

    selected = select_creation_batch(
        candidates,
        policy=policy,
        context=_context(policy=policy),
        now=NOW,
        in_flight_by_provider={},
        recent_creation_times=(NOW - 60.0, NOW - 59.999),
        progress=BatchProgress(),
    )

    assert [candidate.source_session_id for candidate in selected] == [
        "claude:source-1"
    ]


def test_creation_batch_reserves_only_remaining_batch_attempts():
    policy = MirrorPolicy(
        automatic_creation=True,
        stop_after_attempts=20,
        creates_per_minute=10,
        codex_concurrency=5,
    )
    candidates = tuple(
        _candidate(
            _projection(native_id=f"source-{index}", last_active=NOW - index),
            policy=policy,
        )
        for index in range(1, 6)
    )

    selected = select_creation_batch(
        candidates,
        policy=policy,
        context=_context(policy=policy),
        now=NOW,
        in_flight_by_provider={},
        recent_creation_times=(),
        progress=BatchProgress(attempts=19, errors=0),
    )
    exhausted = select_creation_batch(
        candidates,
        policy=policy,
        context=_context(policy=policy),
        now=NOW,
        in_flight_by_provider={},
        recent_creation_times=(),
        progress=BatchProgress(attempts=20, errors=0),
    )

    assert [candidate.source_session_id for candidate in selected] == [
        "claude:source-1"
    ]
    assert exhausted == ()


def test_creation_batch_rejects_unknown_in_flight_provider_keys():
    policy = MirrorPolicy(automatic_creation=True)
    candidate = _candidate(_projection(), policy=policy)

    with pytest.raises(ValueError, match="Claude or Codex"):
        select_creation_batch(
            (candidate,),
            policy=policy,
            context=_context(policy=policy),
            now=NOW,
            in_flight_by_provider={"other": 1},
            recent_creation_times=(),
            progress=BatchProgress(),
        )


def test_creation_batch_normalizes_known_string_in_flight_provider_keys():
    policy = MirrorPolicy(automatic_creation=True, codex_concurrency=1)
    candidate = _candidate(_projection(), policy=policy)

    selected = select_creation_batch(
        (candidate,),
        policy=policy,
        context=_context(policy=policy),
        now=NOW,
        in_flight_by_provider={"codex": 1},
        recent_creation_times=(),
        progress=BatchProgress(),
    )

    assert selected == ()


def test_creation_batch_rejects_future_creation_timestamps():
    policy = MirrorPolicy(automatic_creation=True)
    candidate = _candidate(_projection(), policy=policy)

    with pytest.raises(ValueError, match="future"):
        select_creation_batch(
            (candidate,),
            policy=policy,
            context=_context(policy=policy),
            now=NOW,
            in_flight_by_provider={},
            recent_creation_times=(NOW + 0.001,),
            progress=BatchProgress(),
        )


@pytest.mark.parametrize(
    "projection",
    (
        _projection(messages=()),
        _projection(
            started_at=NOW + 1,
            last_active=NOW + 10,
            messages=(_message(timestamp=NOW + 5),),
        ),
        _projection(
            last_active=NOW,
            messages=(_message(timestamp=NOW - 4.999),),
        ),
    ),
)
def test_creation_batch_revalidates_handcrafted_candidate_at_selection_time(
    projection,
):
    policy = MirrorPolicy(automatic_creation=True)
    candidate = MirrorCandidate(
        source_session_id=canonical_session_id(
            projection.provider, projection.native_id
        ),
        target_provider=Provider.CODEX,
        last_active=projection.last_active,
        projection=projection,
    )

    selected = select_creation_batch(
        (candidate,),
        policy=policy,
        context=_context(policy=policy),
        now=NOW,
        in_flight_by_provider={},
        recent_creation_times=(),
        progress=BatchProgress(),
    )

    assert selected == ()


def test_creation_batch_revalidates_age_watermark_and_existing_mappings():
    policy = MirrorPolicy(automatic_creation=True)
    old_projection = _projection(last_active=NOW - 30 * DAY - 1)
    old_candidate = MirrorCandidate(
        source_session_id="claude:source-1",
        target_provider=Provider.CODEX,
        last_active=old_projection.last_active,
        projection=old_projection,
    )
    continuous_projection = _projection(
        native_id="continuous",
        started_at=NOW - 120,
        last_active=NOW - 10,
    )
    continuous_candidate = MirrorCandidate(
        source_session_id="claude:continuous",
        target_provider=Provider.CODEX,
        last_active=continuous_projection.last_active,
        projection=continuous_projection,
    )
    mapped_projection = _projection(native_id="mapped")
    mapped_candidate = MirrorCandidate(
        source_session_id="claude:mapped",
        target_provider=Provider.CODEX,
        last_active=mapped_projection.last_active,
        projection=mapped_projection,
    )

    old_selected = select_creation_batch(
        (old_candidate,),
        policy=policy,
        context=_context(policy=policy),
        now=NOW,
        in_flight_by_provider={},
        recent_creation_times=(),
        progress=BatchProgress(),
    )
    continuous_selected = select_creation_batch(
        (continuous_candidate,),
        policy=policy,
        context=_context(
            mode=DiscoveryMode.CONTINUOUS,
            watermark=NOW - 60,
            policy=policy,
        ),
        now=NOW,
        in_flight_by_provider={},
        recent_creation_times=(),
        progress=BatchProgress(),
    )
    mapped_selected = select_creation_batch(
        (mapped_candidate,),
        policy=policy,
        context=_context(
            mappings=frozenset({("claude:mapped", Provider.CODEX)}),
            policy=policy,
        ),
        now=NOW,
        in_flight_by_provider={},
        recent_creation_times=(),
        progress=BatchProgress(),
    )

    assert old_selected == ()
    assert continuous_selected == ()
    assert mapped_selected == ()


def test_automatic_creation_batch_requires_current_matching_context():
    policy = MirrorPolicy(automatic_creation=True)
    candidate = _candidate(_projection(), policy=policy)

    with pytest.raises(ValueError, match="context"):
        select_creation_batch(
            (candidate,),
            policy=policy,
            now=NOW,
            in_flight_by_provider={},
            recent_creation_times=(),
            progress=BatchProgress(),
        )
    with pytest.raises(ValueError, match="context"):
        select_creation_batch(
            (candidate,),
            policy=policy,
            context=_context(now=NOW - 1, policy=policy),
            now=NOW,
            in_flight_by_provider={},
            recent_creation_times=(),
            progress=BatchProgress(),
        )


def test_safe_default_disables_automatic_creation_selection():
    policy = MirrorPolicy()
    candidate = _candidate(_projection(), policy=policy)

    selected = select_creation_batch(
        (candidate,),
        policy=policy,
        context=_context(policy=policy),
        now=NOW,
        in_flight_by_provider={},
        recent_creation_times=(),
        progress=BatchProgress(),
    )

    assert selected == ()


def test_batch_halts_at_attempt_cap_or_error_rate_threshold():
    policy = MirrorPolicy(stop_after_attempts=20, stop_error_rate=0.25)

    assert should_halt_batch(BatchProgress(attempts=20, errors=0), policy) is True
    assert should_halt_batch(BatchProgress(attempts=4, errors=1), policy) is True
    assert should_halt_batch(BatchProgress(attempts=3, errors=0), policy) is False


def test_zero_error_rate_threshold_means_stop_on_first_error_not_first_success():
    policy = MirrorPolicy(stop_error_rate=0)

    assert should_halt_batch(BatchProgress(attempts=1, errors=0), policy) is False
    assert should_halt_batch(BatchProgress(attempts=20, errors=0), policy) is True
    assert should_halt_batch(BatchProgress(attempts=1, errors=1), policy) is True


def test_halted_batch_selects_no_jobs():
    policy = MirrorPolicy(automatic_creation=True)
    candidate = _candidate(_projection(), policy=policy)

    selected = select_creation_batch(
        (candidate,),
        policy=policy,
        context=_context(policy=policy),
        now=NOW,
        in_flight_by_provider={},
        recent_creation_times=(),
        progress=BatchProgress(attempts=4, errors=1),
    )

    assert selected == ()


def test_policy_and_progress_reject_unsafe_values():
    with pytest.raises(ValueError, match="backfill_days"):
        MirrorPolicy(backfill_days=-1)
    with pytest.raises(ValueError, match="creates_per_minute"):
        MirrorPolicy(creates_per_minute=0)
    with pytest.raises(ValueError, match="stop_error_rate"):
        MirrorPolicy(stop_error_rate=1.1)
    with pytest.raises(ValueError, match="errors cannot exceed attempts"):
        BatchProgress(attempts=1, errors=2)


@pytest.mark.parametrize("value", ("false", 0, 1, None))
def test_automatic_creation_requires_an_exact_boolean(value):
    with pytest.raises((TypeError, ValueError), match="automatic_creation"):
        MirrorPolicy(automatic_creation=value)


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("debounce_seconds", "5.0"),
        ("debounce_seconds", True),
        ("debounce_seconds", float("nan")),
        ("debounce_seconds", float("inf")),
        ("stop_error_rate", "0.25"),
        ("stop_error_rate", False),
        ("stop_error_rate", float("nan")),
        ("stop_error_rate", float("-inf")),
    ),
)
def test_float_policy_fields_require_real_finite_numbers(field_name: str, value):
    with pytest.raises(ValueError, match=field_name):
        replace(MirrorPolicy(), **{field_name: value})


def test_float_policy_fields_are_normalized_in_frozen_policy():
    policy = MirrorPolicy(debounce_seconds=5, stop_error_rate=0)

    assert policy.debounce_seconds == 5.0
    assert type(policy.debounce_seconds) is float
    assert policy.stop_error_rate == 0.0
    assert type(policy.stop_error_rate) is float


def test_eligibility_value_objects_are_frozen():
    context = _context()
    result = classify_mirror_eligibility(_projection(), context)

    with pytest.raises(FrozenInstanceError):
        context.now = NOW + 1  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.reason = "empty"  # type: ignore[misc]
