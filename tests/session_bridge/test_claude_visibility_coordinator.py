from __future__ import annotations

import logging
from dataclasses import replace
from datetime import timezone
from decimal import Decimal

from hermes_state import SessionDB
from session_bridge.claude_visibility import (
    ClaudeVisibilityClaim,
    build_claude_registration_prompt,
    build_claude_visibility_candidate,
    derive_claude_visibility_identity,
)
from session_bridge.config import BridgeConfig
from session_bridge.coordinator import (
    ClaudeVisibilityCoordinator,
    _claude_visibility_enqueue_gates,
)
from session_bridge.models import (
    OriginKind,
    ProjectedMessage,
    Provider,
    SessionProjection,
)
from session_bridge.store import SessionBridgeStore, SidebarSource


SECRET = b"visibility-coordinator-test-secret"
NOW = 2_000_000.0


def _source(
    native_id: str,
    *,
    provider: Provider = Provider.CODEX,
    active: float = NOW - 60,
    text: str = "Implement the reviewed visibility coordinator",
    origin: OriginKind = OriginKind.NATIVE,
    automation: bool = False,
    subagent: bool = False,
) -> SidebarSource:
    projection = SessionProjection(
        provider=provider,
        native_id=native_id,
        title="Exact source title",
        cwd="C:/work/exact",
        started_at=active - 10,
        last_active=active,
        messages=(ProjectedMessage("m1", 1, "user", text, active),),
        origin_kind=origin,
        origin_bridge_id="bridge:x" if origin is not OriginKind.NATIVE else None,
        git_branch="feature/exact",
    )
    return SidebarSource(
        source_session_id=f"{provider.value}:{native_id}",
        projection=projection,
        git_root="C:/work",
        git_head="abc123",
        worktree_id="wt-1",
        automation_only=automation,
        subagent_only=subagent,
    )


class FakeStore:
    def __init__(self, *, claim: ClaudeVisibilityClaim | None = None) -> None:
        self.enqueued = []
        self.claim = claim or ClaudeVisibilityClaim(status="no_due_job")
        self.claim_calls = 0
        self.status_calls = 0
        self.source_checks = 0
        self.cycle_records = []
        self.open_sources: set[str] = set()
        self.raw_status = {
            "counts": {
                "claude_pending": 0,
                "claude_leased": 0,
                "claude_retry": 0,
                "claude_visible": 0,
                "claude_failed": 0,
            },
            "retry_codes": {},
            "failed_codes": {},
            "usage": {
                "local_day": "1970-01-24",
                "attempts": 0,
                "reserved_cost_usd": "0",
            },
        }

    def claude_visibility_status(self, now: float):
        self.status_calls += 1
        return self.raw_status

    def has_claude_visibility_source(self, source_session_id: str) -> bool:
        self.source_checks += 1
        return source_session_id in self.open_sources

    def enqueue_claude_visibility_job(self, candidate, identity, marker_secret):
        assert marker_secret == SECRET
        self.enqueued.append((candidate, identity))
        self.open_sources.add(candidate.source_session_id)
        return {"id": identity.job_id}

    def enqueue_claude_visibility_batch_if_idle(self, items, marker_secret):
        assert marker_secret == SECRET
        open_reasons, fatal_reasons = _claude_visibility_enqueue_gates(self.raw_status)
        if fatal_reasons:
            return {
                "status": "fatal",
                "inserted": 0,
                "duplicates": 0,
                "fatal_reasons": list(fatal_reasons),
            }
        if open_reasons:
            return {"status": "open_work", "inserted": 0, "duplicates": 0}
        inserted = 0
        duplicates = 0
        for candidate, identity in items:
            if candidate.source_session_id in self.open_sources:
                duplicates += 1
                continue
            self.enqueue_claude_visibility_job(candidate, identity, marker_secret)
            inserted += 1
        return {"status": "inserted", "inserted": inserted, "duplicates": duplicates}

    def claim_claude_visibility_job(self, *args):
        self.claim_calls += 1
        return self.claim

    def record_claude_visibility_cycle(self, **record):
        self.cycle_records.append(record)


class FakeRegistrar:
    def __init__(self) -> None:
        self.claims = []

    def process(self, claim):
        self.claims.append(claim)
        return type("Outcome", (), {"status": "visible", "error_code": None})()


