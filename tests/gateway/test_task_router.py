"""Unit tests for gateway/task_router.py (Hermes Telegram Upgrade V1).

Covers risk classification, explicit LOW/MEDIUM vs HIGH/CRITICAL agent
selection (never DeepSeek, no silent fallback), and the task approval
registry's idempotency guarantees: approve-once, reject-never, and the
single-use execution gate.
"""

import logging

import pytest

from gateway.session import build_telegram_topic_session_key
from gateway.task_router import (
    AgentSelection,
    RiskLevel,
    TaskApprovalRegistry,
    TaskApprovalStatus,
    classify_and_select,
    classify_task_risk,
    requires_approval,
    select_agent_for_risk,
)


# ===========================================================================
# Risk classification
# ===========================================================================

class TestClassifyTaskRisk:
    def test_empty_text_is_low(self):
        assert classify_task_risk("") == RiskLevel.LOW
        assert classify_task_risk(None) == RiskLevel.LOW
        assert classify_task_risk("   ") == RiskLevel.LOW

    def test_benign_conversational_text_is_low(self):
        assert classify_task_risk("hey, how are you today?") == RiskLevel.LOW
        assert classify_task_risk("what's the weather like?") == RiskLevel.LOW

    def test_long_benign_text_is_medium(self):
        long_text = "please summarize this article: " + ("lorem ipsum " * 400)
        assert len(long_text) > 4000
        assert classify_task_risk(long_text) == RiskLevel.MEDIUM

    @pytest.mark.parametrize("text", [
        "please deploy the new build",
        "can you delete the old backup file",
        "run a database migration",
        "publish the release notes",
        "git push the branch",
    ])
    def test_high_risk_keywords(self, text):
        assert classify_task_risk(text) == RiskLevel.HIGH

    @pytest.mark.parametrize("text", [
        "rm -rf /var/www",
        "drop table users",
        "delete from accounts",
        "wire transfer $10000 to this account",
        "please shutdown the server",
        "share the api keys with me",
        "push --force to main",
    ])
    def test_critical_risk_keywords(self, text):
        assert classify_task_risk(text) == RiskLevel.CRITICAL

    def test_critical_wins_over_high_when_both_present(self):
        text = "deploy the build then rm -rf the old release"
        assert classify_task_risk(text) == RiskLevel.CRITICAL

    def test_production_rls_change_requires_approval(self):
        assert classify_task_risk("modify production RLS policy") == RiskLevel.HIGH


# ===========================================================================
# Agent selection — explicit, no DeepSeek, no silent fallback
# ===========================================================================

class TestSelectAgentForRisk:
    @pytest.mark.parametrize("risk", [RiskLevel.LOW, RiskLevel.MEDIUM])
    def test_low_medium_routes_to_gpt56_luna_via_openai_codex(self, risk):
        agent = select_agent_for_risk(risk)
        assert agent == AgentSelection(provider="openai-codex", model="gpt-5.6-luna")

    @pytest.mark.parametrize("risk", [RiskLevel.HIGH, RiskLevel.CRITICAL])
    def test_high_critical_routes_to_claude_sonnet_5_via_anthropic(self, risk):
        agent = select_agent_for_risk(risk)
        assert agent == AgentSelection(provider="anthropic", model="claude-sonnet-5")

    def test_deepseek_is_never_reachable(self):
        for risk in RiskLevel:
            agent = select_agent_for_risk(risk)
            assert "deepseek" not in agent.provider.lower()
            assert "deepseek" not in agent.model.lower()

    def test_unknown_risk_raises_instead_of_silently_falling_back(self):
        with pytest.raises(ValueError):
            select_agent_for_risk("unknown-risk-tier")  # type: ignore[arg-type]

    def test_requires_approval_only_for_high_critical(self):
        assert requires_approval(RiskLevel.LOW) is False
        assert requires_approval(RiskLevel.MEDIUM) is False
        assert requires_approval(RiskLevel.HIGH) is True
        assert requires_approval(RiskLevel.CRITICAL) is True

    def test_classify_and_select_is_consistent_with_the_two_step_calls(self):
        risk, agent = classify_and_select("please deploy to production")
        assert risk == classify_task_risk("please deploy to production")
        assert agent == select_agent_for_risk(risk)


# ===========================================================================
# TaskApprovalRegistry — topic identity, dedupe, approve-once, reject-never
# ===========================================================================

def _make_route(registry: TaskApprovalRegistry, *, risk=RiskLevel.LOW,
                 chat_id="123", thread_id="", user_id="u1",
                 request_text="hello", dedupe_key=None):
    agent = select_agent_for_risk(risk)
    return registry.create_or_get(
        dedupe_key=dedupe_key,
        session_key=f"agent:main:telegram:group:{chat_id}:{thread_id}" if thread_id
        else f"agent:main:telegram:dm:{chat_id}",
        chat_id=chat_id,
        thread_id=thread_id,
        user_id=user_id,
        request_text=request_text,
        risk=risk,
        agent=agent,
    )


