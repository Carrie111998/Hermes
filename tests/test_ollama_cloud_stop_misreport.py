"""Regression tests for #60928: Ollama Cloud (ollama.com) stop→truncated false positive.

Ollama Cloud is a hosted OpenAI-compatible API that reports finish_reason
correctly. The local-Ollama workaround in ``_is_ollama_glm_backend`` must not
fire for ``ollama.com`` (only for genuinely local Ollama: port 11434,
``ollama.`` hostnames, or ``provider: ollama``).
"""

import pytest

from run_agent import AIAgent


def _tool_defs(*names):
    return [
        {"type": "function", "function": {"name": n, "description": n, "parameters": {}}}
        for n in names
    ]


class _FakeOpenAI:
    def __init__(self, **kw):
        self.api_key = kw.get("api_key", "test")
        self.base_url = kw.get("base_url", "http://test")

    def close(self):
        pass


def _make_agent(monkeypatch, *, base_url, provider, model):
    monkeypatch.setattr("run_agent.get_tool_definitions", lambda **kw: _tool_defs("web_search"))
    monkeypatch.setattr("run_agent.check_toolset_requirements", lambda: {})
    monkeypatch.setattr("run_agent.OpenAI", _FakeOpenAI)
    return AIAgent(
        api_key="test-key",
        base_url=base_url,
        provider=provider,
        model=model,
        api_mode="chat_completions",
        max_iterations=4,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )


class TestOllamaCloudNotLocal:
    """ollama.com must not trip the local-Ollama GLM workaround (#60928)."""

    def test_ollama_cloud_host_excluded(self, monkeypatch):
        agent = _make_agent(
            monkeypatch,
            base_url="https://ollama.com/v1",
            provider="ollama-cloud",
            model="glm-5.2",
        )
        assert agent._is_ollama_glm_backend() is False

    def test_ollama_cloud_subdomain_excluded(self, monkeypatch):
        # ponytail: catches the latent variant where the host matches but
        # isn't the public cloud domain.
        agent = _make_agent(
            monkeypatch,
            base_url="https://api.ollama.com/v1",
            provider="ollama-cloud",
            model="glm-5.2",
        )
        assert agent._is_ollama_glm_backend() is False

    def test_local_ollama_port_still_detected(self, monkeypatch):
        agent = _make_agent(
            monkeypatch,
            base_url="http://localhost:11434/v1",
            provider="ollama",
            model="glm-5.2",
        )
        assert agent._is_ollama_glm_backend() is True

    def test_local_ollama_hostname_still_detected(self, monkeypatch):
        agent = _make_agent(
            monkeypatch,
            base_url="http://ollama.local:11434/v1",
            provider="ollama",
            model="glm-5.2",
        )
        assert agent._is_ollama_glm_backend() is True

    def test_non_glm_model_not_flagged(self, monkeypatch):
        # The workaround only applies to GLM-family models + zai provider.
        agent = _make_agent(
            monkeypatch,
            base_url="http://localhost:11434/v1",
            provider="ollama",
            model="llama3.1",
        )
        assert agent._is_ollama_glm_backend() is False


class TestTruncatedJoinKeepsMediaDelimiter:
    """Bug 2 (#60928): a part-boundary MEDIA: tag must stay delimiter-separated.

    The continuation join now uses ``"\\n".join`` instead of ``"".join`` so the
    tag is followed by a delimiter and MEDIA_TAG_CLEANUP_RE still strips it.
    """

    def test_joined_media_tag_is_stripped(self):
        from gateway.platforms.base import _strip_media_tag_directives

        part1 = "Here is your file: MEDIA:/tmp/audio/tts_20260708_052306.ogg"
        part2 = "It's a warm and humid day."
        # ponytail: mirrors the fixed join in conversation_loop.py.
        assembled = "\n".join([part1, part2])

        cleaned = _strip_media_tag_directives(assembled)
        assert "MEDIA:/tmp/audio/tts_20260708_052306.ogg" not in cleaned
        assert cleaned.strip().startswith("Here is your file:")

    def test_old_glued_join_leaves_raw_tag(self):
        # Documents the bug class: the pre-fix "".join glues the tag to the
        # next word so the cleanup regex's lookahead can't match.
        from gateway.platforms.base import _strip_media_tag_directives

        part1 = "MEDIA:/tmp/audio/tts_20260708_052306.ogg"
        part2 = "It's a warm day."
        glued = "".join([part1, part2])  # pre-fix behavior

        cleaned = _strip_media_tag_directives(glued)
        assert "MEDIA:/tmp/audio/tts_20260708_052306.ogg" in cleaned