def _config(*, enabled: bool = True, continuous: bool = False) -> BridgeConfig:
    base = BridgeConfig()
    return replace(
        base,
        claude_visibility=replace(
            base.claude_visibility,
            enabled=enabled,
            continuous=continuous,
            emergency_daily_cost_usd=Decimal("0.50"),
            reserved_cost_per_attempt_usd=Decimal("0.02"),
        ),
    )


def _coordinator(sources, *, store=None, registrar=None, config=None):
    calls = []

    def inventory(after: float):
        calls.append(after)
        return list(sources)

    value = ClaudeVisibilityCoordinator(
        config=config or _config(),
        store=store or FakeStore(),
        inventory=inventory,
        registrar=registrar or FakeRegistrar(),
        marker_secret=SECRET,
        clock=lambda: NOW,
    )
    return value, calls


def test_discovery_is_stable_bounded_and_reports_fixed_exclusions() -> None:
    sources = [
        _source("b", active=NOW - 10),
        _source("a", active=NOW - 10),
        _source("old", active=NOW - 31 * 86400),
        _source("claude", provider=Provider.CLAUDE),
        _source("bridge", origin=OriginKind.BRIDGE_CONTINUATION),
        _source("cron", provider=Provider.HERMES, automation=True),
        _source("sub", provider=Provider.HERMES, subagent=True),
        _source("ack", text="ok"),
        _source("control", text="/resume"),
    ]
    coordinator, _calls = _coordinator(sources)

    result = coordinator.discover(days=30, limit=10)

    assert [item.candidate.source_session_id for item in result.candidates] == [
        "codex:a",
        "codex:b",
    ]
    assert {item.reason for item in result.exclusions} == {
        "outside_activity_window",
        "source_claude",
        "bridge_continuation",
        "automation_only",
        "subagent_only",
        "acknowledgement_only",
        "control_only",
    }
    assert result.degraded is False


def test_discovery_interleaves_hermes_and_codex_newest_first_stably() -> None:
    coordinator, _calls = _coordinator([
        _source("hermes-older", provider=Provider.HERMES, active=NOW - 3),
        _source("codex-newer", active=NOW - 1),
        _source("hermes-tie", provider=Provider.HERMES, active=NOW - 2),
        _source("codex-tie", active=NOW - 2),
    ])

    result = coordinator.discover(days=30, limit=10)

    assert [item.candidate.source_session_id for item in result.candidates] == [
        "codex:codex-newer",
        "codex:codex-tie",
        "hermes-tie",
        "hermes-older",
    ]


def test_discovery_reports_missing_source_cwd_without_invalidating_inventory() -> None:
    missing_cwd = replace(
        _source("missing-cwd", provider=Provider.HERMES).projection,
        cwd=None,
    )
    source = replace(
        _source("missing-cwd", provider=Provider.HERMES),
        projection=missing_cwd,
    )
    coordinator, _calls = _coordinator([source, _source("valid")])

    result = coordinator.discover(days=30, limit=10)

    assert result.degraded is False
    assert [item.candidate.source_session_id for item in result.candidates] == [
        "codex:valid"
    ]
    assert [(item.source_session_id, item.reason) for item in result.exclusions] == [
        ("hermes:missing-cwd", "source_cwd_missing")
    ]


def test_discovery_preserves_nonmeaningful_reason_when_source_cwd_is_missing() -> None:
    source = _source("empty", provider=Provider.HERMES, text="")
    source = replace(source, projection=replace(source.projection, cwd=None))
    coordinator, _calls = _coordinator([source])

    result = coordinator.discover(days=30, limit=10)

    assert result.degraded is False
    assert [(item.source_session_id, item.reason) for item in result.exclusions] == [
        ("hermes:empty", "no_meaningful_request")
    ]


def test_discovery_keeps_unknown_candidate_metadata_validation_fail_closed() -> None:
    malformed = replace(_source("bad-git-root"), git_root=" C:/work")
    coordinator, _calls = _coordinator([malformed])

    result = coordinator.discover(days=30, limit=10)

    assert result.degraded is True
    assert result.reasons == ("inventory_invalid",)
    assert result.candidates == ()
    assert result.exclusions == ()