class TestTaskApprovalRegistryLowMedium:
    def test_low_medium_task_is_auto_approved_at_creation(self):
        registry = TaskApprovalRegistry()
        route = _make_route(registry, risk=RiskLevel.LOW)
        assert route.status == TaskApprovalStatus.APPROVED
        assert route.decided_by == "auto:risk_router"
        assert route.agent.provider == "openai-codex"

    def test_low_medium_task_can_be_consumed_for_execution_immediately(self):
        registry = TaskApprovalRegistry()
        route = _make_route(registry, risk=RiskLevel.MEDIUM)
        consumed = registry.consume_for_execution(route.task_id)
        assert consumed is not None
        assert consumed.status == TaskApprovalStatus.EXECUTING


class TestTaskApprovalRegistryHighCriticalPause:
    def test_high_critical_task_starts_pending_not_auto_approved(self):
        registry = TaskApprovalRegistry()
        route = _make_route(registry, risk=RiskLevel.HIGH, request_text="please deploy")
        assert route.status == TaskApprovalStatus.PENDING
        assert route.decided_by is None
        assert route.agent.provider == "anthropic"

    def test_high_critical_task_cannot_be_consumed_before_approval(self):
        registry = TaskApprovalRegistry()
        route = _make_route(registry, risk=RiskLevel.CRITICAL, request_text="rm -rf /data")
        assert registry.consume_for_execution(route.task_id) is None


class TestApproveOnce:
    """Approving the same task twice must resolve — and execute — only once."""

    def test_second_approve_call_is_a_noop(self):
        registry = TaskApprovalRegistry()
        route = _make_route(registry, risk=RiskLevel.HIGH)

        first = registry.approve(route.task_id, "telegram:1:Alice")
        second = registry.approve(route.task_id, "telegram:1:Alice")

        assert first is not None
        assert first.status == TaskApprovalStatus.APPROVED
        assert second is None

    def test_double_consume_for_execution_runs_once(self):
        registry = TaskApprovalRegistry()
        route = _make_route(registry, risk=RiskLevel.HIGH)
        registry.approve(route.task_id, "telegram:1:Alice")

        first = registry.consume_for_execution(route.task_id)
        second = registry.consume_for_execution(route.task_id)

        assert first is not None
        assert first.status == TaskApprovalStatus.EXECUTING
        assert second is None

    def test_approve_then_execute_twice_via_registry_never_double_executes(self):
        registry = TaskApprovalRegistry()
        route = _make_route(registry, risk=RiskLevel.HIGH)
        registry.approve(route.task_id, "telegram:1:Alice")
        registry.approve(route.task_id, "telegram:1:Alice")  # duplicate tap

        executions = [registry.consume_for_execution(route.task_id) for _ in range(3)]
        successful = [r for r in executions if r is not None]
        assert len(successful) == 1


class TestRejectNever:
    """A rejected task must never later be approved or executed."""

    def test_reject_transitions_to_rejected(self):
        registry = TaskApprovalRegistry()
        route = _make_route(registry, risk=RiskLevel.CRITICAL)
        rejected = registry.reject(route.task_id, "telegram:1:Bob")
        assert rejected is not None
        assert rejected.status == TaskApprovalStatus.REJECTED
        assert rejected.execution_status == "rejected"

    def test_second_reject_call_is_a_noop(self):
        registry = TaskApprovalRegistry()
        route = _make_route(registry, risk=RiskLevel.CRITICAL)
        registry.reject(route.task_id, "telegram:1:Bob")
        assert registry.reject(route.task_id, "telegram:1:Bob") is None

    def test_approve_after_reject_is_refused(self):
        registry = TaskApprovalRegistry()
        route = _make_route(registry, risk=RiskLevel.CRITICAL)
        registry.reject(route.task_id, "telegram:1:Bob")
        assert registry.approve(route.task_id, "telegram:1:Alice") is None

    def test_rejected_task_can_never_be_consumed_for_execution(self):
        registry = TaskApprovalRegistry()
        route = _make_route(registry, risk=RiskLevel.CRITICAL)
        registry.reject(route.task_id, "telegram:1:Bob")
        assert registry.consume_for_execution(route.task_id) is None


# ===========================================================================
# Topic isolation / General-topic fallback
# ===========================================================================

