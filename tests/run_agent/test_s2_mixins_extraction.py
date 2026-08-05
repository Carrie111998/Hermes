"""Regression: wave-1 mixin extraction of run_agent.py shard s2.

The pure methods moved verbatim into plugins/agent/mixins/* must behave
identically when reached through the ``AIAgent`` MRO (bare adapters built
with ``object.__new__`` + stub config, matching the repo's existing test
seam — see the ``_compile_mention_patterns``-style docstrings in the moved
methods).
"""

import re
from types import SimpleNamespace

from run_agent import AIAgent


def _bare_agent(**attrs):
    """Build an AIAgent without running __init__ (existing repo pattern)."""
    agent = object.__new__(AIAgent)
    for key, value in attrs.items():
        setattr(agent, key, value)
    return agent


# --- c1 reasoning heuristics --------------------------------------------

class TestStripThinkBlocks:
    def test_has_content_after_think_block_empty(self):
        agent = _bare_agent(_strip_think_blocks=lambda c: c)
        assert AIAgent._has_content_after_think_block(agent, "") is False

    def test_has_content_after_think_block_only_think(self):
        agent = _bare_agent(_strip_think_blocks=lambda c: "")
        assert AIAgent._has_content_after_think_block(agent, "<think>hi</think>") is False

    def test_has_content_after_think_block_real_text(self):
        agent = _bare_agent(_strip_think_blocks=lambda c: " answer")
        assert AIAgent._has_content_after_think_block(agent, "<think>hi</think> answer") is True

    def test_strip_think_blocks_forwarder(self):
        # Forwarder is exercised end-to-end by the runtime helpers; here we
        # only assert the MRO wiring exists and the method is callable.
        agent = _bare_agent()
        assert callable(AIAgent._strip_think_blocks.__get__(agent))


class TestHasNaturalResponseEnding:
    def test_empty(self):
        assert AIAgent._has_natural_response_ending("") is False

    def test_code_fence(self):
        assert AIAgent._has_natural_response_ending("some code ```") is True

    def test_emoji(self):
        assert AIAgent._has_natural_response_ending("done 🎉") is True

    def test_punctuation(self):
        assert AIAgent._has_natural_response_ending("we are done.") is True

    def test_bare_word(self):
        assert AIAgent._has_natural_response_ending("almost") is False


class TestOllamaGlamBackend:
    def test_ollama_port(self):
        agent = _bare_agent(model="glm-4", provider="openai", _base_url_lower="http://localhost:11434")
        assert AIAgent._is_ollama_glm_backend(agent) is True

    def test_ollama_provider(self):
        agent = _bare_agent(model="glm-4", provider="ollama", _base_url_lower="http://localhost:9999")
        assert AIAgent._is_ollama_glm_backend(agent) is True

    def test_non_glm(self):
        agent = _bare_agent(model="gpt-4o", provider="openai", _base_url_lower="http://localhost:11434")
        assert AIAgent._is_ollama_glm_backend(agent) is False


class TestShouldTreatStopAsTruncated:
    def _agent(self):
        return _bare_agent(
            model="glm-4",
            provider="ollama",
            _base_url_lower="http://localhost:11434",
            api_mode="chat_completions",
            _strip_think_blocks=lambda c: c,
        )

    def test_non_stop_finish(self):
        agent = self._agent()
        assert AIAgent._should_treat_stop_as_truncated(agent, "length", None) is False

    def test_no_tool_messages(self):
        agent = self._agent()
        assert AIAgent._should_treat_stop_as_truncated(
            agent, "stop", SimpleNamespace(content="hello world", tool_calls=None), messages=[]
        ) is False

    def test_short_text_not_truncated(self):
        agent = self._agent()
        assert AIAgent._should_treat_stop_as_truncated(
            agent, "stop", SimpleNamespace(content="hi", tool_calls=None),
            messages=[{"role": "tool"}],
        ) is False

    def test_truncated_looking(self):
        agent = self._agent()
        # Long text, tool message present, no natural ending => treated truncated
        assert AIAgent._should_treat_stop_as_truncated(
            agent, "stop", SimpleNamespace(content="word " * 30, tool_calls=None),
            messages=[{"role": "tool"}],
        ) is True


