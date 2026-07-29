"""Hermetic gap tests — no-op / rollback / audit (2 tests).

Implements:

* test_no_op_05_rollback_after_partial_provider_failure
* test_audit_nomut_05_multi_conflict_appends_one_event_per_high
"""
from __future__ import annotations

def test_no_op_05_rollback_after_partial_provider_failure(b1_engine_with_partial_provider_failure):
    """discover() with one failing provider + one OK → rollback() returns
    True (deleted 1 key), then False on a second call (idempotent).
    """
    engine = b1_engine_with_partial_provider_failure
    pack = engine.discover(objective_id='b1-noop-05', objective_text='discovery partial content')
    assert 'obsidian' in pack.sources_failed, pack.sources_failed
    assert 'gbrain' in pack.sources_queried, pack.sources_queried
    assert len(pack.hits) >= 1
    assert engine.rollback('b1-noop-05') is True
    assert engine.rollback('b1-noop-05') is False

def test_audit_nomut_05_multi_conflict_appends_one_event_per_high(b1_engine_with_three_policy_vs_obsidian_conflicts):
    """Pairwise detection with N policy + N obsidian hits → N×N high conflicts
    → exactly N×N audit events with gate_type='knowledge_conflict'
    severity='high'. The contract is "1 audit event per high conflict" —
    independent of whether the conflict came from pairwise enumeration.
    """
    engine = b1_engine_with_three_policy_vs_obsidian_conflicts
    pack = engine.dry_run(objective_id='b1-audit-nomut-05', objective_text='discovery multi conflict alpha beta')
    high = [c for c in pack.conflicts if c.severity == 'high']
    assert all((c.conflict_type == 'policy_vs_goal' for c in high)), f'unexpected conflict_type in high set: {[c.conflict_type for c in high]}'
    assert len(high) == 9, f'expected 9 high-severity conflicts (3×3 pairwise), got {len(high)}'
    events = engine._audit_sink.get_events()
    pvg_events = [e for e in events if e.get('gate_type') == 'knowledge_conflict' and e.get('severity') == 'high']
    assert len(pvg_events) == 9, f'expected 9 audit events (1 per high conflict), got {len(pvg_events)}: {pvg_events}'
