"""RED contracts for opt-in native OpenAI Responses compaction."""

from __future__ import annotations

from types import SimpleNamespace
import sys
import types

import pytest

from agent.transports.anthropic import AnthropicTransport
from agent.transports.bedrock import BedrockTransport
from agent.transports.chat_completions import ChatCompletionsTransport

from agent.codex_responses_adapter import (
    _chat_messages_to_responses_input,
    _normalize_codex_response,
    _preflight_codex_api_kwargs,
)
from agent.responses_compaction import (
    NativeCompactionLedger,
    NativeCompactionPolicy,
    NativeCompactionRoute,
    advance_policy_after_success,
    build_native_request_overrides,
    is_structured_compaction_unsupported_error,
    prepare_emergency_hermes_compaction,
    resolve_native_compaction_threshold,
    should_defer_hermes_compaction,
)
from hermes_cli.config import DEFAULT_CONFIG
from hermes_state import SessionDB

sys.modules.setdefault("fire", types.SimpleNamespace(Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", types.SimpleNamespace())
import run_agent


def _message_item(role: str, text: str) -> dict:
    content_type = "output_text" if role == "assistant" else "input_text"
    return {
        "type": "message",
        "role": role,
        "content": [{"type": content_type, "text": text}],
    }


def _contains_key(value, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(v, key) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_key(v, key) for v in value)
    return False


@pytest.mark.parametrize(
    "transport,kwargs",
    [
        (ChatCompletionsTransport(), {"model": "gpt-5.6-sol"}),
        (AnthropicTransport(), {"base_url": "https://api.anthropic.com"}),
        (BedrockTransport(), {}),
    ],
)
def test_every_non_responses_transport_drops_native_output_sidecars(
    transport, kwargs
):
    messages = [
        {
            "role": "assistant",
            "content": "visible",
            "codex_output_items": [
                {"type": "compaction", "encrypted_content": "opaque"}
            ],
        }
    ]

    converted = transport.convert_messages(messages, **kwargs)

    assert not _contains_key(converted, "codex_output_items")
    assert messages[0]["codex_output_items"][0]["encrypted_content"] == "opaque"


def _route(*, issuer: str = "openai", model: str = "gpt-5.6-sol") -> dict:
    endpoint = (
        "https://api.x.ai/v1"
        if issuer == "xai"
        else "https://chatgpt.com/backend-api/codex"
    )
    return {"issuer_kind": issuer, "endpoint": endpoint, "model": model}


def _boundary_messages(*, issuer: str = "openai") -> list[dict]:
    route = _route(issuer=issuer)
    return [
        {"role": "user", "content": "old user"},
        {"role": "assistant", "content": "old assistant"},
        {
            "role": "assistant",
            "content": "boundary visible text",
            "codex_output_items": [
                {
                    "type": "compaction",
                    "encrypted_content": "opaque-compact-state",
                    "_issuer_kind": issuer,
                    "_compaction_route": route,
                },
                {
                    **_message_item("assistant", "boundary visible text"),
                    "_issuer_kind": issuer,
                    "_compaction_route": route,
                },
            ],
        },
        {"role": "user", "content": "new user"},
    ]


class _StructuredCompactionError(Exception):
    status_code = 400
    body = {
        "error": {
            "code": "unknown_parameter",
            "param": "context_management",
            "message": "unknown parameter",
        }
    }


def test_config_defaults_keep_native_responses_compaction_off():
    cfg = DEFAULT_CONFIG["compression"]
    assert cfg["codex_responses_auto"] == "hermes"
    assert cfg["codex_responses_compact_threshold"] == 200_000


def test_native_threshold_stays_below_hermes_fallback():
    assert resolve_native_compaction_threshold(200_000, hermes_threshold=231_200) == 200_000
    assert resolve_native_compaction_threshold(200_000, hermes_threshold=136_000) == 127_808
    assert resolve_native_compaction_threshold(500, hermes_threshold=8_000) == 500


def test_request_override_is_route_scoped_and_terminal_states_fail_closed():
    route = NativeCompactionRoute(
        issuer_kind="codex_backend",
        endpoint="https://chatgpt.com/backend-api/codex",
        model="gpt-5.6-sol",
    )
    policy = NativeCompactionPolicy(route=route)
    overrides = build_native_request_overrides(
        {}, mode="native", policy=policy, compact_threshold=200_000
    )
    assert overrides["context_management"] == [
        {"type": "compaction", "compact_threshold": 200_000}
    ]
    unsupported = policy.transition("unsupported")
    assert build_native_request_overrides(
        {}, mode="native", policy=unsupported, compact_threshold=200_000
    ) == {}
    xai = NativeCompactionPolicy(
        route=NativeCompactionRoute(
            issuer_kind="xai_responses",
            endpoint="https://api.x.ai/v1",
            model="grok-4.5",
        )
    )
    assert build_native_request_overrides(
        {}, mode="native", policy=xai, compact_threshold=200_000
    ) == {}


def test_emergency_overflow_hands_native_ownership_back_before_hermes():
    route = NativeCompactionRoute(
        issuer_kind="codex_backend",
        endpoint="https://chatgpt.com/backend-api/codex",
        model="gpt-5.6-sol",
    )
    agent = SimpleNamespace(
        api_mode="codex_responses",
        compression_enabled=True,
        codex_responses_auto_compaction="native",
        provider="openai-codex",
        base_url=route.endpoint,
        model=route.model,
        _native_compaction_policy=NativeCompactionPolicy(
            route=route,
            capability="item_observed",
        ),
        _session_db=None,
        session_id=None,
        _native_compaction_request_active=True,
        _native_compaction_replay_attempted=True,
    )

    assert not prepare_emergency_hermes_compaction(
        agent, reason="provider_context_overflow"
    )
    assert agent._native_compaction_policy.capability == "quarantined"
    assert agent._native_compaction_policy.fallback_count == 0
    assert agent._native_compaction_request_active is True
    assert agent._native_compaction_replay_attempted is True


def test_emergency_overflow_does_not_override_effective_off():
    agent = SimpleNamespace(
        api_mode="codex_responses",
        compression_enabled=True,
        codex_responses_auto_compaction="off",
    )
    assert not prepare_emergency_hermes_compaction(
        agent, reason="provider_context_overflow"
    )


def test_preflight_accepts_only_valid_context_management_shape():
    kwargs = {
        "model": "gpt-5.6-sol",
        "instructions": "You are Hermes.",
        "input": [{"role": "user", "content": "hi"}],
        "store": False,
        "context_management": [
            {"type": "compaction", "compact_threshold": 200_000}
        ],
    }
    assert _preflight_codex_api_kwargs(kwargs)["context_management"] == kwargs["context_management"]
    kwargs["context_management"] = [
        {"type": "compaction", "compact_threshold": True}
    ]
    with pytest.raises(ValueError):
        _preflight_codex_api_kwargs(kwargs)


@pytest.mark.parametrize(
    "reserved_key,reserved_value",
    [
        ("store", True),
        ("previous_response_id", "resp_existing"),
        (
            "context_management",
            [{"type": "compaction", "compact_threshold": 1}],
        ),
    ],
)
def test_preflight_rejects_reserved_responses_fields_inside_extra_body(
    reserved_key, reserved_value
):
    kwargs = {
        "model": "gpt-5.6-sol",
        "instructions": "You are Hermes.",
        "input": [{"role": "user", "content": "hi"}],
        "store": False,
        "extra_body": {
            reserved_key: reserved_value,
            "provider_extension": "kept",
        },
    }

    with pytest.raises(ValueError, match="extra_body.*reserved"):
        _preflight_codex_api_kwargs(kwargs)


def test_preflight_keeps_non_reserved_provider_extensions_in_extra_body():
    kwargs = {
        "model": "gpt-5.6-sol",
        "instructions": "You are Hermes.",
        "input": [{"role": "user", "content": "hi"}],
        "store": False,
        "extra_body": {
            "prompt_cache_key": "pck_test",
            "provider_extension": {"enabled": True},
        },
    }

    assert _preflight_codex_api_kwargs(kwargs)["extra_body"] == kwargs["extra_body"]


def test_normalizer_preserves_exact_order_when_compaction_is_emitted():
    response = SimpleNamespace(
        status="completed",
        incomplete_details=None,
        output_text=None,
        output=[
            SimpleNamespace(
                type="compaction",
                id="cmp_1",
                encrypted_content="opaque",
                created_by=None,
                status=None,
            ),
            SimpleNamespace(
                type="message",
                id="msg_1",
                role="assistant",
                status="completed",
                phase=None,
                content=[SimpleNamespace(type="output_text", text="answer")],
            ),
        ],
    )
    route = _route(issuer="codex_backend")
    message, finish_reason = _normalize_codex_response(
        response,
        issuer_kind="codex_backend",
        compaction_route=route,
    )
    assert finish_reason == "stop"
    assert [item["type"] for item in message.codex_output_items] == [
        "compaction",
        "message",
    ]
    assert all(
        item["_issuer_kind"] == "codex_backend"
        for item in message.codex_output_items
    )
    assert all(
        item["_compaction_route"] == route for item in message.codex_output_items
    )
    assert message.codex_output_items[0]["encrypted_content"] == "opaque"
    assert message.codex_output_items[1]["content"][0]["text"] == "answer"


def test_policy_requires_observed_item_and_verified_replay_to_advance():
    policy = NativeCompactionPolicy(
        route=NativeCompactionRoute(
            issuer_kind="codex_backend",
            endpoint="https://chatgpt.com/backend-api/codex",
            model="gpt-5.6-sol",
        )
    )
    policy = advance_policy_after_success(
        policy, codex_output_items=None, replay_attempted=False
    )
    assert policy.capability == "shape_accepted"
    assert not should_defer_hermes_compaction("native", policy)
    policy = advance_policy_after_success(
        policy,
        codex_output_items=[
            {"type": "compaction", "encrypted_content": "opaque"}
        ],
        replay_attempted=False,
    )
    assert policy.capability == "item_observed"
    assert policy.compaction_count == 1
    assert should_defer_hermes_compaction("native", policy)
    policy = advance_policy_after_success(
        policy,
        codex_output_items=[
            {"type": "compaction", "encrypted_content": "opaque"}
        ],
        replay_attempted=False,
    )
    assert policy.compaction_count == 1
    policy = advance_policy_after_success(
        policy,
        codex_output_items=None,
        replay_attempted=True,
    )
    assert policy.capability == "replay_verified"


def test_same_issuer_projection_starts_from_ordered_compaction_sidecar():
    items = _chat_messages_to_responses_input(
        _boundary_messages(),
        current_issuer_kind="openai",
        current_compaction_route=_route(),
    )
    assert [item.get("type") for item in items[:2]] == ["compaction", "message"]
    assert items[0] == {
        "type": "compaction",
        "encrypted_content": "opaque-compact-state",
    }
    assert items[1]["content"][0]["text"] == "boundary visible text"
    assert items[2] == {"role": "user", "content": "new user"}


def test_foreign_issuer_ignores_sidecar_and_replays_intact_transcript():
    items = _chat_messages_to_responses_input(
        _boundary_messages(issuer="openai"),
        current_issuer_kind="xai",
        current_compaction_route=_route(issuer="xai", model="grok-4.5"),
    )
    assert all(item.get("type") != "compaction" for item in items)
    assert [item.get("content") for item in items] == [
        "old user",
        "old assistant",
        "boundary visible text",
        "new user",
    ]


def test_malformed_compaction_sidecar_fails_open_to_intact_transcript():
    messages = _boundary_messages()
    messages[2]["codex_output_items"] = [
        {"type": "compaction", "encrypted_content": "", "_issuer_kind": "openai"}
    ]
    items = _chat_messages_to_responses_input(
        messages,
        current_issuer_kind="openai",
        current_compaction_route=_route(),
    )
    assert all(item.get("type") != "compaction" for item in items)
    assert items[0] == {"role": "user", "content": "old user"}


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (400, {"error": {"code": "unknown_parameter", "param": "context_management"}}, True),
        (422, {"error": {"code": "invalid_value", "param": "compact_threshold"}}, True),
        (400, {"error": {"code": "invalid_request", "param": "messages"}}, False),
        (401, {"error": {"code": "unknown_parameter", "param": "context_management"}}, False),
        (500, {"error": {"code": "unknown_parameter", "param": "context_management"}}, False),
        (400, None, False),
    ],
)
def test_only_structured_compaction_parameter_4xx_downgrades(status, body, expected):
    error = SimpleNamespace(status_code=status, body=body)
    assert is_structured_compaction_unsupported_error(error) is expected