class TestLooksLikeCodexIntermediateAck:
    def test_forwarder_wired(self):
        agent = _bare_agent()
        assert callable(AIAgent._looks_like_codex_intermediate_ack.__get__(agent))


class TestExtractReasoning:
    def test_forwarder_wired(self):
        agent = _bare_agent()
        assert callable(AIAgent._extract_reasoning.__get__(agent))


# --- c6 trajectory -------------------------------------------------------

class TestSaveTrajectory:
    def test_disabled_returns_none(self):
        agent = _bare_agent(save_trajectories=False)
        assert AIAgent._save_trajectory(agent, [], "q", False) is None

    def test_format_tools_wired(self):
        agent = _bare_agent()
        assert callable(AIAgent._format_tools_for_system_message.__get__(agent))

    def test_convert_to_trajectory_wired(self):
        agent = _bare_agent()
        assert callable(AIAgent._convert_to_trajectory_format.__get__(agent))


# --- c7 api error --------------------------------------------------------

class TestCoerceApiErrorDetail:
    def test_string_passthrough(self):
        assert AIAgent._coerce_api_error_detail("boom") == "boom"

    def test_dict_message(self):
        assert AIAgent._coerce_api_error_detail({"message": "nope"}) == "nope"

    def test_nested_dict(self):
        assert AIAgent._coerce_api_error_detail({"error": {"message": "deep"}}) == "deep"

    def test_list(self):
        assert AIAgent._coerce_api_error_detail(["a", "b"]) == "a; b"

    def test_json_fallback(self):
        assert AIAgent._coerce_api_error_detail({"x": 1}) == '{"x": 1}'


class TestDecorateXaiEntitlementError:
    def test_no_double_decorate(self):
        first = AIAgent._decorate_xai_entitlement_error("does not have permission to run grok")
        assert "X Premium+" in first
        second = AIAgent._decorate_xai_entitlement_error(first)
        assert second == first

    def test_non_entitlement_untouched(self):
        assert AIAgent._decorate_xai_entitlement_error("plain failure") == "plain failure"


class TestIsEntitlementFailure:
    def test_grok_subscription(self):
        ctx = {"message": "You do not have an active Grok subscription"}
        assert AIAgent._is_entitlement_failure(ctx, 403) is True

    def test_wke_unauthenticated_is_not_entitlement(self):
        ctx = {"message": "The caller does not have permission [WKE=unauthenticated:token expired]"}
        assert AIAgent._is_entitlement_failure(ctx, 403) is False

    def test_wrong_status(self):
        ctx = {"message": "You do not have an active Grok subscription"}
        assert AIAgent._is_entitlement_failure(ctx, 500) is False


class TestSummarizeApiError:
    def test_cloudflare_title_extracted(self):
        err = Exception("<!DOCTYPE html><html><head><title>502 Bad Gateway</title></head></html>")
        summary = AIAgent._summarize_api_error(err)
        assert "502 Bad Gateway" in summary

    def test_empty_body_response_text_fallback(self):
        err = Exception("")
        err.status_code = 400
        err.body = {}
        err.response = SimpleNamespace(text='{"error": {"message": "model does not exist"}}')
        summary = AIAgent._summarize_api_error(err)
        assert "model does not exist" in summary

    def test_fallback_truncation(self):
        err = Exception("x" * 1000)
        summary = AIAgent._summarize_api_error(err)
        assert len(summary) < 600


class TestMaskApiKeyForLogs:
    def test_callable_entra(self):
        agent = _bare_agent()
        assert AIAgent._mask_api_key_for_logs(agent, lambda: "x") == "<entra-id-bearer>"

    def test_short_key(self):
        agent = _bare_agent()
        assert AIAgent._mask_api_key_for_logs(agent, "short") == "***"

    def test_long_key(self):
        agent = _bare_agent()
        masked = AIAgent._mask_api_key_for_logs(agent, "abcdefgh1234567890")
        assert masked == "abcdefgh...7890"

    def test_none(self):
        agent = _bare_agent()
        assert AIAgent._mask_api_key_for_logs(agent, None) is None


