"""Adversarial tests for the durable API execution-context boundary."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from gateway.api_execution_context import (
    ApiExecutionContextError,
    SCHEMA,
    canonicalize_session_endpoint,
    normalize_api_mode,
    normalize_api_execution_context,
    normalize_model_identifier,
    normalize_provider_slug,
    normalize_route_source,
    normalize_service_tier,
    transport_semantic_digest,
)
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB


SECRET = "sk-proj-abcdef1234567890abcdef"


@pytest.fixture
def session_db(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        yield db
    finally:
        db.close()


def _valid_context() -> dict:
    model = "anthropic/claude-opus-4.6"
    provider = "openai-codex"
    return {
        "schema": SCHEMA,
        "gateway_session_key": (
            "agent:main:discord:channel:1504852355588423801"
        ),
        "request_model": model,
        "request_provider": provider,
        "model_options": {
            "reasoning": {"enabled": True, "effort": "high"},
            "service_tier": "priority",
        },
        "route_alias": "",
        "route_model": "",
        "route_provider": "",
        "route_semantic_sha256": "",
        "session_model": model,
        "confirmed_runtime_lock": True,
        "requested_runtime": {
            "model": model,
            "provider": provider,
        },
        "route_source": "session_model_lock",
        "effective_model": model,
        "effective_provider": provider,
        "effective_transport_sha256": transport_semantic_digest(
            model=model,
            provider=provider,
            base_url="https://api.openai.com/v1/",
            api_mode="chat_completions",
        ),
    }


def _stored_model_config(row: dict) -> dict:
    value = row["model_config"]
    return json.loads(value) if isinstance(value, str) else value


def test_identifier_helpers_canonicalize_real_runtime_values():
    assert (
        normalize_model_identifier("  anthropic/claude-opus-4.6  ")
        == "anthropic/claude-opus-4.6"
    )
    assert normalize_provider_slug(" OpenAI-Codex ") == "openai-codex"
    assert (
        normalize_provider_slug("custom:mimo-v2.5-pro")
        == "custom:mimo-v2.5-pro"
    )
    assert normalize_service_tier(None) is None
    assert normalize_service_tier("  ") is None
    assert normalize_service_tier(" PRIORITY ") == "priority"
    assert normalize_route_source(" RAW_REQUEST ") == "raw_request"
    assert normalize_api_mode(" CODEX_RESPONSES ") == "codex_responses"
    assert (
        canonicalize_session_endpoint("ACP://COPILOT/")
        == "acp://copilot"
    )


def test_api_mode_rejects_unknown_syntactically_valid_value():
    with pytest.raises(ApiExecutionContextError, match="unsupported"):
        normalize_api_mode("totally_bogus")


@pytest.mark.parametrize(
    ("normalizer", "value"),
    [
        (normalize_model_identifier, SECRET),
        (normalize_provider_slug, SECRET),
        (normalize_service_tier, SECRET),
        (normalize_route_source, SECRET),
    ],
)
def test_identifier_helpers_reject_forced_redaction_changes(
    normalizer,
    value,
):
    with pytest.raises(ApiExecutionContextError, match="secret-like"):
        normalizer(value)


@pytest.mark.parametrize(
    "provider",
    [
        "openai/api",
        "custom:",
        "custom:local-(127.0.0.1:4141)",
        "-openai",
        "openai-",
        "openai--codex",
    ],
)
def test_provider_slug_syntax_is_strict(provider):
    with pytest.raises(ApiExecutionContextError, match="provider syntax"):
        normalize_provider_slug(provider)


@pytest.mark.parametrize("tier", ["fast", "priority", "on"])
def test_service_tier_priority_aliases_canonicalize_to_priority(tier):
    assert normalize_service_tier(tier) == "priority"


@pytest.mark.parametrize(
    "tier",
    ["", None, "normal", "default", "standard", "off", "none"],
)
def test_service_tier_default_aliases_canonicalize_to_none(tier):
    assert normalize_service_tier(tier) is None


@pytest.mark.parametrize("tier", ["auto", "premium", "urgent"])
def test_service_tier_rejects_unknown_values(tier):
    with pytest.raises(ApiExecutionContextError, match="unsupported"):
        normalize_service_tier(tier)


@pytest.mark.parametrize(
    "source",
    ["", None, "global", "model_routes", "raw_request",
     "session_model_lock", "session_model_override"],
)
def test_route_source_only_admits_canonical_enum(source):
    expected = "global" if source in ("", None) else source
    assert normalize_route_source(source) == expected


def test_normalize_context_canonicalizes_all_replayed_identifiers():
    context = _valid_context()
    context["request_provider"] = " OpenAI-Codex "
    context["effective_provider"] = "OPENAI-CODEX"
    context["requested_runtime"]["provider"] = "OpenAI-Codex"
    context["model_options"]["service_tier"] = " PRIORITY "
    context["route_source"] = " SESSION_MODEL_LOCK "

    normalized = normalize_api_execution_context(context, allow_none=False)

    assert normalized["request_provider"] == "openai-codex"
    assert normalized["effective_provider"] == "openai-codex"
    assert normalized["requested_runtime"]["provider"] == "openai-codex"
    assert normalized["model_options"]["service_tier"] == "priority"
    assert normalized["route_source"] == "session_model_lock"


@pytest.mark.parametrize(
    "path",
    [
        ("gateway_session_key",),
        ("request_model",),
        ("request_provider",),
        ("model_options", "service_tier"),
        ("session_model",),
        ("requested_runtime", "model"),
        ("requested_runtime", "provider"),
        ("effective_model",),
        ("effective_provider",),
    ],
)
def test_context_rejects_secret_smuggling_in_every_free_runtime_string(path):
    context = _valid_context()
    target = context
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = SECRET

    with pytest.raises(ApiExecutionContextError, match="secret-like"):
        normalize_api_execution_context(context, allow_none=False)


def test_context_rejects_secret_route_alias_and_route_identifiers():
    for field in ("route_alias", "route_model", "route_provider"):
        context = _valid_context()
        context.update(
            {
                "route_alias": "customer-route",
                "route_model": "openai/gpt-5.6",
                "route_provider": "openai-api",
                "route_semantic_sha256": "b" * 64,
            }
        )
        context[field] = SECRET
        with pytest.raises(ApiExecutionContextError, match="secret-like"):
            normalize_api_execution_context(context, allow_none=False)


@pytest.mark.parametrize(
    "transport",
    [
        {
            "model": SECRET,
            "provider": "openai-api",
            "base_url": "https://api.openai.com/v1",
            "api_mode": "chat_completions",
        },
        {
            "model": "openai/gpt-5.6",
            "provider": SECRET,
            "base_url": "https://api.openai.com/v1",
            "api_mode": "chat_completions",
        },
        {
            "model": "openai/gpt-5.6",
            "provider": "openai-api",
            "base_url": f"https://example.test/v1/{SECRET}",
            "api_mode": "chat_completions",
        },
        {
            "model": "openai/gpt-5.6",
            "provider": "openai-api",
            "base_url": "https://api.openai.com/v1",
            "api_mode": SECRET,
        },
    ],
)
def test_transport_digest_rejects_secret_material(transport):
    with pytest.raises(ApiExecutionContextError, match="secret-like"):
        transport_semantic_digest(**transport)


@pytest.mark.parametrize(
    "unsafe_kwargs",
    [
        {"requested_model": SECRET},
        {"requested_provider": SECRET},
        {"model_options": {"service_tier": SECRET}},
        {"route_source": SECRET},
    ],
)
def test_detached_builder_marks_secret_context_ineligible(unsafe_kwargs):
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    kwargs = {
        "agent": SimpleNamespace(
            model="openai/gpt-5.6",
            provider="openai-api",
            base_url="https://api.openai.com/v1",
            api_mode="chat_completions",
        ),
        "gateway_session_key": "api:session:customer-7",
        "ephemeral_system_prompt": None,
        "requested_model": "openai/gpt-5.6",
        "requested_provider": "openai-api",
        "model_options": {"service_tier": "priority"},
        "route": None,
        "session_model": None,
        "requested_runtime": None,
        "route_source": "raw_request",
        "confirmed_runtime_lock": False,
    }
    kwargs.update(unsafe_kwargs)

    context, reason = adapter._build_api_detached_execution_context(**kwargs)

    assert context is None
    assert "not safely replayable" in reason
    assert "secret-like" in reason


def test_session_create_rejects_secret_model_before_any_row_is_written(
    session_db,
):
    with pytest.raises(ApiExecutionContextError, match="secret-like"):
        session_db.create_session(
            "secret-create",
            "api_server",
            model=SECRET,
        )

    assert session_db.get_session("secret-create") is None


@pytest.mark.parametrize(
    "unsafe_kwargs",
    [
        {"model": SECRET},
        {"provider": SECRET},
        {"model_options": {"service_tier": SECRET}},
        {"route_source": "untrusted_request_source"},
    ],
)
def test_runtime_lock_rejection_leaves_existing_row_byte_stable(
    session_db,
    unsafe_kwargs,
):
    session_id = session_db.create_session(
        "runtime-lock-stability",
        "api_server",
        model="openai/gpt-5.6",
        model_config={"_branched_from": "parent"},
        system_prompt="stable prompt",
    )
    before = copy.deepcopy(session_db.get_session(session_id))
    kwargs = {
        "model": "anthropic/claude-opus-4.6",
        "provider": "anthropic",
        "model_options": {"service_tier": "priority"},
        "route_source": "raw_request",
        "confirmed": True,
    }
    kwargs.update(unsafe_kwargs)

    with pytest.raises(ApiExecutionContextError):
        session_db.update_session_runtime_lock(session_id, **kwargs)

    after = session_db.get_session(session_id)
    assert after["model"] == before["model"]
    assert after["model_config"] == before["model_config"]
    assert after["system_prompt"] == before["system_prompt"]


def test_runtime_lock_persists_only_canonical_safe_values(session_db):
    session_id = session_db.create_session(
        "canonical-runtime-lock",
        "api_server",
        model="openai/gpt-5.6",
    )

    session_db.update_session_runtime_lock(
        session_id,
        model=" anthropic/claude-opus-4.6 ",
        provider=" Anthropic ",
        model_options={"service_tier": " PRIORITY "},
        route_source=" RAW_REQUEST ",
        confirmed=True,
    )

    row = session_db.get_session(session_id)
    lock = _stored_model_config(row)["browser_model_lock"]
    assert row["model"] == "anthropic/claude-opus-4.6"
    assert lock["model"] == "anthropic/claude-opus-4.6"
    assert lock["provider"] == "anthropic"
    assert lock["model_options"]["service_tier"] == "priority"
    assert lock["route_source"] == "raw_request"


@pytest.mark.parametrize("method", ["update_session_model", "update_session_meta"])
def test_direct_session_model_updates_reject_secret_without_mutation(
    session_db,
    method,
):
    session_id = session_db.create_session(
        f"direct-{method}",
        "api_server",
        model="openai/gpt-5.6",
        model_config={"stable": True},
        system_prompt="stable prompt",
    )
    before = copy.deepcopy(session_db.get_session(session_id))

    with pytest.raises(ApiExecutionContextError, match="secret-like"):
        if method == "update_session_meta":
            session_db.update_session_meta(
                session_id,
                json.dumps({"changed": True}),
                model=SECRET,
            )
        else:
            session_db.update_session_model(session_id, SECRET)

    after = session_db.get_session(session_id)
    assert after["model"] == before["model"]
    assert after["model_config"] == before["model_config"]
    assert after["system_prompt"] == before["system_prompt"]


def test_model_config_rejects_nested_credential_before_create(session_db):
    with pytest.raises(ApiExecutionContextError, match="secret-like"):
        session_db.create_session(
            "secret-config-create",
            "api_server",
            model="openai/gpt-5.6",
            model_config={
                "provider": "openai",
                "nested": {"api_key": "opaque-value"},
            },
        )

    assert session_db.get_session("secret-config-create") is None


def test_explicit_normal_service_tier_survives_canonical_storage(session_db):
    session_db.create_session(
        "explicit-normal",
        "tui",
        model="openai/gpt-5.6",
        model_config={
            "provider": "openai",
            "service_tier": "default",
        },
    )

    config = _stored_model_config(session_db.get_session("explicit-normal"))
    assert config["service_tier"] == "normal"


def test_compression_rejects_unsafe_child_before_parent_close(session_db):
    session_db.create_session(
        "compression-parent",
        "api_server",
        model="openai/gpt-5.6",
    )

    with pytest.raises(ApiExecutionContextError, match="secret-like"):
        session_db.publish_compression_child(
            parent_session_id="compression-parent",
            child_session_id="compression-child",
            source="api_server",
            messages=[{"role": "user", "content": "handoff"}],
            model=SECRET,
            require_compression_lease=False,
        )

    assert session_db.get_session("compression-child") is None
    assert session_db.get_session("compression-parent")["ended_at"] is None


def test_billing_and_usage_route_rejection_is_non_mutating(session_db):
    session_db.create_session(
        "usage-route",
        "api_server",
        model="openai/gpt-5.6",
    )
    before = copy.deepcopy(session_db.get_session("usage-route"))

    with pytest.raises(ApiExecutionContextError, match="secret-like"):
        session_db.update_session_billing_route(
            "usage-route",
            provider=SECRET,
            base_url="https://api.openai.com/v1",
        )
    with pytest.raises(ApiExecutionContextError, match="secret-like"):
        session_db.update_token_counts(
            "usage-route",
            input_tokens=7,
            model=SECRET,
            billing_provider="openai",
            billing_base_url="https://api.openai.com/v1",
        )
    session_db.record_auxiliary_usage(
        "usage-route",
        "vision",
        model=SECRET,
        billing_provider="openai",
        billing_base_url="https://api.openai.com/v1",
        input_tokens=9,
    )

    after = session_db.get_session("usage-route")
    assert after["input_tokens"] == before["input_tokens"]
    assert after["billing_provider"] == before["billing_provider"]
    assert (
        session_db._conn.execute(
            "SELECT COUNT(*) FROM session_model_usage WHERE session_id = ?",
            ("usage-route",),
        ).fetchone()[0]
        == 0
    )


def test_import_rejects_unsafe_runtime_metadata_as_one_batch(session_db):
    result = session_db.import_sessions(
        [
            {
                "id": "safe-import-peer",
                "source": "api_server",
                "model": "openai/gpt-5.6",
                "messages": [],
            },
            {
                "id": "unsafe-import-peer",
                "source": "api_server",
                "model": SECRET,
                "messages": [],
            },
        ]
    )

    assert result["ok"] is False
    assert result["imported"] == 0
    assert session_db.get_session("safe-import-peer") is None
    assert session_db.get_session("unsafe-import-peer") is None