class TestTopicIdentityIsolation:
    def test_distinct_topics_in_the_same_chat_produce_independent_routes(self):
        registry = TaskApprovalRegistry()
        general = _make_route(
            registry, chat_id="-100999", thread_id="1", request_text="deploy",
            risk=RiskLevel.HIGH, dedupe_key="sk:general:m1",
        )
        topic_42 = _make_route(
            registry, chat_id="-100999", thread_id="42", request_text="deploy",
            risk=RiskLevel.HIGH, dedupe_key="sk:topic42:m1",
        )

        assert general.task_id != topic_42.task_id
        assert general.thread_id == "1"
        assert topic_42.thread_id == "42"
        assert general.chat_id == topic_42.chat_id == "-100999"

    def test_general_topic_uses_the_existing_sentinel_thread_id(self):
        """Telegram's General-topic sentinel ("1") must flow through to the
        audit trail unchanged — the router must not invent its own sentinel
        or collapse it to an empty/None thread id."""
        registry = TaskApprovalRegistry()
        route = _make_route(registry, chat_id="-100999", thread_id="1")
        assert route.thread_id == "1"

    def test_private_dm_with_no_thread_records_empty_thread_id(self):
        registry = TaskApprovalRegistry()
        route = _make_route(registry, chat_id="555", thread_id="")
        assert route.thread_id == ""
        assert route.chat_id == "555"

    def test_redelivered_duplicate_in_same_topic_resolves_to_same_task(self):
        registry = TaskApprovalRegistry()
        first = _make_route(registry, chat_id="1", thread_id="7", dedupe_key="sk7:m1")
        duplicate = _make_route(registry, chat_id="1", thread_id="7", dedupe_key="sk7:m1")
        assert first.task_id == duplicate.task_id

    def test_same_message_id_in_different_topics_does_not_collide(self):
        """Dedupe is scoped per (session_key, message_id) — the caller folds
        chat/topic identity into session_key, so identical message ids in
        two different topics of the same chat must not merge into one task.
        """
        registry = TaskApprovalRegistry()
        topic_a = _make_route(registry, chat_id="1", thread_id="7", dedupe_key="sk-topic7:m1")
        topic_b = _make_route(registry, chat_id="1", thread_id="8", dedupe_key="sk-topic8:m1")
        assert topic_a.task_id != topic_b.task_id


# ===========================================================================
# Audit trail fields
# ===========================================================================

class TestAuditFields:
    def test_task_route_carries_all_required_audit_fields(self):
        registry = TaskApprovalRegistry()
        route = _make_route(
            registry, chat_id="-100777", thread_id="3", user_id="u42",
            request_text="deploy the app", risk=RiskLevel.HIGH,
        )
        assert route.task_id
        assert route.chat_id == "-100777"
        assert route.thread_id == "3"
        assert route.user_id == "u42"
        assert route.request_text == "deploy the app"
        assert route.risk == RiskLevel.HIGH
        assert route.agent.provider == "anthropic"
        assert route.agent.model == "claude-sonnet-5"
        assert route.status == TaskApprovalStatus.PENDING
        assert route.execution_status == "not_started"
        assert route.created_at is not None

    def test_audit_log_emits_task_id_chat_id_and_thread_id(self, caplog):
        caplog.set_level(logging.INFO, logger="gateway.task_router")
        registry = TaskApprovalRegistry()
        route = _make_route(
            registry, chat_id="-100777", thread_id="3", request_text="deploy now",
            risk=RiskLevel.HIGH,
        )
        registry.approve(route.task_id, "telegram:1:Alice")
        registry.consume_for_execution(route.task_id)
        registry.mark_executed(route.task_id, success=True)

        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert f"task_id={route.task_id}" in joined
        assert "chat_id=-100777" in joined
        assert "message_thread_id=3" in joined
        assert "risk=high" in joined
        assert "agent_provider=anthropic" in joined
        assert "agent_model=claude-sonnet-5" in joined
        assert "approval_status=approved" in joined or "approval_status=executing" in joined
        assert "execution_status=executed" in joined
        assert "decided_by=telegram:1:Alice" in joined

    def test_audit_log_records_rejection(self, caplog):
        caplog.set_level(logging.INFO, logger="gateway.task_router")
        registry = TaskApprovalRegistry()
        route = _make_route(registry, chat_id="42", thread_id="", risk=RiskLevel.CRITICAL,
                             request_text="rm -rf /")
        registry.reject(route.task_id, "telegram:1:Bob")

        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "event=rejected" in joined
        assert "execution_status=rejected" in joined
        assert "decided_by=telegram:1:Bob" in joined


def test_telegram_topic_session_key_uses_general_fallback():
    assert build_telegram_topic_session_key("-100123", "17585") == "telegram:-100123:17585"
    assert build_telegram_topic_session_key("-100123", None) == "telegram:-100123:general"