def test_dry_run_never_writes_claims_or_invokes_registrar() -> None:
    store = FakeStore()
    registrar = FakeRegistrar()
    coordinator, _calls = _coordinator(
        [_source("one")], store=store, registrar=registrar
    )

    result = coordinator.backfill(days=30, limit=10, apply=False)

    assert result.mode == "dry_run"
    assert result.applied == 0
    assert store.enqueued == []
    assert store.claim_calls == 0
    assert registrar.claims == []


def test_imported_current_registration_is_excluded_before_dry_run_or_apply() -> None:
    original = _source("original", text="Implement the original request")
    candidate = build_claude_visibility_candidate(
        original.projection,
        eligible_at=original.projection.last_active,
        git_root=original.git_root,
        git_head=original.git_head,
        worktree_id=original.worktree_id,
    )
    identity = derive_claude_visibility_identity(candidate, SECRET)
    prompt = build_claude_registration_prompt(candidate, identity, SECRET)
    imported = _source("imported-registration", text=prompt, origin=OriginKind.NATIVE)

    for apply in (False, True):
        store = FakeStore()
        registrar = FakeRegistrar()
        coordinator, _calls = _coordinator(
            [imported], store=store, registrar=registrar
        )

        result = coordinator.backfill(days=30, limit=10, apply=apply)

        assert result.candidates == ()
        assert [(item.source_session_id, item.reason) for item in result.exclusions] == [
            ("codex:imported-registration", "bridge_placeholder")
        ]
        assert result.applied == 0
        assert store.enqueued == []
        assert store.claim_calls == 0
        assert registrar.claims == []


def test_apply_refuses_every_nonvisible_open_or_failed_state() -> None:
    for state in ("claude_pending", "claude_leased", "claude_retry", "claude_failed"):
        store = FakeStore()
        store.raw_status["counts"][state] = 1
        coordinator, _calls = _coordinator([_source("one")], store=store)

        result = coordinator.backfill(days=30, limit=10, apply=True)

        assert result.applied == 0
        assert result.open_reasons == ("open_visibility_work",)
        assert store.enqueued == []


def test_apply_hard_caps_ten_and_uses_reviewed_deterministic_enqueue() -> None:
    sources = [
        _source(f"source-{index:02d}", active=NOW - index) for index in range(15)
    ]
    store = FakeStore()
    coordinator, _calls = _coordinator(sources, store=store)

    result = coordinator.backfill(days=30, limit=999, apply=True)

    assert result.applied == 10
    assert len(store.enqueued) == 10
    assert len({identity.job_id for _, identity in store.enqueued}) == 10


def test_apply_does_not_count_or_enqueue_an_already_queued_source() -> None:
    store = FakeStore()
    store.open_sources.add("codex:existing")
    coordinator, _calls = _coordinator(
        [_source("existing"), _source("new", active=NOW - 1)], store=store
    )

    result = coordinator.backfill(days=30, limit=10, apply=True)

    assert result.applied == 1
    assert result.duplicates == 0
    assert [(item.source_session_id, item.reason) for item in result.exclusions] == [
        ("codex:existing", "duplicate_source")
    ]
    assert [candidate.source_session_id for candidate, _identity in store.enqueued] == [
        "codex:new"
    ]


def test_manual_limit_is_applied_after_queued_sources_are_excluded() -> None:
    store = FakeStore()
    sources = [
        _source(f"source-{index:02d}", active=NOW - index) for index in range(20)
    ]
    store.open_sources.update(source.source_session_id for source in sources[:10])
    coordinator, _calls = _coordinator(sources, store=store)

    result = coordinator.backfill(days=30, limit=10, apply=False)

    assert [item.candidate.source_session_id for item in result.candidates] == [
        f"codex:source-{index:02d}" for index in range(10, 20)
    ]
    assert [(item.source_session_id, item.reason) for item in result.exclusions] == [
        (f"codex:source-{index:02d}", "duplicate_source") for index in range(10)
    ]


def test_atomic_enqueue_rechecks_a_source_that_races_after_discovery() -> None:
    class RacingStore(FakeStore):
        def enqueue_claude_visibility_batch_if_idle(self, items, marker_secret):
            batch = tuple(items)
            self.open_sources.add(batch[0][0].source_session_id)
            return super().enqueue_claude_visibility_batch_if_idle(batch, marker_secret)

    store = RacingStore()
    coordinator, _calls = _coordinator(
        [_source("first", active=NOW), _source("second", active=NOW - 1)],
        store=store,
    )

    result = coordinator.backfill(days=30, limit=10, apply=True)

    assert result.applied == 1
    assert result.duplicates == 1
    assert [candidate.source_session_id for candidate, _identity in store.enqueued] == [
        "codex:second"
    ]


