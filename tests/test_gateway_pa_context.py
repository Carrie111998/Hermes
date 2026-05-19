from pathlib import Path

from gateway.config import Platform
from gateway.run import (
    GatewayRunner,
    _merge_pa_toolsets,
    _pa_max_output_tokens,
    _record_pa_agent_action,
    _pa_slash_denial,
    _pa_tenant_slug,
    _render_pa_ephemeral_prompt,
    _resolve_pa_context,
    _source_pa_metadata,
)
from gateway.session import SessionSource


FIXTURE = Path(__file__).parent / "fixtures" / "pa" / "bobby_tgg_constitution.yaml"


def _config():
    return {
        "pa": {
            "enabled": True,
            "constitution_path": str(FIXTURE),
        }
    }


def _source(chat_id: str) -> SessionSource:
    return SessionSource(
        platform=Platform.WHATSAPP,
        chat_id=chat_id,
        chat_name=chat_id,
        chat_type="group",
        user_id="sky",
        user_name="Sky",
    )


def test_gateway_pa_context_selects_job_briefs_from_chat_metadata():
    ops = _resolve_pa_context(
        _config(),
        {},
        _source_pa_metadata(_source("tgg-ops"), session_id="s1", session_key="k1"),
    )
    management = _resolve_pa_context(
        _config(),
        {},
        _source_pa_metadata(_source("tgg-management"), session_id="s2", session_key="k2"),
    )

    assert ops is not None
    assert management is not None
    assert ops.constitution.id == "bobby_tgg"
    assert ops.identity_hash == management.identity_hash
    assert ops.job_type == "tgg_ops_ingest"
    assert management.job_type == "tgg_management"
    assert ops.job_hash != management.job_hash


def test_gateway_pa_event_job_type_can_override_selector():
    metadata = _source_pa_metadata(
        _source("tgg-ops"),
        session_id="s1",
        session_key="k1",
        pa_job_type="tgg_management",
    )

    resolved = _resolve_pa_context(_config(), {}, metadata)

    assert resolved is not None
    assert resolved.job_type == "tgg_management"


def test_gateway_pa_prompt_and_toolsets_enter_cache_signature():
    ops = _resolve_pa_context(
        _config(),
        {},
        _source_pa_metadata(_source("tgg-ops"), session_id="s1", session_key="k1"),
    )
    management = _resolve_pa_context(
        _config(),
        {},
        _source_pa_metadata(_source("tgg-management"), session_id="s2", session_key="k2"),
    )
    assert ops is not None
    assert management is not None

    ops_prompt = _render_pa_ephemeral_prompt(ops)
    management_prompt = _render_pa_ephemeral_prompt(management)
    ops_enabled, ops_disabled = _merge_pa_toolsets(["web"], ["clarify"], ops)
    management_enabled, management_disabled = _merge_pa_toolsets(["web"], ["clarify"], management)

    assert "Personal Assistant Identity" in ops_prompt
    assert "TGG Operations Ingest" in ops_prompt
    assert "TGG Management Brief" in management_prompt
    assert ops_enabled == ["memory", "file", "pa-business", "pa-photo-pair"]
    assert management_enabled == ["memory", "file", "web", "pa-business"]
    assert ops_disabled == ["clarify", "web", "shell"]
    assert management_disabled == ["clarify", "shell"]
    assert _pa_tenant_slug(ops) == "tgg"
    assert _pa_max_output_tokens(ops) == 2048
    assert _pa_max_output_tokens(management) == 8192
    assert _pa_slash_denial(ops, "help") == "Command `/help` is not available here."
    assert _pa_slash_denial(management, "pause") is None
    assert _pa_slash_denial(management, "sethome") == "Command `/sethome` is not available here."

    runtime = {"api_key": "key", "base_url": "https://example.invalid", "provider": "test"}
    ops_sig = GatewayRunner._agent_config_signature("model", runtime, ops_enabled, ops_prompt)
    management_sig = GatewayRunner._agent_config_signature(
        "model",
        runtime,
        management_enabled,
        management_prompt,
    )
    assert ops_sig != management_sig


def test_gateway_pa_prompt_uses_context_business_bridge(monkeypatch):
    import tools.pa_business_tools as pa_business_tools

    ops = _resolve_pa_context(
        _config(),
        {},
        _source_pa_metadata(_source("tgg-ops"), session_id="s1", session_key="k1"),
    )
    assert ops is not None
    sentinel = object()
    seen = {}

    def fake_load_business_bridge_config(config=None, *, pa_context=None):
        assert config is None
        assert pa_context is ops
        return sentinel

    def fake_render_agent_config_prompt(config=None, *, operation="agent_config_read"):
        seen["config"] = config
        seen["operation"] = operation
        return "## Live Behavior Configuration\n- behavior.tone = brief"

    monkeypatch.setattr(
        pa_business_tools,
        "load_business_bridge_config",
        fake_load_business_bridge_config,
    )
    monkeypatch.setattr(
        pa_business_tools,
        "render_agent_config_prompt",
        fake_render_agent_config_prompt,
    )

    prompt = _render_pa_ephemeral_prompt(ops)

    assert "Live Behavior Configuration" in prompt
    assert seen == {"config": sentinel, "operation": "agent_config_read"}


def test_gateway_pa_action_logging_uses_context_business_bridge(monkeypatch):
    import tools.pa_business_tools as pa_business_tools

    ops = _resolve_pa_context(
        _config(),
        {},
        _source_pa_metadata(_source("tgg-ops"), session_id="s1", session_key="k1"),
    )
    assert ops is not None
    sentinel = object()
    seen = {}

    def fake_load_business_bridge_config(config=None, *, pa_context=None):
        assert config is None
        assert pa_context is ops
        return sentinel

    def fake_record_agent_action(**kwargs):
        seen.update(kwargs)
        return True

    monkeypatch.setattr(
        pa_business_tools,
        "load_business_bridge_config",
        fake_load_business_bridge_config,
    )
    monkeypatch.setattr(pa_business_tools, "record_agent_action", fake_record_agent_action)

    ok = _record_pa_agent_action(
        ops,
        action_type="observation",
        payload={"message": "hello"},
        source="whatsapp:tgg-ops",
        status="pending",
        turn_id="turn-1",
    )

    assert ok is True
    assert seen["config"] is sentinel
    assert seen["agent_id"] == "bobby"
    assert seen["engagement_id"] == "c432bb00-3f30-4344-b9b2-b962220cd512"
    assert seen["action_type"] == "observation"
