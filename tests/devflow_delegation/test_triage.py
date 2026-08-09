"""Deterministic triage: select REQUESTED rows and drive REQUESTED -> TRIAGED.

Triage is the missing programmatic driver of the already-wired REQUESTED->TRIAGED
lifecycle edge. It is pure-selection + a thin transition driver: no model calls,
no side effects beyond the audited lifecycle.transition (ledger state + history +
telemetry). It must be idempotent/re-runnable and never raise for a row that has
already advanced.
"""
import json

from devflow_delegation import cli
from devflow_delegation.emitter import DelegationEmitter
from devflow_delegation.triage import run_triage, triage_order
from tests.devflow_delegation.conftest import make_delegate_kwargs


def _queue_all(hermes_root):
    (hermes_root / "devflow" / "policy.json").write_text(
        json.dumps({"explicit": {"mode": "queue"}, "critic": {"mode": "queue"},
                    "arch-review": {"mode": "queue"}}),
        encoding="utf-8")


def test_triage_order_is_deterministic_severity_priority_age():
    rows = [
        {"request_id": "c", "severity": "low", "created_at": "2026-08-01T00:00:00+00:00",
         "envelope_json": json.dumps({"priority": "P3"})},
        {"request_id": "a", "severity": "high", "created_at": "2026-08-02T00:00:00+00:00",
         "envelope_json": json.dumps({"priority": "P1"})},
        {"request_id": "b", "severity": "high", "created_at": "2026-08-01T00:00:00+00:00",
         "envelope_json": json.dumps({"priority": "P0"})},
    ]
    ordered = [r["request_id"] for r in triage_order(rows)]
    # high beats low; within high, P0 (b) before P1 (a); age breaks final ties
    assert ordered == ["b", "a", "c"]


def test_triage_order_is_pure_no_mutation():
    rows = [{"request_id": "x", "severity": "high", "created_at": "2026-08-01T00:00:00+00:00",
             "envelope_json": "{}"}]
    before = json.dumps(rows, sort_keys=True)
    triage_order(rows)
    assert json.dumps(rows, sort_keys=True) == before


def test_run_triage_advances_requested_to_triaged(hermes_root, allowlist_file):
    _queue_all(hermes_root)
    em = DelegationEmitter()
    r1 = em.delegate(**make_delegate_kwargs(title="Alpha"))
    r2 = em.delegate(**make_delegate_kwargs(
        source={"agent": "roadmap-intake", "kind": "arch-review", "finding_id": "SR-1"},
        title="Bravo", idempotency_key="roadmap:sr-1:v1", severity="medium", priority="P1"))
    assert r1.status == "queued" and r2.status == "queued"

    result = run_triage(em.ledger, em.bus)
    assert result["triaged"] == 2
    assert result["errors"] == 0
    for rid in (r1.request_id, r2.request_id):
        assert em.ledger.get_request(rid)["state"] == "TRIAGED"
        assert [t["to_state"] for t in em.ledger.transitions_for(rid)] == ["TRIAGED"]

    types = [e["event_type"] for e in em.bus._get_conn().execute(
        "SELECT event_type FROM events ORDER BY timestamp").fetchall()]
    assert types.count("devflow.work_triaged") == 2


def test_run_triage_is_idempotent(hermes_root, allowlist_file):
    _queue_all(hermes_root)
    em = DelegationEmitter()
    em.delegate(**make_delegate_kwargs(title="Only"))

    first = run_triage(em.ledger, em.bus)
    second = run_triage(em.ledger, em.bus)
    assert first["triaged"] == 1
    assert second["triaged"] == 0 and second["considered"] == 0
    # a second pass emits no additional telemetry
    n = em.bus._get_conn().execute(
        "SELECT COUNT(*) AS n FROM events WHERE event_type='devflow.work_triaged'"
    ).fetchone()["n"]
    assert n == 1


def test_reconcile_then_triage_creates_one_durable_human_approval_gate(hermes_root, allowlist_file):
    """A controlled v3 request is adopted once and stops at TRIAGED."""
    _queue_all(hermes_root)
    em = DelegationEmitter()
    request = em.delegate(
        **make_delegate_kwargs(
            source={"agent": "roadmap-intake", "kind": "arch-review", "finding_id": "SR-500"},
            idempotency_key="roadmap:sr-500:v1",
        )
    )
    assert request.status == "queued"

    reconciliation = em.reconcile()
    first = run_triage(em.ledger, em.bus)
    second = run_triage(em.ledger, em.bus)

    assert reconciliation == {"adopted": 0, "rewritten": 0}
    assert first == {"considered": 1, "triaged": 1, "errors": 0}
    assert second == {"considered": 0, "triaged": 0, "errors": 0}
    assert em.ledger.summary_counts() == {
        "total": 1,
        "by_state": {"TRIAGED": 1},
        "by_source": {"roadmap-intake": 1},
    }
    assert [transition["to_state"] for transition in em.ledger.transitions_for(request.request_id)] == ["TRIAGED"]


def test_run_triage_empty_ledger_is_noop(hermes_root, allowlist_file):
    em = DelegationEmitter()
    result = run_triage(em.ledger, em.bus)
    assert result == {"triaged": 0, "errors": 0, "considered": 0}


def test_run_triage_respects_limit(hermes_root, allowlist_file):
    _queue_all(hermes_root)
    em = DelegationEmitter()
    for i in range(4):
        em.delegate(**make_delegate_kwargs(title=f"Item {i}"))
    result = run_triage(em.ledger, em.bus, limit=2)
    assert result["considered"] == 2 and result["triaged"] == 2
    assert em.ledger.summary_counts()["by_state"].get("REQUESTED") == 2
    assert em.ledger.summary_counts()["by_state"].get("TRIAGED") == 2


def test_triage_cli_subcommand(hermes_root, allowlist_file, capsys):
    _queue_all(hermes_root)
    em = DelegationEmitter()
    em.delegate(**make_delegate_kwargs(title="ViaCLI"))
    em.ledger.close()  # release WAL handle before the CLI re-opens the ledger

    rc = cli.main(["triage"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "triaged=1" in out


def test_triage_bus_none_still_advances_state(hermes_root, allowlist_file):
    """A dry tooling path may pass bus=None; state must still advance with no
    telemetry (mirrors lifecycle.transition's bus=None contract)."""
    _queue_all(hermes_root)
    em = DelegationEmitter()
    r = em.delegate(**make_delegate_kwargs(title="NoBus"))
    result = run_triage(em.ledger, None)
    assert result["triaged"] == 1
    assert em.ledger.get_request(r.request_id)["state"] == "TRIAGED"