def test_continuous_enqueues_only_first_new_candidate() -> None:
    store = FakeStore()
    store.open_sources.add("codex:first")
    coordinator, _calls = _coordinator(
        [_source("first", active=NOW), _source("second", active=NOW - 1)],
        store=store,
        config=_config(continuous=True),
    )

    result = coordinator.continuous_once()

    assert result.applied == 1
    assert [item.candidate.source_session_id for item in result.candidates] == [
        "codex:second"
    ]
    assert [(item.source_session_id, item.reason) for item in result.exclusions] == [
        ("codex:first", "duplicate_source")
    ]
    assert store.enqueued[0][0].source_session_id == "codex:second"


def test_continuous_inventory_resumes_from_last_empty_cycle_with_overlap() -> None:
    store = FakeStore()
    store.raw_status["last_empty_cycle"] = {
        "tracked": True,
        "value": NOW - 30,
    }
    coordinator, calls = _coordinator(
        [], store=store, config=_config(continuous=True)
    )

    result = coordinator.continuous_once()

    assert result.degraded is False
    assert calls == [NOW - 150]


def test_manual_discovery_ignores_continuous_empty_cycle_cursor() -> None:
    store = FakeStore()
    store.raw_status["last_empty_cycle"] = {
        "tracked": True,
        "value": NOW - 30,
    }
    coordinator, calls = _coordinator([], store=store)

    result = coordinator.discover(days=30, limit=10)

    assert result.degraded is False
    assert calls == [NOW - 30 * 86400]


def test_continuous_discovery_uses_dedicated_fast_inventory() -> None:
    store = FakeStore()
    store.raw_status["last_empty_cycle"] = {
        "tracked": True,
        "value": NOW - 30,
    }
    full_calls: list[float] = []
    fast_calls: list[float] = []

    coordinator = ClaudeVisibilityCoordinator(
        config=_config(continuous=True),
        store=store,
        inventory=lambda after: full_calls.append(after) or [],
        continuous_inventory=lambda after: fast_calls.append(after) or [],
        registrar=FakeRegistrar(),
        marker_secret=SECRET,
        clock=lambda: NOW,
    )

    assert coordinator.continuous_once().degraded is False
    assert fast_calls == [NOW - 150]
    assert full_calls == []

    assert coordinator.discover(days=30, limit=10).degraded is False
    assert full_calls == [NOW - 30 * 86400]


def test_disabled_config_short_circuits_every_dependency() -> None:
    coordinator, calls = _coordinator([_source("one")], config=_config(enabled=False))

    result = coordinator.run_once(discover_continuous=True)

    assert result.status == "disabled"
    assert calls == []


def test_run_once_claims_once_and_calls_registrar_once_only_for_claim() -> None:
    claim = ClaudeVisibilityClaim(status="claimed", lease_kind="launch", job_id="job")
    store = FakeStore(claim=claim)
    registrar = FakeRegistrar()
    coordinator, _calls = _coordinator([], store=store, registrar=registrar)

    result = coordinator.run_once()

    assert result.status == "visible"
    assert store.claim_calls == 1
    assert registrar.claims == [claim]
    assert store.cycle_records == [
        {
            "status": "visible",
            "error_code": None,
            "registrar_result": True,
        }
    ]


def test_continuous_run_processes_open_retry_before_new_discovery() -> None:
    claim = ClaudeVisibilityClaim(status="claimed", lease_kind="launch", job_id="job")
    store = FakeStore(claim=claim)
    store.raw_status["counts"]["claude_retry"] = 1
    registrar = FakeRegistrar()
    coordinator, inventory_calls = _coordinator(
        [_source("newer")],
        store=store,
        registrar=registrar,
        config=_config(continuous=True),
    )

    result = coordinator.run_once(discover_continuous=True)

    assert result.status == "visible"
    assert inventory_calls == []
    assert store.claim_calls == 1
    assert registrar.claims == [claim]


