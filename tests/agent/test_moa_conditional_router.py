"""Behavioural tests for the native conditional MoA router."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agent.moa_router import decide_moa_route
from hermes_cli.moa_config import normalize_moa_config


def _response(content: str):
    message = SimpleNamespace(content=content, tool_calls=[])
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=None,
        model="fake-model",
    )


def test_auto_router_escalates_deliberative_prompt_and_keeps_simple_prompt_direct():
    config = {"mode": "auto", "threshold": 3}

    direct = decide_moa_route("What time is the meeting?", config)
    fused = decide_moa_route(
        "Compare the two architectures, identify security risks, and recommend "
        "which design we should use with reasons.",
        config,
    )

    assert direct.fanout is False
    assert direct.mode == "auto"
    assert fused.fanout is True
    assert fused.score >= 3
    assert "deliberation" in fused.reasons


def test_auto_router_fails_safe_for_ambiguous_and_non_english_prompts():
    ambiguous = decide_moa_route("Explain the implications.", {"mode": "auto"})
    non_english = decide_moa_route("请分析这个方案。", {"mode": "auto"})

    assert ambiguous.fanout is True
    assert non_english.fanout is True
    assert "ambiguous" in ambiguous.reasons


def test_explicit_modes_are_backward_compatible_and_deterministic():
    assert decide_moa_route("hello", {"mode": "always"}).fanout is True
    assert decide_moa_route("review every security risk", {"mode": "never"}).fanout is False


def test_simple_prefix_does_not_bypass_high_stakes_deliberation():
    decision = decide_moa_route(
        "Translate this legal contract and assess the risk.",
        {"mode": "auto", "threshold": 3},
    )

    assert decision.fanout is True
    assert "high-stakes" in decision.reasons


def test_moa_config_normalizes_per_preset_router_without_changing_default_behaviour():
    configured = normalize_moa_config(
        {
            "presets": {
                "review": {
                    "routing": {"mode": "auto", "threshold": "4"},
                }
            }
        }
    )
    defaulted = normalize_moa_config({})

    assert configured["presets"]["review"]["routing"] == {
        "mode": "auto",
        "threshold": 4,
    }
    assert defaulted["presets"]["default"]["routing"] == {
        "mode": "always",
        "threshold": 3,
    }


def test_normalized_flattened_view_exposes_active_routing_policy():
    cfg = normalize_moa_config(
        {
            "default_preset": "review",
            "presets": {
                "review": {
                    "routing": {"mode": "auto", "threshold": 5},
                }
            },
        }
    )

    assert cfg["routing"] == {"mode": "auto", "threshold": 5}


def test_auto_router_skips_advisors_for_simple_turn_but_fans_out_for_deliberation(
    monkeypatch, tmp_path
):
    from agent.moa_loop import MoAClient

    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        """
moa:
  default_preset: review
  presets:
    review:
      routing:
        mode: auto
        threshold: 3
      reference_models:
        - provider: openai-codex
          model: gpt-5.5
      aggregator:
        provider: bedrock
        model: anthropic.claude-opus
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        return _response("advice" if kwargs["task"] == "moa_reference" else "answer")

    monkeypatch.setattr("agent.moa_loop.call_llm", fake_call_llm)
    simple = MoAClient("review")
    simple.chat.completions.create(
        messages=[{"role": "user", "content": "What time is the meeting?"}]
    )
    assert [call["task"] for call in calls] == ["moa_aggregator"]
    assert calls[0]["provider"] == "bedrock"

    calls.clear()
    complex_turn = MoAClient("review")
    complex_turn.chat.completions.create(
        messages=[
            {
                "role": "user",
                "content": (
                    "Compare both architectures, identify the security risks, "
                    "and recommend the best production design."
                ),
            }
        ]
    )
    assert [call["task"] for call in calls] == [
        "moa_reference",
        "moa_aggregator",
    ]
    assert [call["provider"] for call in calls] == ["openai-codex", "bedrock"]


def test_dashboard_payload_round_trips_router_configuration():
    from hermes_cli.web_models import MoaPresetPayload

    payload = MoaPresetPayload(routing={"mode": "auto", "threshold": 5})

    assert payload.model_dump()["routing"] == {"mode": "auto", "threshold": 5}


