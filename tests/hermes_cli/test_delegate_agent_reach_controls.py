from __future__ import annotations

import pytest

from hermes_cli.config_defaults import DEFAULT_CONFIG
from hermes_cli.delegate_agent_reach_controls import (
    AGENT_REACH_PROVENANCE_SCHEMA,
    DELEGATE_BRIDGE_SCHEMA,
    EXECUTION_RECEIPT_SCHEMA,
    AgentReachControlError,
    DelegateControlError,
    build_agent_reach_provenance,
    build_omh_delegate_bridge,
    enabled_agent_reach_sources,
    enabled_delegate_adapters,
    normalize_delegate_execution_receipt,
    resolve_agent_reach_plan,
    social_mutation_capabilities_enabled,
)


def test_delegate_config_exposes_only_codex_and_claude_capabilities():
    config = {
        "delegation": {
            "adapters": {
                "codex": {"enabled": True},
                "claude": {"enabled": True},
                "shell": {"enabled": True, "capability": "delegate.shell"},
            }
        }
    }

    assert enabled_delegate_adapters(config) == {
        "codex": "delegate.codex",
        "claude": "delegate.claude",
    }


def test_default_config_does_not_blanket_enable_delegate_adapters():
    assert enabled_delegate_adapters(DEFAULT_CONFIG) == {}
    assert set(DEFAULT_CONFIG["delegation"]["adapters"]) == {"codex", "claude"}


def test_omh_delegate_bridge_rejects_unknown_adapter():
    with pytest.raises(DelegateControlError):
        build_omh_delegate_bridge(adapter="shell", objective="inspect repository")


def test_delegate_final_text_is_not_execution_proof_without_independent_verification():
    bridge = build_omh_delegate_bridge(
        adapter="codex",
        objective="change one file",
        allowed_actions=["file.write"],
        required_evidence=["git diff", "pytest"],
    )

    receipt = normalize_delegate_execution_receipt(
        bridge_payload=bridge,
        delegate_result={"final_response": "Done; tests pass."},
    )

    assert bridge["schema"] == DELEGATE_BRIDGE_SCHEMA
    assert bridge["constraints"]["delegate_final_text_is_not_proof"] is True
    assert receipt["schema"] == EXECUTION_RECEIPT_SCHEMA
    assert receipt["status"] == "needs_verification"
    assert receipt["delegate"]["final_text"] == "Done; tests pass."
    assert receipt["delegate"]["final_text_is_execution_proof"] is False
    assert receipt["verification"]["independently_verified"] is False


def test_delegate_commit_marks_policy_violation_even_with_summary():
    bridge = build_omh_delegate_bridge(adapter="claude", objective="edit docs")

    receipt = normalize_delegate_execution_receipt(
        bridge_payload=bridge,
        delegate_result="committed fix",
        delegate_committed=True,
        verification={"independently_verified": True, "commands": ["git show --stat"]},
    )

    assert receipt["status"] == "policy_violation"
    assert receipt["delegate"]["committed"] is True
    assert receipt["policy"]["delegate_must_not_commit"] is True


def test_agent_reach_defaults_are_read_only_and_disabled():
    assert enabled_agent_reach_sources(DEFAULT_CONFIG) == {}
    assert social_mutation_capabilities_enabled(DEFAULT_CONFIG) == []
    assert DEFAULT_CONFIG["agent_reach"]["canonical_connectors_preferred"] is True


def test_agent_reach_read_sources_do_not_enable_social_mutation():
    config = {
        "agent_reach": {
            "social_mutation_enabled": True,
            "mutation_capabilities": [
                "x.post",
                "linkedin.dm",
                "github.write",
                "reddit.read",
            ],
            "sources": {
                "x": {"enabled": True},
                "linkedin": {"enabled": True},
                "github": {"enabled": True},
            },
        }
    }

    sources = enabled_agent_reach_sources(config)

    assert sources["x"]["capability"] == "agent_reach.x.read"
    assert sources["x"]["mutation_enabled"] is False
    assert sources["linkedin"]["mutation_enabled"] is False
    assert sources["github"]["mutation_enabled"] is False
    assert social_mutation_capabilities_enabled(config) == ["x.post", "linkedin.dm"]


def test_agent_reach_provenance_carries_required_source_readback_fields():
    provenance = build_agent_reach_provenance(
        source="youtube",
        source_id="video-123",
        url="https://youtu.be/video-123",
        backend="agent-reach",
        account_session_class="anonymous",
        raw_path="raw/youtube/video-123.json",
        normalized_path="normalized/youtube/video-123.md",
        retrieved_at="2026-09-01T12:00:00+00:00",
    )

    assert provenance == {
        "schema": AGENT_REACH_PROVENANCE_SCHEMA,
        "platform": "youtube",
        "source_id": "video-123",
        "url": "https://youtu.be/video-123",
        "retrieved_at": "2026-09-01T12:00:00+00:00",
        "backend": "agent-reach",
        "account_session_class": "anonymous",
        "raw_path": "raw/youtube/video-123.json",
        "normalized_path": "normalized/youtube/video-123.md",
    }


def test_agent_reach_source_failures_are_source_level_unavailable_states():
    plan = resolve_agent_reach_plan(
        source="reddit",
        config={"agent_reach": {"sources": {"reddit": {"enabled": False}}}},
    )

    assert plan["selected"] == "unavailable"
    assert plan["source_health"] == "unavailable"
    assert plan["mutation_capabilities"] == []


def test_canonical_connector_outranks_agent_reach_fallback():
    plan = resolve_agent_reach_plan(
        source="github",
        config={"agent_reach": {"sources": {"github": {"enabled": True}}}},
        canonical_connector_available=True,
    )

    assert plan["selected"] == "canonical_connector"
    assert plan["fallback_available"] is True
    assert plan["read_capability"] == "agent_reach.github.read"


def test_agent_reach_rejects_unknown_sources():
    with pytest.raises(AgentReachControlError):
        build_agent_reach_provenance(
            source="tiktok",
            source_id="1",
            url="https://example.test/1",
            backend="agent-reach",
            account_session_class="anonymous",
            raw_path="raw.json",
            normalized_path="normalized.md",
        )
