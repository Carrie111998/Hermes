from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from session_bridge.claude_visibility import ClaudeVisibilityClaim
from session_bridge.config import BridgeConfig
from session_bridge.coordinator import ClaudeVisibilityCoordinator
from session_bridge.models import OriginKind, ProjectedMessage, Provider, SessionProjection
from session_bridge.store import SidebarSource


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

    def claim_claude_visibility_job(self, *args):
        self.claim_calls += 1
        return self.claim


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


def test_dry_run_never_writes_claims_or_invokes_registrar() -> None:
    store = FakeStore()
    registrar = FakeRegistrar()
    coordinator, _calls = _coordinator([_source("one")], store=store, registrar=registrar)

    result = coordinator.backfill(days=30, limit=10, apply=False)

    assert result.mode == "dry_run"
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
    sources = [_source(f"source-{index:02d}", active=NOW - index) for index in range(15)]
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
    assert [candidate.source_session_id for candidate, _identity in store.enqueued] == [
        "codex:new"
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
    assert store.enqueued[0][0].source_session_id == "codex:second"


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


def test_provider_exception_is_sanitized_degraded_result() -> None:
    def inventory(_after: float):
        raise RuntimeError("secret-token-123 raw provider failure")

    coordinator = ClaudeVisibilityCoordinator(
        config=_config(), store=FakeStore(), inventory=inventory,
        registrar=FakeRegistrar(), marker_secret=SECRET, clock=lambda: NOW,
    )

    result = coordinator.discover(days=30, limit=10)

    assert result.degraded is True
    assert result.reasons == ("source_discovery_failed",)
    assert "secret" not in repr(result)


def test_disabled_methods_touch_no_inventory_store_or_registrar() -> None:
    store = FakeStore()
    registrar = FakeRegistrar()
    coordinator, inventory_calls = _coordinator(
        [_source("one")], store=store, registrar=registrar,
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
    sources = [_source(f"source-{index:02d}", active=NOW - index) for index in range(11)]
    store.open_sources.update(source.source_session_id for source in sources[:10])
    coordinator, _calls = _coordinator(
        sources, store=store, config=_config(continuous=True)
    )

    result = coordinator.continuous_once()

    assert result.applied == 1
    assert store.enqueued[0][0].source_session_id == "codex:source-10"


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
            return type("Outcome", (), {"status": "future_outcome", "error_code": None})()

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