def test_dashboard_rejects_invalid_router_policy():
    from hermes_cli.web_models import MoaPresetPayload

    with pytest.raises(ValidationError):
        MoaPresetPayload(routing={"mode": "guess", "threshold": 3})
    with pytest.raises(ValidationError):
        MoaPresetPayload(routing={"mode": "auto", "threshold": 0})


def test_malformed_on_disk_router_policy_fails_to_historical_fanout():
    cfg = normalize_moa_config(
        {"presets": {"review": {"routing": {"mode": "guess", "threshold": 0}}}}
    )

    assert cfg["presets"]["review"]["routing"] == {
        "mode": "always",
        "threshold": 1,
    }


def test_non_finite_on_disk_router_threshold_falls_back_safely():
    cfg = normalize_moa_config(
        {"presets": {"review": {"routing": {"mode": "auto", "threshold": "inf"}}}}
    )

    assert cfg["presets"]["review"]["routing"] == {
        "mode": "auto",
        "threshold": 3,
    }


def test_route_decision_is_pinned_for_retries_within_one_user_turn(monkeypatch, tmp_path):
    from agent.moa_loop import MoAClient
    from agent.moa_router import MoARouteDecision

    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        """
moa:
  presets:
    review:
      routing: {mode: auto, threshold: 3}
      reference_models:
        - {provider: openai-codex, model: gpt-5.5}
      aggregator: {provider: bedrock, model: anthropic.claude-opus}
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    decisions = []

    def unstable_router(*_args, **_kwargs):
        decisions.append(True)
        return MoARouteDecision(
            fanout=len(decisions) == 1,
            mode="auto",
            score=3,
            reasons=("test",),
        )

    monkeypatch.setattr("agent.moa_router.decide_moa_route", unstable_router)
    monkeypatch.setattr("agent.moa_loop.call_llm", lambda **_kwargs: _response("ok"))
    client = MoAClient("review")
    messages = [{"role": "user", "content": "Review this architecture."}]

    client.chat.completions.create(messages=messages)
    client.chat.completions.create(messages=messages)

    assert len(decisions) == 1
    assert client.chat.completions.last_route_decision.fanout is True


def test_route_identity_ignores_cache_decoration(monkeypatch, tmp_path):
    from agent.moa_loop import MoAClient
    from agent.moa_router import MoARouteDecision

    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        """
moa:
  presets:
    review:
      routing: {mode: auto, threshold: 3}
      reference_models: [{provider: openai-codex, model: gpt-5.5}]
      aggregator: {provider: bedrock, model: anthropic.claude-opus}
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    decisions = []

    def router(*_args, **_kwargs):
        decisions.append(True)
        return MoARouteDecision(True, "auto", 3, ("test",))

    monkeypatch.setattr("agent.moa_router.decide_moa_route", router)
    monkeypatch.setattr("agent.moa_loop.call_llm", lambda **_kwargs: _response("ok"))
    client = MoAClient("review")
    client.chat.completions.create(messages=[{"role": "user", "content": "Review this."}])
    client.chat.completions.create(
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Review this.",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        ]
    )

    assert len(decisions) == 1


def test_prepared_request_restores_route_pin_on_fresh_client(monkeypatch, tmp_path):
    from agent.moa_loop import MoAClient
    from agent.moa_router import MoARouteDecision

    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        """
moa:
  presets:
    review:
      routing: {mode: auto, threshold: 3}
      reference_models: [{provider: openai-codex, model: gpt-5.5}]
      aggregator: {provider: bedrock, model: anthropic.claude-opus}
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    decisions = []

    def unstable_router(*_args, **_kwargs):
        decisions.append(True)
        return MoARouteDecision(len(decisions) == 1, "auto", 3, ("test",))

    monkeypatch.setattr("agent.moa_router.decide_moa_route", unstable_router)
    monkeypatch.setattr("agent.moa_loop.call_llm", lambda **_kwargs: _response("ok"))
    messages = [{"role": "user", "content": "Review this architecture."}]
    prepared = MoAClient("review").chat.completions.create(
        messages=messages,
        _moa_prepare_only=True,
    )
    rebuilt = MoAClient("review")
    rebuilt.chat.completions.create(_moa_prepared_request=prepared)
    rebuilt.chat.completions.create(messages=messages)

    assert len(decisions) == 1
    assert rebuilt.chat.completions.last_route_decision.fanout is True


def test_rebased_prepared_request_rekeys_route_pin_to_compacted_transcript(
    monkeypatch, tmp_path
):
    from agent.moa_loop import MoAClient
    from agent.moa_router import MoARouteDecision

    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        """
