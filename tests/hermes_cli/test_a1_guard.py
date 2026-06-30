import json

import pytest

from hermes_cli.a1_guard import (
    A1DispatchDenied,
    guard_model_dispatch,
    guarded_model_dispatch,
    record_dispatch_result,
)


def _ctx(**overrides):
    ctx = {
        "api_request_id": "req-1",
        "correlation_id": "corr-1",
        "session_id": "sess-1",
        "surface": "test",
        "profile": "pennyworth-localdaily",
        "classification": "C0_PUBLIC",
        "requested_provider": "custom:headroom-openrouter-litellm",
        "requested_model": "frontier-fast",
        "canonical_provider": "custom:headroom-openrouter-litellm",
        "canonical_model": "frontier-fast",
        "canonical_api_mode": "chat_completions",
        "canonical_base_url": "http://localhost:8787/v1",
        "provider_source": "custom-provider",
        "policy_version": "test-policy",
        "config_hash": "cfg-test",
        "allowed_base_url_hosts": ["localhost:8787"],
    }
    ctx.update(overrides)
    return ctx


def _request(secret="DO-NOT-STORE-RAW"):
    return {
        "messages": [{"role": "user", "content": f"CLASSIFICATION=C2 {secret}"}],
        "model": "frontier-fast",
        "max_tokens": 16,
    }


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_c2_local_only_to_headroom_is_denied_before_dispatch(tmp_path):
    sink = tmp_path / "a1.jsonl"

    with pytest.raises(A1DispatchDenied) as exc:
        guard_model_dispatch(
            api_kwargs=_request(),
            runtime_context=_ctx(classification="C2_LOCAL_ONLY"),
            evidence_sink=sink,
        )

    assert exc.value.rule_id == "a1.c2.frontier-deny"
    events = _read_jsonl(sink)
    assert [event["event_type"] for event in events] == [
        "resolver_decision",
        "payload_capture",
        "dispatch_result",
    ]
    assert events[-1]["provider_call_attempted"] is False
    assert events[-1]["decision"] == "deny"


def test_c0_public_to_approved_frontier_is_allowed_and_records_envelope(tmp_path):
    sink = tmp_path / "a1.jsonl"

    result = guard_model_dispatch(
        api_kwargs=_request(secret="PUBLIC-SYNTHETIC"),
        runtime_context=_ctx(classification="C0_PUBLIC"),
        evidence_sink=sink,
    )

    assert result.decision.decision == "allow"
    assert result.capture.dispatch_allowed is True
    events = _read_jsonl(sink)
    assert [event["event_type"] for event in events] == [
        "resolver_decision",
        "payload_capture",
    ]
    assert events[0]["api_request_id"] == "req-1"
    assert events[1]["payload_digest"] == result.capture.payload_digest


def test_payload_capture_contains_digest_not_raw_prompt_text(tmp_path):
    sink = tmp_path / "a1.jsonl"
    raw_secret = "RAW-CONFIDENTIAL-PROMPT"

    guard_model_dispatch(
        api_kwargs=_request(secret=raw_secret),
        runtime_context=_ctx(classification="C0_PUBLIC"),
        evidence_sink=sink,
    )

    serialized = sink.read_text()
    assert raw_secret not in serialized
    payload_event = _read_jsonl(sink)[1]
    assert payload_event["event_type"] == "payload_capture"
    assert payload_event["redaction_class"] == "digest-only"
    assert len(payload_event["payload_digest"]) == 64


def test_unexpected_base_url_is_denied_before_dispatch(tmp_path):
    sink = tmp_path / "a1.jsonl"

    with pytest.raises(A1DispatchDenied) as exc:
        guard_model_dispatch(
            api_kwargs=_request(),
            runtime_context=_ctx(
                classification="C0_PUBLIC",
                canonical_base_url="https://unexpected.example.com/v1",
                allowed_base_url_hosts=["localhost:8787"],
            ),
            evidence_sink=sink,
        )

    assert exc.value.rule_id == "a1.route.unexpected-base-url"
    events = _read_jsonl(sink)
    assert events[-1]["event_type"] == "dispatch_result"
    assert events[-1]["provider_call_attempted"] is False


def test_unknown_classification_to_non_local_route_is_denied(tmp_path):
    sink = tmp_path / "a1.jsonl"

    with pytest.raises(A1DispatchDenied) as exc:
        guard_model_dispatch(
            api_kwargs=_request(),
            runtime_context=_ctx(classification=""),
            evidence_sink=sink,
        )

    assert exc.value.rule_id == "a1.guard.missing-classification"


