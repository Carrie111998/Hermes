"""Candidates are verified concurrently; everything else stays serial.

Three fetches per candidate at a 45-second timeout, one candidate at a time,
made a 150-candidate market roughly 450 sequential round trips — the dominant
wall-clock cost in the system.

Concurrency is confined to the network. Every database write, identity
resolution above all, still happens on the campaign's own thread and in
candidate order: resolution reads before it writes, so two candidates resolving
to the same company have to arrive one after the other.
"""
from __future__ import annotations

import json
import threading

from server.db import Database, json_dump, now
from server.lead_research.candidates import CandidateRepository
from server.lead_research.models import (
    CampaignConfig,
    DatasetDefinition,
    ProviderHealth,
    VerificationBundle,
    VerificationSource,
)
from server.lead_research.registry import ProviderRegistry
from server.lead_research.service import LeadResearchService


class ConcurrencyWatchingVerifier:
    """Records how many verifies were ever in flight at once."""

    def __init__(self, definition, gate: threading.Barrier | None = None):
        self.definition = definition
        self.gate = gate
        self.peak = 0
        self.live = 0
        self.order: list[str] = []
        self.threads: set[int] = set()
        self._lock = threading.Lock()

    def health(self):
        return ProviderHealth(status="active")

    def verify(self, query, candidate):
        del query
        with self._lock:
            self.live += 1
            self.peak = max(self.peak, self.live)
            self.order.append(candidate.source_record_id)
            self.threads.add(threading.get_ident())
        try:
            if self.gate is not None:
                # Only returns once `parties` verifies are inside at the same
                # time, so a serial implementation deadlocks instead of quietly
                # passing.
                self.gate.wait(timeout=10)
            return VerificationBundle(
                candidate_source_record_id=candidate.source_record_id,
                sources=[VerificationSource(
                    provenance_url=f"https://registry.example/{candidate.source_record_id}",
                    raw_hash="d" * 64,
                    classification="independent",
                    retrieved_via="https://search.example",
                    facts={
                        "company_name": [candidate.company_name],
                        "country": [candidate.country],
                        "buyer_role": ["distributor"],
                    },
                )],
                independent_source_count=1,
                requests=1,
            )
        finally:
            with self._lock:
                self.live -= 1


def _definition(source_id="fast-source", max_concurrency=4) -> DatasetDefinition:
    return DatasetDefinition(
        source_id=source_id,
        display_name=source_id,
        publisher="Tests",
        access_tier="public",
        entity_levels=["named_company"],
        capabilities=["candidate_verification"],
        emits=["company_name", "country", "buyer_role"],
        max_concurrency=max_concurrency,
        adapter_mode="live",
        default_enabled=True,
    )


def _harness(tmp_path, candidates=8) -> Database:
    db = Database(tmp_path / "concurrency.db")
    stamp = now()
    db.execute(
        "INSERT INTO companies(id,name,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        ("cmp_1", "Tenant", "active", "{}", stamp, stamp),
    )
    CandidateRepository(db).import_file(
        "buyers", "1", "candidates.jsonl",
        b"\n".join(
            json.dumps({
                "source_record_id": f"buyer-de-{index:02d}",
                "company_name": f"Buyer {index} DE",
                "country": "DE",
                "categories": ["household-appliances"],
            }).encode()
            for index in range(candidates)
        ),
    )
    return db


def _run(db, definitions, providers, *, verify_workers=4, campaign_id="camp_1"):
    config = CampaignConfig(
        name="German appliance distributors",
        target_countries=["DE"],
        sector_ids=["household-appliances"],
        buyer_types=["distributor"],
        enabled_source_ids=[definition.source_id for definition in definitions],
        refresh={"schedule": "monthly", "reuse_public_cache": False},
    )
    stamp = now()
    db.execute(
        "INSERT INTO research_campaigns VALUES(?,?,?,?,?,?,?,?,?,?)",
        (campaign_id, "cmp_1", config.name, "draft", 1,
         json_dump(config.model_dump(mode="json")), None, None, stamp, stamp),
    )
    service = LeadResearchService(
        db, registry=ProviderRegistry(definitions, providers), verify_workers=verify_workers,
    )
    return service.run("cmp_1", campaign_id)


# ── the concurrency is real ───────────────────────────────────────────────────

def test_candidates_in_a_batch_are_verified_at_the_same_time(tmp_path):
    """The regression this file exists for.

    The barrier only releases once four verifies are inside it together, so a
    sequential implementation cannot get past it.
    """
    definition = _definition()
    verifier = ConcurrencyWatchingVerifier(definition, gate=threading.Barrier(4))
    db = _harness(tmp_path, candidates=8)

    result = _run(db, [definition], {definition.source_id: verifier}, verify_workers=4)

    assert result["status"] == "succeeded", result
    assert verifier.peak == 4
    assert len(verifier.threads) > 1