moa:
  presets:
    review:
      routing: {mode: auto, threshold: 3}
      reference_models: [{provider: openai-codex, model: gpt-5.5}]
      aggregator: {provider: bedrock, model: anthropic.claude-opus}
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    decisions = []

    def unstable_router(*_args, **_kwargs):
        decisions.append(True)
        return MoARouteDecision(len(decisions) == 1, "auto", 3, ("test",))

    monkeypatch.setattr("agent.moa_router.decide_moa_route", unstable_router)
    monkeypatch.setattr("agent.moa_loop.call_llm", lambda **_kwargs: _response("ok"))
    original = [
        {"role": "user", "content": "Earlier context."},
        {"role": "assistant", "content": "Acknowledged."},
        {"role": "user", "content": "Review this architecture."},
    ]
    compacted = [
        {"role": "system", "content": "Compacted earlier context."},
        {"role": "user", "content": "Review this architecture."},
    ]
    client = MoAClient("review")
    prepared = client.chat.completions.create(
        messages=original,
        _moa_prepare_only=True,
    )
    rebased = client.chat.completions.rebase_prepared_request(prepared, compacted)
    client.chat.completions.create(_moa_prepared_request=rebased)
    client.chat.completions.create(messages=compacted)

    assert len(decisions) == 1
    assert client.chat.completions.last_route_decision.fanout is True


def test_conditional_routes_preserve_canonical_provider_oauth_runtime(monkeypatch, tmp_path):
    from agent import moa_loop
    from agent.moa_loop import MoAClient

    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        """
moa:
  presets:
    direct:
      routing: {mode: never}
      reference_models: [{provider: openai-codex, model: gpt-5.5}]
      aggregator: {provider: azure-foundry, model: gpt-5.5}
    fused:
      routing: {mode: always}
      reference_models: [{provider: openai-codex, model: gpt-5.5}]
      aggregator: {provider: azure-foundry, model: gpt-5.5}
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    token_callable = lambda: "oauth-token"  # noqa: E731 - identity is asserted below
    resolved = []

    def resolve_runtime_provider(*, requested, target_model):
        resolved.append((requested, target_model))
        return {
            "provider": requested,
            "model": target_model,
            "base_url": f"https://{requested}.internal.example/v1",
            "api_key": token_callable,
            "api_mode": "responses" if requested == "azure-foundry" else "chat_completions",
            "request_overrides": {"extra_body": {"tenant": "internal"}},
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        resolve_runtime_provider,
    )
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        return _response("ok")

    monkeypatch.setattr(moa_loop, "call_llm", fake_call_llm)
    with moa_loop._runtime_cache_lock:
        moa_loop._runtime_cache.clear()

    MoAClient("direct").chat.completions.create(
        messages=[{"role": "user", "content": "Review this."}]
    )
    assert resolved == [("azure-foundry", "gpt-5.5")]
    assert calls[0]["api_key"] is token_callable
    assert calls[0]["base_url"] == "https://azure-foundry.internal.example/v1"
    assert calls[0]["api_mode"] == "responses"
    assert calls[0]["extra_body"] == {"tenant": "internal"}

    # The cached runtime must be immutable from a caller's perspective. MoA's
    # aggregator path consumes request overrides from its request-local copy;
    # a second call must still receive the resolver-supplied extra_body.
    MoAClient("direct").chat.completions.create(
        messages=[{"role": "user", "content": "Review this again."}]
    )
    assert resolved == [("azure-foundry", "gpt-5.5")]
    assert calls[1]["extra_body"] == {"tenant": "internal"}
    assert calls[1]["api_key"] is token_callable

    resolved.clear()
    calls.clear()
    with moa_loop._runtime_cache_lock:
        moa_loop._runtime_cache.clear()
    MoAClient("fused").chat.completions.create(
        messages=[{"role": "user", "content": "Review this."}]
    )
    assert resolved == [
        ("openai-codex", "gpt-5.5"),
        ("azure-foundry", "gpt-5.5"),
    ]
    assert calls[-1]["api_key"] is token_callable
    assert calls[-1]["api_mode"] == "responses"
