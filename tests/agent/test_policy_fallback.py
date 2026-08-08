import time

from agent.policy_fallback import (
    Store,
    classify,
    maybe_run,
    normalize_question,
    question_hashes,
)
from agent.policy_fallback import runtime_scope
from agent.policy_tool_policy import enforce


def test_policy_sources_are_distinct():
    provider = classify({"error": "content_policy_blocked: rejected"}, "goal")
    model = classify({"final_response": '{"status":"partial","reason_code":"policy_restriction","remaining":["part"]}'}, "goal")
    assert provider["source"] == "provider_policy_block"
    assert model["source"] == "model_reported_policy"
    assert classify({"error": "rate_limit: retry"}, "goal") is None


def test_question_hash_includes_evidence_and_normalizes_urls():
    assert normalize_question(" HTTPS://Example.COM/a?tracking=1 ") == "https://example.com/a"
    _, normalized_a, evidence_a, question_a = question_hashes("Question", [{"source": "a"}])
    _, normalized_b, evidence_b, question_b = question_hashes("question", [{"source": "b"}])
    assert normalized_a == normalized_b
    assert evidence_a != evidence_b
    assert question_a != question_b


def test_store_claim_is_idempotent_and_stale_worker_fails(tmp_path):
    store = Store(tmp_path / "fallback.db")
    row = store.create("parent", "goal", "blocked", "provider_policy_block", "reason", [], [])
    assert store.claim(row["fallback_id"], "worker-one") is True
    assert store.claim(row["fallback_id"], "worker-two") is False
    with store.db() as db:
        db.execute("UPDATE fallbacks SET lease_expires_at=? WHERE fallback_id=?", (time.time()-1, row["fallback_id"]))
    assert store.fail_stale() == 1
    with store.db() as db:
        current = db.execute("SELECT state,error FROM fallbacks WHERE fallback_id=?", (row["fallback_id"],)).fetchone()
    assert tuple(current) == ("failed", "worker_lost")


def test_passed_verification_requires_evidence(tmp_path, monkeypatch):
    store = Store(tmp_path / "fallback.db")
    row = store.create("parent", "goal", "blocked", "model_reported_policy", "reason", [], [])
    monkeypatch.setattr("agent.policy_fallback.embedding", lambda _text: b"\0\0\0\0")
    consultation, error = store.reserve_consultation(row["fallback_id"], "low_confidence", "question", [], "")
    assert not error
    try:
        store.verify(consultation["consultation_id"], "passed", "looks good", [])
    except ValueError as exc:
        assert "requires" in str(exc)
    else:
        raise AssertionError("passed verification accepted without evidence")
    result = store.verify(consultation["consultation_id"], "inconclusive", "not enough data", [])
    assert result["verification_result"]["status"] == "inconclusive"


def test_untrusted_worker_tool_policy_blocks_infrastructure():
    with runtime_scope({"fallback_id": "test"}):
        assert enforce("terminal", {"command": "curl https://porx.local:8006/api2/json"})
        assert enforce("terminal", {"command": "ssh external.example"})
        assert enforce("terminal", {"command": "python local_check.py"}) is None


def test_policy_fallback_merges_qwen_result(tmp_path, monkeypatch):
    import agent.policy_fallback as module
    import tools.delegate_tool as delegate

    monkeypatch.setattr(module, "Store", lambda: Store(tmp_path / "fallback.db"))
    child = {
        "status": "partial", "source": "qwen_subagent",
        "original_blocked_task": "goal", "completed": ["researched"],
        "remaining": ["needs hardware"], "result": "finding", "notes": [],
    }
    monkeypatch.setattr(delegate, "delegate_task", lambda **_kw: __import__("json").dumps({"results": [{"summary": __import__("json").dumps(child)}]}))

    class Agent:
        platform = "telegram"

    result = maybe_run(
        Agent(),
        {"error": "content_policy_blocked: provider refused", "final_response": "blocked", "failed": True},
        "goal", "parent-task",
    )
    assert result["policy_fallback_id"].startswith("pf-")
    merged = __import__("json").loads(result["final_response"])
    assert merged["policy_source"] == "provider_policy_block"
    assert merged["remaining"] == ["needs hardware"]