def test_missing_capture_sink_fails_closed_for_allowed_route():
    with pytest.raises(A1DispatchDenied) as exc:
        guard_model_dispatch(
            api_kwargs=_request(secret="PUBLIC-SYNTHETIC"),
            runtime_context=_ctx(classification="C0_PUBLIC"),
            evidence_sink=None,
        )

    assert exc.value.rule_id == "a1.guard.capture-failed"


def test_record_dispatch_result_appends_correlated_result(tmp_path):
    sink = tmp_path / "a1.jsonl"
    guard_model_dispatch(
        api_kwargs=_request(secret="PUBLIC-SYNTHETIC"),
        runtime_context=_ctx(classification="C0_PUBLIC"),
        evidence_sink=sink,
    )

    record_dispatch_result(
        evidence_sink=sink,
        api_request_id="req-1",
        correlation_id="corr-1",
        provider_call_attempted=True,
        provider_call_completed=True,
    )

    events = _read_jsonl(sink)
    assert events[-1]["event_type"] == "dispatch_result"
    assert events[-1]["api_request_id"] == "req-1"
    assert events[-1]["provider_call_completed"] is True



def test_guard_records_hl_aos_frozen_classification_source_in_evidence(tmp_path):
    sink = tmp_path / 'a1.jsonl'
    guard_model_dispatch(
        api_kwargs=_request(),
        runtime_context=_ctx(
            classification='C0_PUBLIC',
            classification_source='hl_aos_frozen',
        ),
        evidence_sink=sink,
    )
    events = _read_jsonl(sink)
    assert events[0]['classification_source'] == 'hl_aos_frozen'

    guard_model_dispatch(
        api_kwargs=_request(),
        runtime_context=_ctx(
            classification='C0_PUBLIC',
            classification_source='unclassified',
        ),
        evidence_sink=sink,
    )
    events = _read_jsonl(sink)
    # Find the second resolver_decision event
    resolver_events = [e for e in events if e['event_type'] == 'resolver_decision']
    assert len(resolver_events) == 2
    assert resolver_events[1]['classification_source'] == 'unclassified'


def test_guarded_model_dispatch_calls_provider_only_after_allowed_envelope(tmp_path):
    sink = tmp_path / "a1.jsonl"
    calls = []

    def provider_call(request):
        calls.append(request)
        return {"ok": True}

    response = guarded_model_dispatch(
        api_kwargs=_request(secret="PUBLIC-SYNTHETIC"),
        next_call=provider_call,
        runtime_context=_ctx(classification="C0_PUBLIC"),
        evidence_sink=sink,
    )

    assert response == {"ok": True}
    assert len(calls) == 1
    events = _read_jsonl(sink)
    assert [event["event_type"] for event in events] == [
        "resolver_decision",
        "payload_capture",
        "dispatch_result",
    ]
    assert events[-1]["provider_call_attempted"] is True
    assert events[-1]["provider_call_completed"] is True


def test_guarded_model_dispatch_denial_never_calls_provider(tmp_path):
    sink = tmp_path / "a1.jsonl"
    calls = []

    def provider_call(request):  # pragma: no cover - must not run
        calls.append(request)
        return {"ok": True}

    with pytest.raises(A1DispatchDenied) as exc:
        guarded_model_dispatch(
            api_kwargs=_request(),
            next_call=provider_call,
            runtime_context=_ctx(classification="C2_LOCAL_ONLY"),
            evidence_sink=sink,
        )

    assert exc.value.rule_id == "a1.c2.frontier-deny"
    assert calls == []
    events = _read_jsonl(sink)
    assert events[-1]["event_type"] == "dispatch_result"
    assert events[-1]["provider_call_attempted"] is False


def test_guarded_model_dispatch_records_provider_exception(tmp_path):
    sink = tmp_path / "a1.jsonl"

    def provider_call(request):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        guarded_model_dispatch(
            api_kwargs=_request(secret="PUBLIC-SYNTHETIC"),
            next_call=provider_call,
            runtime_context=_ctx(classification="C0_PUBLIC"),
            evidence_sink=sink,
        )

    events = _read_jsonl(sink)
    assert events[-1]["event_type"] == "dispatch_result"
    assert events[-1]["provider_call_attempted"] is True
    assert events[-1]["provider_call_completed"] is False
    assert events[-1]["error_type"] == "RuntimeError"
