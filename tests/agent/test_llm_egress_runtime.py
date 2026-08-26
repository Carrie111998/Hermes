from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.llm_egress_firewall import EgressBlocked, SanitizedTextRejected
from agent.llm_egress_runtime import (
    authorize_agent_sdk_kwargs,
    dispatch_authorized_agent_request,
)
from agent.source_provenance import SourceProvenanceRegistry


def _agent(tmp_path: Path, registry: SourceProvenanceRegistry | None = None):
    return SimpleNamespace(
        provider="custom",
        model="test-model",
        base_url="https://llm.example.test/v1",
        api_mode="chat_completions",
        session_id="session-1",
        _current_turn_id="turn-1",
        _current_api_request_id="req-1",
        _llm_egress_policy_digest=sha256(b"policy-1").hexdigest(),
        _llm_egress_state_dir=tmp_path,
        _source_provenance_registry=registry or SourceProvenanceRegistry(),
    )


def _grant(tmp_path: Path, registry: SourceProvenanceRegistry):
    path = tmp_path / "source.py"
    content = b"verified source\n"
    path.write_bytes(content)
    return registry.issue_file_slice(
        path=path,
        line_start=1,
        line_end=1,
        content=content,
        session_id="session-1",
        turn_id="turn-1",
        request_id="req-1",
        policy_digest=sha256(b"policy-1").hexdigest(),
    )


def test_runtime_authorizes_mixed_exact_source_and_bounded_sanitized_text(tmp_path):
    registry = SourceProvenanceRegistry()
    _grant(tmp_path, registry)
    agent = _agent(tmp_path, registry)
    kwargs = {
        "model": "test-model",
        "messages": [
            {"role": "system", "content": "Fix CI now."},
            {"role": "user", "content": "CI:\nverified source\nDo fix."},
        ],
        "temperature": 0,
    }

    authorized, receipt = authorize_agent_sdk_kwargs(agent, kwargs)

    assert authorized == kwargs
    assert receipt.decision.source_grant_count == 1
    assert receipt.decision.source_segment_count == 1
    wire = json.loads(receipt.payload_bytes)
    assert wire == kwargs
    assert "session_id" not in wire
    assert "turn_id" not in wire
    assert "request_id" not in wire
    assert "policy_digest" not in wire


def test_runtime_granted_caps_default_to_the_configured_request_caps(tmp_path):
    registry = SourceProvenanceRegistry()
    path = tmp_path / "large-source.txt"
    content = b"plain source sentence\n" * 12
    path.write_bytes(content)
    registry.issue_file_slice(
        path=path,
        line_start=1,
        line_end=12,
        content=content,
        session_id="session-1",
        turn_id="turn-1",
        request_id="req-1",
        policy_digest=sha256(b"policy-1").hexdigest(),
    )
    agent = _agent(tmp_path, registry)
    agent._llm_egress_max_serialized_bytes = 128

    with pytest.raises(EgressBlocked) as exc_info:
        authorize_agent_sdk_kwargs(
            agent,
            {
                "model": "test-model",
                "messages": [{"role": "user", "content": content.decode("utf-8")}],
            },
        )

    assert "serialized_bytes_exceeded" in exc_info.value.decision.reason_codes


def test_runtime_keeps_sdk_controls_out_of_authorized_body(tmp_path):
    agent = _agent(tmp_path)
    timeout = object()
    kwargs = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "Fix CI now."}],
        "timeout": timeout,
    }
    authorized, receipt = authorize_agent_sdk_kwargs(agent, kwargs)
    assert authorized["timeout"] is timeout
    assert "timeout" not in json.loads(receipt.payload_bytes)


def test_runtime_scans_extra_headers_and_query_as_request_content(tmp_path):
    agent = _agent(tmp_path)
    calls = []
    with pytest.raises((EgressBlocked, SanitizedTextRejected)):
        dispatch_authorized_agent_request(
            agent,
            {
                "model": "test-model",
                "messages": [{"role": "user", "content": "Fix CI now."}],
                "extra_headers": {"Authorization": "token=secret-value"},
                "extra_query": {"trace": "safe"},
            },
            lambda request: calls.append(request),
        )
    assert calls == []


def test_runtime_verifies_authorized_payload_at_provider_boundary(
    tmp_path, monkeypatch
):
    agent = _agent(tmp_path)
    calls = []
    original = __import__(
        "agent.llm_egress_firewall", fromlist=["AuthorizedEgress"]
    ).AuthorizedEgress.verify_payload
    verified = []

    def _verify(self, candidate):
        verified.append(candidate)
        return original(self, candidate)

    monkeypatch.setattr(
        "agent.llm_egress_firewall.AuthorizedEgress.verify_payload", _verify
    )
    dispatch_authorized_agent_request(
        agent,
        {
            "model": "test-model",
            "messages": [{"role": "user", "content": "Fix CI now."}],
        },
        lambda request: calls.append(request),
    )
    assert calls
    assert len(verified) == 1


@pytest.mark.parametrize(
    "text",
    [
        "token=super-secret-value",
        "Read /Users/private/repository/file.py",
        "ZW5jb2RlZCBwcml2YXRlIGRldGFpbA==",
    ],
)
def test_runtime_denies_unsafe_text_before_provider_callback(tmp_path, text):
    agent = _agent(tmp_path)
    calls = []
    with pytest.raises((EgressBlocked, SanitizedTextRejected)):
        dispatch_authorized_agent_request(
            agent,
            {"model": "test-model", "messages": [{"role": "user", "content": text}]},
            lambda request: calls.append(request),
        )
    assert calls == []


def test_runtime_dispatches_exactly_once_with_authorized_bytes(tmp_path):
    agent = _agent(tmp_path)
    calls = []
    result = dispatch_authorized_agent_request(
        agent,
        {
            "model": "test-model",
            "messages": [{"role": "user", "content": "Fix CI now."}],
        },
        lambda request: calls.append(request) or "ok",
    )
    assert result == "ok"
    assert calls == [
        {
            "messages": [{"content": "Fix CI now.", "role": "user"}],
            "model": "test-model",
        }
    ]


def test_provider_callback_cannot_mutate_authorized_request(tmp_path):
    agent = _agent(tmp_path)

    def mutate(request):
        request["messages"] = [{"role": "user", "content": "replacement"}]

    with pytest.raises(TypeError):
        dispatch_authorized_agent_request(
            agent,
            {
                "model": "test-model",
                "messages": [{"role": "user", "content": "Fix CI now."}],
            },
            mutate,
        )
