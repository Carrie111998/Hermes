from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone

import pytest

from hermes_state import SessionDB
from session_bridge.mirror import (
    BatchProgress,
    DiscoveryMode,
    EligibilityContext,
    MirrorCandidate,
    MirrorPolicy,
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
    started_at: float = NOW - 60.0,
    last_active: float = NOW - 10.0,
    messages: tuple[ProjectedMessage, ...] | None = None,
    origin_kind: OriginKind = OriginKind.NATIVE,
    origin_bridge_id: str | None = None,
) -> SessionProjection:
    return SessionProjection(
        provider=provider,
        native_id=native_id,
        title="A real session",
        cwd="C:/workspace/project",
        started_at=started_at,
        last_active=last_active,
        messages=messages if messages is not None else (_message(),),
        native_cursor="cursor-1",
        native_hash="hash-1",
        origin_kind=origin_kind,
        origin_bridge_id=origin_bridge_id,
    )


def _context(
    *,
    mode: DiscoveryMode = DiscoveryMode.INITIAL_BACKFILL,
    watermark: float | None = None,
    mappings: frozenset[tuple[str, Provider]] = frozenset(),
    policy: MirrorPolicy = MirrorPolicy(),
) -> EligibilityContext:
    return EligibilityContext(
        now=NOW,
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
        _projection(messages=(_message(timestamp=NOW - 4.999),)), _context()
    )
    stable = classify_mirror_eligibility(
        _projection(messages=(_message(timestamp=NOW - 5.0),)), _context()
    )

    assert (still_debouncing.eligible, still_debouncing.reason) == (False, "empty")
    assert (stable.eligible, stable.reason) == (True, "eligible")


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


def test_mirror_idempotency_key_matches_store_unique_key(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    projection = _projection()
    store.upsert_projection(projection)

    first = enqueue_mirror_job(
        store,
        canonical_session_id(projection.provider, projection.native_id),
        Provider.CODEX,
        policy=MirrorPolicy(generation=7),
    )
    replay = enqueue_mirror_job(
        store,
        canonical_session_id(projection.provider, projection.native_id),
        Provider.CODEX,
        policy=MirrorPolicy(generation=7),
    )

    assert replay == first
    assert first["idempotency_key"] == mirror_idempotency_key(
        "claude:source-1", Provider.CODEX, 7
    )
    assert len(_job_rows(db)) == 1


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


def test_failure_retries_then_moves_to_manual_failure_at_max_attempts(db):
    store = SessionBridgeStore(db, clock=lambda: NOW)
    projection = _projection()
    store.upsert_projection(projection)
    policy = MirrorPolicy(max_attempts=2)
    job = enqueue_mirror_job(store, "claude:source-1", Provider.CODEX, policy=policy)
    first_claim = store.claim_due_jobs(now=NOW, limit=1)[0]

    first_state = record_mirror_failure(
        store,
        first_claim,
        policy=policy,
        now=NOW,
        code="target_down",
        detail="temporary",
    )
    delay = retry_delay_seconds(job["idempotency_key"], 1)
    second_claim = store.claim_due_jobs(now=NOW + delay, limit=1)[0]
    final_state = record_mirror_failure(
        store,
        second_claim,
        policy=policy,
        now=NOW + delay,
        code="target_down",
        detail="temporary",
    )

    assert first_state is MirrorJobState.RETRY
    assert second_claim["attempts"] == 2
    assert final_state is MirrorJobState.MANUAL_FAILURE
    assert _job_rows(db)[0]["state"] == "manual_failure"


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
        now=NOW,
        in_flight_by_provider={},
        recent_creation_times=(NOW - 60.0, NOW - 59.999),
        progress=BatchProgress(),
    )

    assert [candidate.source_session_id for candidate in selected] == [
        "claude:source-1"
    ]


def test_safe_default_disables_automatic_creation_selection():
    policy = MirrorPolicy()
    candidate = _candidate(_projection(), policy=policy)

    selected = select_creation_batch(
        (candidate,),
        policy=policy,
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


def test_halted_batch_selects_no_jobs():
    policy = MirrorPolicy(automatic_creation=True)
    candidate = _candidate(_projection(), policy=policy)

    selected = select_creation_batch(
        (candidate,),
        policy=policy,
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


def test_eligibility_value_objects_are_frozen():
    context = _context()
    result = classify_mirror_eligibility(_projection(), context)

    with pytest.raises(FrozenInstanceError):
        context.now = NOW + 1  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.reason = "empty"  # type: ignore[misc]
