"""Deterministic context-health audit contracts."""

from __future__ import annotations


def _candidate(candidate_id: str, status: str = "active") -> dict:
    return {
        "candidate_id": candidate_id,
        "subsystem": "skills",
        "action": "patch",
        "status": status,
        "payload_fingerprint": f"sha256:{candidate_id}",
        "dedup_key": f"sha256:{candidate_id}",
        "proposal": {"summary": "Improve workflow"},
        "source": {"origin": "background_review"},
        "evidence": {"status": "captured", "risk": "medium"},
        "precondition": {},
    }


def test_audit_finds_duplicate_memory_and_unvalidated_learning(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    memories = home / "memories"
    memories.mkdir(parents=True)
    (memories / "MEMORY.md").write_text(
        "Project uses pytest\n§\nProject uses pytest\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    from agent import learning_ledger
    from agent.context_health import audit_context

    learning_ledger.create_candidate(_candidate("candidate-1"))
    result = audit_context()

    kinds = {finding["kind"] for finding in result["findings"]}
    assert "duplicate_memory" in kinds
    assert "unvalidated_learning" in kinds
    assert 0 <= result["score"] < 100


def test_failed_outcome_is_reported_as_quality_risk(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from agent import learning_ledger
    from agent.context_health import audit_context

    learning_ledger.create_candidate(_candidate("candidate-1"))
    learning_ledger.record_outcome(
        "candidate-1", "verification_failed", detail={"reason": "fixture failed"}
    )

    result = audit_context()

    finding = next(item for item in result["findings"] if item["kind"] == "failed_learning_outcome")
    assert finding["subject"] == "candidate-1"
    assert finding["severity"] == "high"


def test_audit_text_is_shared_cli_friendly(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from agent.context_health import format_context_audit

    text = format_context_audit()

    assert text.startswith("Context Health:")
    assert "score=" in text