class TestCleanErrorMessage:
    def test_html_page(self):
        agent = _bare_agent()
        assert "HTML error page" in AIAgent._clean_error_message(agent, "<!DOCTYPE html><html>oops")

    def test_truncation(self):
        agent = _bare_agent()
        cleaned = AIAgent._clean_error_message(agent, " ".join(["word"] * 200))
        assert len(cleaned) <= 154

    def test_empty(self):
        agent = _bare_agent()
        assert AIAgent._clean_error_message(agent, "") == "Unknown error"


class TestExtractApiErrorContext:
    def test_forwarder_wired(self):
        assert callable(AIAgent._extract_api_error_context)


# --- c8 api hooks --------------------------------------------------------

class TestHookPayloadMaxChars:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("HERMES_PLUGIN_PAYLOAD_MAX_CHARS", raising=False)
        assert AIAgent._hook_payload_max_chars() == 50000

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("HERMES_PLUGIN_PAYLOAD_MAX_CHARS", "1234")
        assert AIAgent._hook_payload_max_chars() == 1234

    def test_invalid_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("HERMES_PLUGIN_PAYLOAD_MAX_CHARS", "garbage")
        assert AIAgent._hook_payload_max_chars() == 50000


class TestIsSensitiveHookKey:
    def test_exact(self):
        assert AIAgent._is_sensitive_hook_key("authorization") is True
        assert AIAgent._is_sensitive_hook_key("API_KEY") is True

    def test_suffix(self):
        assert AIAgent._is_sensitive_hook_key("openai_api_key") is True

    def test_plain(self):
        assert AIAgent._is_sensitive_hook_key("model") is False

    def test_non_string(self):
        assert AIAgent._is_sensitive_hook_key(123) is False


class TestHookJsonable:
    def test_primitives(self):
        assert AIAgent._hook_jsonable(None) is None
        assert AIAgent._hook_jsonable(42) == 42
        assert AIAgent._hook_jsonable("hi") == "hi"

    def test_string_truncation(self):
        out = AIAgent._hook_jsonable("x" * 9000)
        assert len(out) < 8100

    def test_bytes(self):
        assert AIAgent._hook_jsonable(b"abc") == "<3 bytes>"

    def test_sensitive_key_redacted(self):
        out = AIAgent._hook_jsonable({"api_key": "sk-secret"})
        assert out == {"api_key": "<redacted>"}

    def test_simplenamespace(self):
        out = AIAgent._hook_jsonable(SimpleNamespace(a=1, b="two"))
        assert out == {"a": 1, "b": "two"}

    def test_depth_limit(self):
        deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {"i": "end"}}}}}}}}}
        out = AIAgent._hook_jsonable(deep)
        assert "depth limit" in str(out)


class TestSanitizeHookPayload:
    def test_small_payload_roundtrip(self):
        out = AIAgent._sanitize_hook_payload({"method": "POST", "body": {"model": "gpt"}})
        assert out == {"method": "POST", "body": {"model": "gpt"}}

    def test_huge_payload_truncated(self):
        # 200 x 8000-char strings: >50000 chars even after the retry pass
        # (max_string=1000, max_sequence=50) => _truncated marker.
        out = AIAgent._sanitize_hook_payload({"data": ["x" * 8000] * 200})
        assert out.get("_truncated") is True
        assert out.get("original_type") == "dict"
        assert out.get("preview")


class TestApiRequestPayloadForHook:
    def test_excludes_timeout_and_client(self):
        agent = _bare_agent()
        out = AIAgent._api_request_payload_for_hook(
            agent, {"timeout": 30, "http_client": object(), "model": "gpt"}
        )
        assert out["body"] == {"model": "gpt"}


class TestApiResponsePayloadForHook:
    def test_basic(self):
        agent = _bare_agent()
        response = SimpleNamespace(model="gpt-4o", usage=None)
        msg = SimpleNamespace(role="assistant", content="hi", tool_calls=None)
        out = AIAgent._api_response_payload_for_hook(
            agent, response, msg, finish_reason="stop"
        )
        assert out["model"] == "gpt-4o"
        assert out["finish_reason"] == "stop"
        assert out["assistant_message"]["content"] == "hi"
        assert out["usage"] is None