def test_run_once_passes_configured_max_attempts_and_records_limits() -> None:
    class InspectingStore(FakeStore):
        def claim_claude_visibility_job(self, *args):
            self.claim_args = args
            return ClaudeVisibilityClaim(status="daily_limit")

    store = InspectingStore()
    config = _config()
    coordinator, _calls = _coordinator([], store=store, config=config)

    result = coordinator.run_once()

    assert result.status == "daily_limit"
    assert store.claim_args[-1] == config.claude_visibility.max_attempts
    assert store.cycle_records[-1] == {
        "status": "daily_limit",
        "error_code": None,
        "registrar_result": False,
    }


def test_run_once_cycle_persistence_failure_is_typed_provider_degraded() -> None:
    class BrokenCycleStore(FakeStore):
        def record_claude_visibility_cycle(self, **record):
            raise RuntimeError("secret database path")

    coordinator, _calls = _coordinator([], store=BrokenCycleStore())

    result = coordinator.run_once()

    assert result.status == "degraded"
    assert result.error_code == "provider_degraded"
    assert result.degraded is True
    assert "secret" not in repr(result)


def test_run_once_records_sanitized_continuous_discovery_failure() -> None:
    store = FakeStore()

    def inventory(_after: float):
        raise RuntimeError("secret provider exception")

    coordinator = ClaudeVisibilityCoordinator(
        config=_config(continuous=True),
        store=store,
        inventory=inventory,
        registrar=FakeRegistrar(),
        marker_secret=SECRET,
        clock=lambda: NOW,
    )

    result = coordinator.run_once(discover_continuous=True)

    assert result.status == "degraded"
    assert result.error_code == "provider_degraded"
    assert store.cycle_records == [
        {
            "status": "degraded",
            "error_code": "provider_degraded",
            "registrar_result": False,
        }
    ]
    assert "secret" not in repr(result)


def test_provider_exception_is_sanitized_degraded_result() -> None:
    def inventory(_after: float):
        raise RuntimeError("secret-token-123 raw provider failure")

    coordinator = ClaudeVisibilityCoordinator(
        config=_config(),
        store=FakeStore(),
        inventory=inventory,
        registrar=FakeRegistrar(),
        marker_secret=SECRET,
        clock=lambda: NOW,
    )

    result = coordinator.discover(days=30, limit=10)

    assert result.degraded is True
    assert result.reasons == ("provider_degraded",)
    assert "secret" not in repr(result)


def test_disabled_methods_touch_no_inventory_store_or_registrar() -> None:
    store = FakeStore()
    registrar = FakeRegistrar()
    coordinator, inventory_calls = _coordinator(
        [_source("one")],
        store=store,
        registrar=registrar,
        config=_config(enabled=False),
    )

    assert coordinator.discover(days=30, limit=10).enabled is False
    assert coordinator.backfill(days=30, limit=10, apply=False).enabled is False
    assert coordinator.backfill(days=30, limit=10, apply=True).enabled is False
    assert coordinator.continuous_once().enabled is False
    assert coordinator.run_once(discover_continuous=True).status == "disabled"

    assert inventory_calls == []
    assert store.status_calls == 0
    assert store.source_checks == 0
    assert store.enqueued == []
    assert store.claim_calls == 0
    assert store.cycle_records == []
    assert registrar.claims == []


def test_apply_fails_closed_on_unknown_or_malformed_status_codes() -> None:
    statuses = (
        {"retry_codes": {"new_retry_code": 1}, "failed_codes": {}},
        {"retry_codes": {}, "failed_codes": {"new_failure_code": 1}},
        {"retry_codes": [], "failed_codes": {}},
    )
    for updates in statuses:
        store = FakeStore()
        store.raw_status.update(updates)
        coordinator, _calls = _coordinator([_source("one")], store=store)

        result = coordinator.backfill(days=30, limit=10, apply=True)

        assert result.applied == 0
        assert result.degraded is True
        assert result.fatal_reasons
        assert store.enqueued == []


def test_continuous_scans_past_ten_already_queued_candidates() -> None:
    store = FakeStore()
    sources = [
        _source(f"source-{index:02d}", active=NOW - index) for index in range(11)
    ]
    store.open_sources.update(source.source_session_id for source in sources[:10])
    coordinator, _calls = _coordinator(
        sources, store=store, config=_config(continuous=True)
    )

    result = coordinator.continuous_once()

    assert result.applied == 1
    assert store.enqueued[0][0].source_session_id == "codex:source-10"


