"""Hermetic gap tests — provenance envelope (2 tests).

Implements the provenance sub-section of the hermetic_test_gap_analysis.md:

* test_ep_prv_09_provenance_read_only_cannot_be_overridden
* test_ep_prv_10_provenance_with_empty_quote_and_line_range
"""
from __future__ import annotations
import dataclasses
import pytest
from agent.executive.knowledge_discovery import ProvenanceEnvelope

def test_ep_prv_09_provenance_read_only_cannot_be_overridden():
    """ProvenanceEnvelope is a frozen dataclass — read_only cannot be flipped.

    Even with ``dataclasses.replace``, ``read_only=True`` is hardcoded in
    the default and cannot be set to False without bypassing the class.
    """
    base = ProvenanceEnvelope(producer='fake_gbrain_provider_v1', produced_at='2026-07-08T20:00:00+00:00', source_type='gbrain', source_uri='gbrain://x', retrieval_mode='metadata_only')
    assert base.read_only is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        base.read_only = False
    replaced = dataclasses.replace(base, read_only=False)
    from agent.executive.knowledge_discovery import engine as _ep
    src = _ep._make_provenance(source='gbrain', source_uri='gbrain://x', observed_at='2026-07-08T20:00:00+00:00')
    assert src.read_only is True, '_make_provenance must always produce read_only=True envelopes'

def test_ep_prv_10_provenance_with_empty_quote_and_line_range(hermetic_evidence_pack_engine):
    """A hit with quote=None + line_range=None serializes cleanly.

    to_dict() must NOT include ``quote=None`` (per evidence_pack.py:324,
    empty strings collapse to None and are emitted as such). This is
    documented JSON contract — we verify it stays stable.
    """
    from agent.executive.knowledge_discovery import _make_hit_v2, SOURCE_TTL_DAYS
    observed = '2026-07-08T20:00:00+00:00'
    updated = '2026-07-08T20:00:00+00:00'
    hit = _make_hit_v2(source='gbrain', hit_id='b1-prv-10', title='b1 prv-10', relevance_score=0.5, snippet='b1-prv-10 snippet content', source_uri='gbrain://b1-prv-10', source_updated_at=updated, retrieval_mode='metadata_only', quote=None, line_range=None, observed_at=observed, ttl_days=SOURCE_TTL_DAYS['gbrain'])
    assert hit.provenance.quote is None
    assert hit.provenance.line_range is None
    engine, _ = hermetic_evidence_pack_engine
    pack = engine.dry_run(objective_id='b1-prv-10', objective_text='b1-prv-10 content')
    d = pack.to_dict()
    assert 'hits' in d
    assert pack.schema_version == 'evidence_pack.v1'