def test_one_worker_restores_strictly_serial_verification(tmp_path):
    definition = _definition()
    verifier = ConcurrencyWatchingVerifier(definition)
    db = _harness(tmp_path, candidates=4)

    _run(db, [definition], {definition.source_id: verifier}, verify_workers=1)

    assert verifier.peak == 1
    assert verifier.order == [f"buyer-de-{index:02d}" for index in range(4)]


# ── the per-source cap ────────────────────────────────────────────────────────

def test_a_source_that_declares_one_at_a_time_is_never_hit_concurrently(tmp_path):
    """TED answers 429 readily, so concurrency there breaks a working source.

    The cap is a property of the upstream, declared in the provider catalog, and
    it has to hold even when the run itself is verifying four candidates at once.
    """
    serial = _definition("serial-source", max_concurrency=1)
    parallel = _definition("parallel-source", max_concurrency=4)
    serial_verifier = ConcurrencyWatchingVerifier(serial)
    # A barrier, not a timing observation: with instantaneous fakes the workers
    # stagger behind the serial gate and never happen to overlap, which would
    # let this pass or fail on luck. Requiring two to meet proves the capped
    # source does not serialise the uncapped one behind it.
    parallel_verifier = ConcurrencyWatchingVerifier(parallel, gate=threading.Barrier(2))
    db = _harness(tmp_path, candidates=8)

    _run(
        db, [serial, parallel],
        {serial.source_id: serial_verifier, parallel.source_id: parallel_verifier},
        verify_workers=4,
    )

    assert serial_verifier.peak == 1, "a source capped at one was hit concurrently"
    assert parallel_verifier.peak > 1, "the uncapped source was not parallelised"


def test_the_cap_is_shared_across_campaigns_not_reset_per_run(tmp_path):
    """The limit protects the upstream, so it cannot be per-campaign."""
    definition = _definition("serial-source", max_concurrency=1)
    verifier = ConcurrencyWatchingVerifier(definition)
    db = _harness(tmp_path, candidates=4)
    service = LeadResearchService(
        db,
        registry=ProviderRegistry([definition], {definition.source_id: verifier}),
        verify_workers=4,
    )
    first = service._source_gate(definition.source_id)

    assert service._source_gate(definition.source_id) is first


# ── what must stay deterministic ──────────────────────────────────────────────

def test_results_are_written_in_candidate_order_however_they_arrive(tmp_path):
    """Concurrency must not reach the parts of a run that have to reproduce.

    Results are rebuilt from scratch each run, so their creation order decides
    the identity of rows a refresh preserves.
    """
    definition = _definition()
    verifier = ConcurrencyWatchingVerifier(definition)
    db = _harness(tmp_path, candidates=8)

    _run(db, [definition], {definition.source_id: verifier}, verify_workers=4)

    names = [
        row["company_name"] for row in db.all(
            "SELECT company_name FROM leads WHERE company_id='cmp_1' ORDER BY created_at,id"
        )
    ]
    assert names == sorted(names, key=lambda name: int(name.split()[1]))


def test_a_concurrent_run_reaches_the_same_result_as_a_serial_one(tmp_path):
    """The only difference concurrency is allowed to make is how long it takes."""
    definition = _definition()

    serial_db = _harness(tmp_path / "serial", candidates=8)
    _run(
        serial_db, [definition],
        {definition.source_id: ConcurrencyWatchingVerifier(definition)},
        verify_workers=1,
    )
    concurrent_db = _harness(tmp_path / "concurrent", candidates=8)
    _run(
        concurrent_db, [definition],
        {definition.source_id: ConcurrencyWatchingVerifier(definition)},
        verify_workers=4,
    )

    def snapshot(db):
        return [
            (row["verdict"], row["fit_score"], row["evidence_confidence"])
            for row in db.all(
                "SELECT verdict,fit_score,evidence_confidence FROM research_results "
                "WHERE company_id='cmp_1' ORDER BY organization_id"
            )
        ]

    assert snapshot(serial_db) == snapshot(concurrent_db)
    assert snapshot(serial_db), "the fixture produced nothing to compare"


def test_one_candidate_failing_does_not_take_its_batch_down(tmp_path):
    class Erratic(ConcurrencyWatchingVerifier):
        def verify(self, query, candidate):
            if candidate.source_record_id == "buyer-de-02":
                raise RuntimeError("upstream refused")
            return super().verify(query, candidate)

    definition = _definition()
    db = _harness(tmp_path, candidates=8)

    result = _run(db, [definition], {definition.source_id: Erratic(definition)}, verify_workers=4)

    assert result["metrics"]["resolved_organizations"] == 7
    assert result["status"] == "partial"


def test_spend_is_still_attributed_correctly_under_concurrency(tmp_path):
    """Counters are applied on the campaign thread, so none can be lost."""
    definition = _definition()
    db = _harness(tmp_path, candidates=8)

    result = _run(
        db, [definition],
        {definition.source_id: ConcurrencyWatchingVerifier(definition)},
        verify_workers=4,
    )

    assert result["metrics"]["provider_requests"] == 8