class TestUsageSummaryForApiRequestHook:
    def test_none_response(self):
        agent = _bare_agent(provider="openai", api_mode="chat_completions")
        assert AIAgent._usage_summary_for_api_request_hook(agent, None) is None

    def test_no_usage(self):
        agent = _bare_agent(provider="openai", api_mode="chat_completions")
        assert AIAgent._usage_summary_for_api_request_hook(agent, SimpleNamespace(usage=None)) is None

    def test_usage_summary(self):
        agent = _bare_agent(provider="openai", api_mode="chat_completions")
        response = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=3, total_tokens=8)
        )
        out = AIAgent._usage_summary_for_api_request_hook(agent, response)
        assert out["prompt_tokens"] == 5
        assert out["total_tokens"] == 8


class TestInvokeApiRequestErrorHook:
    def test_no_hook_noop(self, monkeypatch):
        agent = _bare_agent(session_id="s", platform="cli", model="m", provider="p",
                            base_url="http://x", api_mode="chat_completions")
        monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda *a, **k: False)
        # Should not raise and return None
        assert AIAgent._invoke_api_request_error_hook(
            agent, task_id="t", turn_id="u", api_request_id="r", api_call_count=1,
            api_start_time=0.0, api_kwargs={}, error_type="timeout", error_message="boom",
        ) is None


class TestDumpApiRequestDebug:
    def test_forwarder_wired(self):
        agent = _bare_agent()
        assert callable(AIAgent._dump_api_request_debug.__get__(agent))


# --- c3 background review ------------------------------------------------

class TestSummarizeBackgroundReviewActions:
    def test_forwarder_wired(self):
        assert callable(AIAgent._summarize_background_review_actions)


class TestBuildMemoryWriteMetadata:
    def test_forwarder_wired(self):
        agent = _bare_agent()
        assert callable(AIAgent._build_memory_write_metadata.__get__(agent))


class TestSpawnBackgroundReview:
    def test_forwarder_wired(self):
        agent = _bare_agent()
        assert callable(AIAgent._spawn_background_review.__get__(agent))


# --- mixin import wiring -------------------------------------------------

def test_mixin_bases_present():
    from plugins.agent.mixins.api_error_mixin import ApiErrorMixin
    from plugins.agent.mixins.api_hooks_mixin import ApiHooksMixin
    from plugins.agent.mixins.background_review_mixin import BackgroundReviewMixin
    from plugins.agent.mixins.reasoning_heuristics_mixin import ReasoningHeuristicsMixin
    from plugins.agent.mixins.trajectory_mixin import TrajectoryMixin
    assert issubclass(AIAgent, ReasoningHeuristicsMixin)
    assert issubclass(AIAgent, BackgroundReviewMixin)
    assert issubclass(AIAgent, TrajectoryMixin)
    assert issubclass(AIAgent, ApiErrorMixin)
    assert issubclass(AIAgent, ApiHooksMixin)


def test_moved_methods_still_reachable_on_aia_agent():
    for name in [
        "_has_content_after_think_block", "_strip_think_blocks",
        "_has_natural_response_ending", "_is_ollama_glm_backend",
        "_should_treat_stop_as_truncated", "_looks_like_codex_intermediate_ack",
        "_extract_reasoning",
        "_summarize_background_review_actions", "_spawn_background_review",
        "_build_memory_write_metadata",
        "_format_tools_for_system_message", "_convert_to_trajectory_format",
        "_save_trajectory",
        "_is_entitlement_failure", "_decorate_xai_entitlement_error",
        "_coerce_api_error_detail", "_summarize_api_error",
        "_mask_api_key_for_logs", "_clean_error_message",
        "_extract_api_error_context",
        "_usage_summary_for_api_request_hook", "_hook_payload_max_chars",
        "_is_sensitive_hook_key", "_hook_jsonable", "_sanitize_hook_payload",
        "_api_request_payload_for_hook", "_api_response_payload_for_hook",
        "_invoke_api_request_error_hook", "_dump_api_request_debug",
    ]:
        assert hasattr(AIAgent, name), f"{name} missing from AIAgent MRO"