def test_policy_state_is_monotonic_and_route_scoped():
    route = NativeCompactionRoute(
        issuer_kind="openai",
        endpoint="https://chatgpt.com/backend-api/codex",
        model="gpt-5.6-sol",
    )
    policy = NativeCompactionPolicy(route=route)
    assert policy.capability == "unknown"
    assert should_defer_hermes_compaction("off", policy)
    policy = policy.transition("shape_accepted")
    policy = advance_policy_after_success(
        policy,
        codex_output_items=[
            {"type": "compaction", "encrypted_content": "opaque"}
        ],
        replay_attempted=False,
    )
    policy = advance_policy_after_success(
        policy, codex_output_items=None, replay_attempted=True
    )
    assert policy.capability == "replay_verified"
    with pytest.raises(ValueError):
        policy.transition("shape_accepted")
    assert NativeCompactionPolicy.from_dict(policy.to_dict()).route == route


def test_sqlite_round_trips_ordered_output_and_cas_session_state(tmp_path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    session_id = db.create_session("native-compact", "test", model="gpt-5.6-sol")
    route = NativeCompactionRoute(
        issuer_kind="openai",
        endpoint="https://chatgpt.com/backend-api/codex",
        model="gpt-5.6-sol",
    )
    route_stamp = {
        "_issuer_kind": route.issuer_kind,
        "_compaction_route": route.to_dict(),
    }
    ordered = [
        {"type": "compaction", "encrypted_content": "opaque", **route_stamp},
        {
            "type": "reasoning",
            "id": "rs_ordered",
            "encrypted_content": "opaque-reasoning",
            **route_stamp,
        },
        {
            "type": "function_call",
            "id": "fc_ordered",
            "call_id": "call_ordered",
            "name": "noop",
            "arguments": "{}",
            **route_stamp,
        },
        {**_message_item("assistant", "after"), **route_stamp},
    ]
    observed_policy = advance_policy_after_success(
        NativeCompactionPolicy(route=route),
        codex_output_items=ordered,
        replay_attempted=False,
    )
    db.append_message(
        session_id,
        role="assistant",
        content="after",
        codex_output_items=ordered,
        codex_responses_compaction_policy=observed_policy.to_dict(),
        expected_codex_responses_compaction_revision=0,
    )
    loaded = db.get_messages_as_conversation(session_id)
    assert loaded[-1]["codex_output_items"] == ordered

    persisted = db.get_codex_responses_compaction_state(session_id)
    assert persisted["revision"] == 1
    persisted_ledger = NativeCompactionLedger.from_dict(persisted)
    assert persisted_ledger.policy_for(route).capability == "item_observed"

    replay_policy = advance_policy_after_success(
        persisted_ledger.policy_for(route),
        codex_output_items=ordered,
        replay_attempted=True,
    )
    replay_ledger = persisted_ledger.with_policy(replay_policy)
    assert db.compare_and_set_codex_responses_compaction_state(
        session_id,
        expected_revision=1,
        state=replay_ledger.to_dict(),
    )
    persisted = db.get_codex_responses_compaction_state(session_id)
    assert persisted["revision"] == 2
    assert NativeCompactionLedger.from_dict(persisted).policy_for(
        route
    ).capability == "replay_verified"
    assert not db.compare_and_set_codex_responses_compaction_state(
        session_id,
        expected_revision=1,
        state=replay_ledger.to_dict(),
    )

    child_id = "native-compact-child"
    db.create_session_fork(
        parent_session_id=session_id,
        child_session_id=child_id,
        source="test",
        model="gpt-5.6-sol",
        model_config=None,
        messages=db.get_messages_as_conversation(session_id),
        quarantine_error="checkpoint_missing_in_test_fork",
    )
    assert db.get_codex_responses_compaction_state(child_id) == persisted
    db.close()

    reopened = SessionDB(db_path=db_path)
    assert reopened.get_codex_responses_compaction_state(session_id) == persisted
    assert reopened.get_messages_as_conversation(session_id)[-1]["codex_output_items"] == ordered
    blob = reopened.export_session(session_id)
    blob["codex_responses_compaction_state"] = persisted
    reopened.close()

    imported = SessionDB(db_path=tmp_path / "imported.db")
    result = imported.import_sessions([blob])
    assert result["ok"] is True
    assert imported.get_messages_as_conversation(session_id)[-1]["codex_output_items"] == ordered
    assert imported.get_codex_responses_compaction_state(session_id) == persisted
    imported.close()