def test_continuous_scans_past_one_thousand_already_queued_candidates() -> None:
    store = FakeStore()
    sources = [
        _source(f"source-{index:04d}", active=NOW - index) for index in range(1001)
    ]
    store.open_sources.update(source.source_session_id for source in sources[:1000])
    coordinator, _calls = _coordinator(
        sources, store=store, config=_config(continuous=True)
    )

    result = coordinator.continuous_once()

    assert result.applied == 1
    assert store.enqueued[0][0].source_session_id == "codex:source-1000"


def test_discovery_malformed_item_bad_clock_and_evaluator_are_typed_degraded(
    monkeypatch,
) -> None:
    coordinator, _calls = _coordinator([object()])
    malformed = coordinator.discover(days=30, limit=10)
    assert malformed.degraded is True
    assert malformed.reasons == ("inventory_invalid",)

    bad_clock = ClaudeVisibilityCoordinator(
        config=_config(),
        store=FakeStore(),
        inventory=lambda _after: [],
        registrar=FakeRegistrar(),
        marker_secret=SECRET,
        clock=lambda: "secret-bad-clock",
    ).discover(days=30, limit=10)
    assert bad_clock.degraded is True
    assert bad_clock.reasons == ("inventory_invalid",)

    monkeypatch.setattr(
        "session_bridge.coordinator.evaluate_claude_visibility",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("secret")),
    )
    evaluator, _calls = _coordinator([_source("one")])
    result = evaluator.discover(days=30, limit=10)
    assert result.degraded is True
    assert result.reasons == ("inventory_invalid",)
    assert "secret" not in repr(result)


def test_inventory_iteration_failure_on_later_page_is_provider_degraded() -> None:
    def inventory(_after):
        def pages():
            yield _source("first")
            raise RuntimeError("later-page-secret")

        return pages()

    coordinator = ClaudeVisibilityCoordinator(
        config=_config(),
        store=FakeStore(),
        inventory=inventory,
        registrar=FakeRegistrar(),
        marker_secret=SECRET,
        clock=lambda: NOW,
    )
    result = coordinator.discover(days=30, limit=10)

    assert result.degraded is True
    assert result.reasons == ("provider_degraded",)
    assert result.candidates == ()
    assert "secret" not in repr(result)


def test_run_once_fails_closed_on_unknown_claim_and_registrar_statuses() -> None:
    unknown_claim = FakeStore(claim=ClaudeVisibilityClaim(status="future_gate"))
    coordinator, _calls = _coordinator([], store=unknown_claim)

    claim_result = coordinator.run_once()

    assert claim_result.status == "degraded"
    assert claim_result.degraded is True
    assert claim_result.fatal is True
    assert claim_result.error_code == "unknown_claim_status"

    class UnknownRegistrar(FakeRegistrar):
        def process(self, claim):
            self.claims.append(claim)
            return type(
                "Outcome", (), {"status": "future_outcome", "error_code": None}
            )()

    claimed = ClaudeVisibilityClaim(status="claimed", lease_kind="launch", job_id="job")
    store = FakeStore(claim=claimed)
    registrar = UnknownRegistrar()
    coordinator, _calls = _coordinator([], store=store, registrar=registrar)

    registrar_result = coordinator.run_once()

    assert registrar_result.status == "degraded"
    assert registrar_result.degraded is True
    assert registrar_result.fatal is True
    assert registrar_result.error_code == "unknown_registrar_status"
    assert registrar.claims == [claimed]


