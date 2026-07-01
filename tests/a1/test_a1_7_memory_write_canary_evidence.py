"""A1.7 memory write sink canary evidence test.

Verifies the cross-session persistence boundary guard works correctly
without invoking live memory writes, disk I/O, or real provider dispatch.
"""

from __future__ import annotations

import json
from pathlib import Path

from hermes_cli.a1_7_memory_write_canaries import run_a1_7_memory_write_canaries


def test_a1_7_memory_write_canary_evidence() -> None:
    """Execute A1.7 canaries and validate the generated evidence artifacts.

    Assertions:
    - Exactly 5 deterministic cases run (T01..T05)
    - Exactly 3 denied, exactly 2 allowed per documented expectations
    - All evidence rows contain required envelope fields
    - Every denial is attributed to the correct A1.7 guard, not a different subsystem
    - Zero live memory writes occurred (synthetic-only envelope)
    - Zero provider calls (no outbound traffic)
    - Zero raw content stored (digest-only)
    - No profile/provider/runtime configuration mutated
    """
    result = run_a1_7_memory_write_canaries()

    # Envelope totals match fixed case count
    assert result.total == 5, f"expected 5 canary cases, got {result.total}"

    # Fixed decision distribution per documented A1.7 expectations
    assert result.denied == 3, (
        f"expected 3 denied cases (T01 missing-paths, T04 missing-taint, "
        f"T05 user-only-MEMORY), got {result.denied}"
    )
    assert result.allowed == 2, (
        f"expected 2 allowed cases (T02 C2-with-memory-path, T03 C0), "
        f"got {result.allowed}"
    )

    # Zero live side-effects — synthetic-envelope invariant
    assert result.live_memory_write_count == 0, (
        "live memory write detected — A1.7 canaries must not touch disk"
    )
    assert result.provider_call_count == 0, (
        "provider call detected — A1.7 canaries must not dispatch live models"
    )
    assert not result.live_config_touched, (
        "profile/provider/runtime config was mutated by canaries"
    )
    assert not result.secret_values_read, (
        "secrets were inspected by canaries"
    )
    assert not result.raw_payload_stored, (
        "raw payload content was written — canaries must remain digest-only"
    )
    assert not result.raw_memory_content_stored, (
        "raw memory content was written — canaries must remain digest-only"
    )

    # Evidence artifact produced and readable
    evidence_path = Path(result.evidence_path)
    assert evidence_path.exists(), (
        f"evidence artifact missing at {evidence_path}"
    )
    assert evidence_path.stat().st_size > 0, (
        "evidence artifact is empty — canaries produced no output"
    )

    # Rows individually validate
    with open(evidence_path, "r", encoding="utf-8") as fp:
        raw = fp.read()
    assert raw.strip(), "evidence file contains no rows"

    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    assert len(rows) == 5, f"expected 5 evidence rows, got {len(rows)}"

    # Required envelope fields per A1 evidence schema
    required_fields = {
        "case_id", "decision", "reason",
        "classification", "classification_source",
        "target", "allowed_paths",
        "live_memory_write_count",
        "provider_call_count",
        "live_config_touched", "secret_values_read",
        "raw_payload_stored", "raw_memory_content_stored",
    }
    for row in rows:
        missing = required_fields - set(row.keys())
        assert not missing, (
            f"row {row.get('case_id', '?')} missing fields: {sorted(missing)}"
        )

    # Fixed-case IDs and per-case decision assertions
    by_id = {r["case_id"]: r for r in rows}
    assert set(by_id.keys()) == {
        "A1.7-T01", "A1.7-T02", "A1.7-T03", "A1.7-T04", "A1.7-T05",
    }

    # T01: C2 without allowed_paths must be denied with correct reason
    t01 = by_id["A1.7-T01"]
    assert t01["decision"] == "denied"
    assert t01["classification"] == "C2"
    assert t01["target"] == "memory"
    assert "hl_aos_allowed_paths" in t01["reason"], (
        f"T01 denial reason must mention allowed_paths, got: {t01['reason']}"
    )

    # T02: C2 with memory dir in allowed_paths must be allowed
    t02 = by_id["A1.7-T02"]
    assert t02["decision"] == "allowed"
    assert t02["classification"] == "C2"
    assert t02["target"] == "memory"
    assert len(t02["allowed_paths"]) == 1

    # T03: C0 agent allowed without restrictions
    t03 = by_id["A1.7-T03"]
    assert t03["decision"] == "allowed"
    assert t03["classification"] == "C0"
    assert t03["target"] == "memory"

    # T04: missing taint must be denied — fail-closed invariant
    t04 = by_id["A1.7-T04"]
    assert t04["decision"] == "denied"
    assert t04["classification"] == ""
    assert "no HL-AOS classification" in t04["reason"], (
        f"T04 denial reason must attribute to missing HL-AOS taint, got: {t04['reason']}"
    )

    # T05: C2 write to USER.md denied when only MEMORY.md is allowed
    t05 = by_id["A1.7-T05"]
    assert t05["decision"] == "denied"
    assert t05["classification"] == "C2"
    assert t05["target"] == "user"
    assert "USER.md" in t05["reason"], (
        f"T05 denial reason must mention USER.md, got: {t05['reason']}"
    )

    # Summary artifact also readable and consistent
    summary_path = Path(result.summary_path)
    assert summary_path.exists(), (
        f"summary artifact missing at {summary_path}"
    )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["total_cases"] == 5
    assert summary["denied"] == 3
    assert summary["allowed"] == 2
    assert summary["live_memory_write_count"] == 0
    assert summary["provider_call_count"] == 0
