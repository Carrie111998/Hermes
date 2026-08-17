"""Tests for issue #88087: model switch must not break implicit prefix cache.

On implicit longest-prefix-cache backends (local llama.cpp / Ollama /
LM Studio) a /model switch that rewrites the volatile ``Model:`` /
``Provider:`` / ``Platform:`` lines in the system prompt invalidates the
KV-cache prefix for the entire conversation history (measured 408s on a
~45k-token session). The identity lines are informational only — they are
surfaced through the platform footer, not read from the prompt — so they
are omitted when the provider has no explicit ``cache_control`` breakpoint
API (``_use_native_cache_layout`` False).
"""

from types import SimpleNamespace

from agent.system_prompt import build_system_prompt_parts


def _make_agent(**overrides):
    """Minimal agent stub mirroring tests/agent/test_system_prompt.py."""
    base = dict(
        load_soul_identity=False,
        skip_context_files=False,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        model="qwen-27b",
        provider="custom:litellm",
        platform="telegram",
        pass_session_id=True,
        session_id="sess-test",
        _use_native_cache_layout=False,
        _bot_chat_timeless_prompt=False,
        _memory_enabled=False,
        _user_profile_enabled=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestImplicitCacheIdentityOmission:
    def test_implicit_cache_omits_identity_lines(self):
        """Implicit backends must NOT embed Model/Provider/Platform lines."""
        agent = _make_agent(_use_native_cache_layout=False)
        volatile = build_system_prompt_parts(agent)["volatile"]
        assert "Model:" not in volatile
        assert "Provider:" not in volatile
        assert "Platform:" not in volatile
        assert "Session ID: sess-test" in volatile

    def test_model_switch_prompt_byte_identical(self):
        """A model/provider switch must not change the volatile tail."""
        before = build_system_prompt_parts(
            _make_agent(model="qwen-27b", provider="custom:litellm",
                        _use_native_cache_layout=False)
        )["volatile"]
        after = build_system_prompt_parts(
            _make_agent(model="deepseek-v4-flash", provider="custom:command-code",
                        _use_native_cache_layout=False)
        )["volatile"]
        assert before == after, "volatile tail changed on model switch!"

    def test_explicit_cache_keeps_identity_lines(self):
        """Explicit cache_control backends keep the informational lines."""
        agent = _make_agent(_use_native_cache_layout=True)
        volatile = build_system_prompt_parts(agent)["volatile"]
        assert "Model: qwen-27b" in volatile
        assert "Provider: custom:litellm" in volatile
        assert "Platform: telegram" in volatile

    def test_switch_on_explicit_cache_changes_tail(self):
        """On explicit-cache backends a model switch does change the tail."""
        before = build_system_prompt_parts(
            _make_agent(model="qwen-27b", provider="custom:litellm",
                        _use_native_cache_layout=True)
        )["volatile"]
        after = build_system_prompt_parts(
            _make_agent(model="deepseek-v4-flash", provider="custom:command-code",
                        _use_native_cache_layout=True)
        )["volatile"]
        assert before != after
        assert "Model: deepseek-v4-flash" in after