def test_real_store_reconciliation_lease_waits_then_recovers_at_exact_expiry(
    tmp_path,
) -> None:
    clock = [NOW]
    database = SessionDB(tmp_path / "coordinator-lease-status.db")
    store = SessionBridgeStore(
        database,
        clock=lambda: clock[0],
        local_timezone=timezone.utc,
    )
    try:
        source = _source("lease-status-recovery")
        candidate = build_claude_visibility_candidate(
            source.projection,
            eligible_at=source.projection.last_active,
            git_root=source.git_root,
            git_head=source.git_head,
            worktree_id=source.worktree_id,
        )
        identity = derive_claude_visibility_identity(candidate, SECRET)
        store.enqueue_claude_visibility_job(candidate, identity, SECRET)
        launch = store.claim_claude_visibility_job(
            NOW, 10.0, 25, "0.50", "0.02", 5
        )
        store.retry_claude_visibility_job(
            identity.job_id,
            launch.lease_digest or "",
            "creation_ambiguous",
            NOW,
            "historical launch uncertainty",
        )
        reconciliation = store.claim_claude_visibility_job(
            NOW, 10.0, 25, "0.50", "0.02", 5
        )
        assert reconciliation.lease_kind == "reconciliation"
        literal = dict(
            database._conn.execute(
                "SELECT * FROM session_claude_visibility_jobs WHERE id = ?",
                (identity.job_id,),
            ).fetchone()
        )
        usage = [
            dict(row)
            for row in database._conn.execute(
                "SELECT * FROM session_claude_registration_usage"
            ).fetchall()
        ]
        registrar = FakeRegistrar()
        coordinator = ClaudeVisibilityCoordinator(
            config=_config(continuous=True),
            store=store,
            inventory=lambda _after: [],
            registrar=registrar,
            marker_secret=SECRET,
            clock=lambda: clock[0],
        )

        waiting = coordinator.run_once(discover_continuous=True)

        assert waiting.status == "no_due_job"
        assert registrar.claims == []
        assert dict(
            database._conn.execute(
                "SELECT * FROM session_claude_visibility_jobs WHERE id = ?",
                (identity.job_id,),
            ).fetchone()
        ) == literal
        assert [
            dict(row)
            for row in database._conn.execute(
                "SELECT * FROM session_claude_registration_usage"
            ).fetchall()
        ] == usage

        clock[0] = NOW + 10.0
        recovered = coordinator.run_once(discover_continuous=True)

        assert recovered.status == "visible"
        assert len(registrar.claims) == 1
        recovered_claim = registrar.claims[0]
        assert recovered_claim.job_id == identity.job_id
        assert recovered_claim.reserved_claude_uuid == identity.claude_uuid
        assert recovered_claim.lease_kind == "reconciliation"
        assert recovered_claim.prior_error_code == "lease_expired"
        assert recovered_claim.registration_reserved is False
        assert recovered_claim.launch_permitted is False
        assert recovered_claim.attempt_ordinal == launch.attempt_ordinal == 1
        assert [
            dict(row)
            for row in database._conn.execute(
                "SELECT * FROM session_claude_registration_usage"
            ).fetchall()
        ] == usage
    finally:
        database.close()


def test_run_once_blocks_before_claim_on_unknown_persisted_job_state() -> None:
    store = FakeStore()
    store.raw_status["fatal"] = [
        {
            "code": "unknown_job_state",
            "state": "future_state",
            "error_code": "future-code",
            "count": 1,
        }
    ]
    coordinator, _calls = _coordinator([], store=store)

    result = coordinator.run_once()

    assert result.status == "degraded"
    assert result.error_code == "unknown_job_state"
    assert result.fatal is True
    assert store.claim_calls == 0


def test_claim_failure_is_logged_with_its_cause(caplog) -> None:
    """A raising claim must name itself in the log, not just record claim_failed.

    This handler swallowed every claim exception silently. A real UNIQUE
    collision in session_claude_registration_usage therefore livelocked the
    lane for a week: each 60s cycle rolled back, recorded ``claim_failed``,
    and logged nothing at all, so the cause was invisible from the outside.
    The sibling handler for the enqueue gates already logs; this asserts the
    claim handler does too. The public result is deliberately unchanged.
    """

    class ExplodingStore(FakeStore):
        def claim_claude_visibility_job(self, *args):
            raise RuntimeError("UNIQUE constraint failed: secret database path")

    coordinator, _calls = _coordinator([], store=ExplodingStore())

    with caplog.at_level(logging.WARNING, logger="session_bridge.coordinator"):
        result = coordinator.run_once()

    assert result.status == "degraded"
    assert result.error_code == "claim_failed"
    assert result.degraded is True
    # The cause reaches the operator log, naming its own gate...
    logged = [
        record.getMessage()
        for record in caplog.records
        if "claude_visibility_discovery_degraded" in record.getMessage()
    ]
    assert len(logged) == 1
    assert "stage=claim" in logged[0]
    assert "RuntimeError" in logged[0]
    assert "UNIQUE constraint failed" in logged[0]
    # ...and still never reaches public output.
    assert "secret" not in repr(result)
