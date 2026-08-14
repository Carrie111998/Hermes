"""Tests for the local-Ollama reasoning capability gate.

Before this fix, ``AIAgent._supports_reasoning_extra_body()`` only probed
Ollama's ``/api/show`` "thinking" capability for the ``ollama.com`` hostname
(Ollama Cloud). A local Ollama server (``http://localhost:11434/v1`` and
equivalents) fell through that check and hit the final
``if "openrouter" not in self._base_url_lower: return False`` — so the gate
never probed at all and always reported "no reasoning support", regardless of
what the local model actually declared.

Combined with the separate ``CustomProfile.build_api_kwargs_extras`` defect
(it never consulted ``supports_reasoning`` and emitted ``reasoning_effort``
unconditionally — covered in
``tests/plugins/model_providers/test_custom_profile.py``), a profile whose
``fallback_providers`` pointed at local Ollama running a non-thinking model
(e.g. ``qwen2.5:7b``) failed with::

    HTTP 400: "qwen2.5:7b" does not support thinking

Fixing only the ``CustomProfile`` side would have been a regression on its
own: since the gate always returned False for localhost, a local
thinking-capable model (e.g. ``deepseek-r1``) would stop receiving
``reasoning_effort`` entirely. Both fixes are required together.

The gate reads exactly one attribute off ``self`` (``_base_url_lower``) plus
two cached probe helpers, so these tests bind the unbound method to a stub
instead of constructing a real ``AIAgent`` — no provider configuration, no
network, no credentials.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _gate(base_url: str, *, probe_result: bool = False, record: list | None = None):
    """Invoke ``_supports_reasoning_extra_body`` against a minimal stub.

    ``record`` collects a marker whenever the Ollama probe is consulted, which
    is what proves the local branch is reached at all (it previously wasn't).
    """
    from run_agent import AIAgent

    def _probe():
        if record is not None:
            record.append(base_url)
        return probe_result

    stub = SimpleNamespace(
        _base_url_lower=base_url.lower(),
        provider="custom",
        model="test-model",
        _ollama_supports_thinking_cached=_probe,
        _lmstudio_reasoning_options_cached=lambda: [],
    )
    return AIAgent._supports_reasoning_extra_body(stub)


LOCAL_URLS = [
    "http://localhost:11434/v1",
    "http://127.0.0.1:11434/v1",
    "http://localhost:11434",
    "http://192.168.1.50:11434/v1",
]


class TestLocalOllamaReasoningGate:
    """Local Ollama is probed exactly like Ollama Cloud."""

    @pytest.mark.parametrize("base_url", LOCAL_URLS)
    def test_model_without_thinking_suppresses_reasoning(self, base_url):
        """qwen2.5:7b-style model: probe says no → gate says no."""
        seen: list = []
        assert _gate(base_url, probe_result=False, record=seen) is False
        assert seen == [base_url], "the Ollama probe must actually be consulted"

    @pytest.mark.parametrize("base_url", LOCAL_URLS)
    def test_model_with_thinking_allows_reasoning(self, base_url):
        """Non-regression: deepseek-r1-style model still gets reasoning.

        This is the half that a CustomProfile-only fix would have broken.
        """
        seen: list = []
        assert _gate(base_url, probe_result=True, record=seen) is True
        assert seen == [base_url]

    def test_ollama_cloud_still_probed(self):
        """The pre-existing ollama.com behaviour is untouched."""
        seen: list = []
        assert _gate("https://ollama.com/v1", probe_result=True, record=seen) is True
        assert seen == ["https://ollama.com/v1"]

    def test_non_ollama_local_server_is_not_probed(self):
        """The check is Ollama-specific, not "any local endpoint".

        A local vLLM/llama.cpp server on another port must keep falling
        through to the existing OpenRouter-only branch, so this fix cannot
        change behaviour for endpoints it knows nothing about.
        """
        seen: list = []
        assert _gate("http://localhost:8000/v1", probe_result=True, record=seen) is False
        assert seen == [], "a non-Ollama port must not trigger the Ollama probe"

    def test_openrouter_branch_unaffected(self):
        """Sanity: the gate still refuses unknown non-OpenRouter hosts."""
        assert _gate("https://api.example.com/v1", probe_result=True) is False


class TestSupportsReasoningReachesCustomProfile:
    """The resolved capability actually reaches the profile.

    ``CustomProfile.build_api_kwargs_extras`` defaults ``supports_reasoning``
    to False (fail closed). That default is only safe because every real call
    site passes the transport-resolved value explicitly — this pins it.
    """

    def test_transport_passes_supports_reasoning_to_profile(self):
        import inspect

        from agent.transports import chat_completions

        src = inspect.getsource(chat_completions)
        assert "supports_reasoning=params.get(" in src, (
            "the transport must forward the resolved capability to the profile; "
            "without it CustomProfile's fail-closed default would silently "
            "disable reasoning for every custom endpoint"
        )

    def test_non_thinking_model_gets_no_effort(self):
        """End-to-end on the profile: capability False → no effort emitted."""
        import model_tools  # noqa: F401  (triggers plugin discovery)
        import providers

        profile = providers.get_provider_profile("custom")
        eb, tl = profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "medium"},
            supports_reasoning=False,
            model="qwen2.5:7b",
        )
        assert "reasoning_effort" not in tl
        assert "think" not in eb

    def test_thinking_model_still_gets_effort(self):
        """End-to-end on the profile: capability True → effort emitted."""
        import model_tools  # noqa: F401
        import providers

        profile = providers.get_provider_profile("custom")
        _, tl = profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "high"},
            supports_reasoning=True,
            model="deepseek-r1",
        )
        assert tl == {"reasoning_effort": "high"}
