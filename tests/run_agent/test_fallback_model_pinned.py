"""Model-pinned fallback entries (require_same_model / match_model).

Signal technique: stub resolve_provider_client via guarded __import__ so it
records args then raises RuntimeError(sentinel). try_activate_fallback wraps
client construction in a broad `except Exception` that logs
"Failed to activate fallback <model>: <sentinel>" — a captured-log hit proves
the entry passed every gate. The boolean return is NOT usable (False both when
skipped and when construction raised).
"""
import logging
import sys

import pytest


CHAIN = [{
    "provider": "openrouter",
    "model": "stealth/ox-alpha",
    "match_model": ["x-preview-f-free"],
    "require_same_model": True,
}]

SENTINEL = "pinned-sentinel"


class _Agent:
    def __init__(self, model, provider="nous",
                 base_url="https://inference-api.nousresearch.com/v1"):
        import hermes_constants  # noqa: F401  (ensure repo on path)

        self.model = model
        self.provider = provider
        self.base_url = base_url
        self.quiet_mode = True
        self._fallback_chain = CHAIN
        self._fallback_index = 0
        self._fallback_activated = False
        self._unavailable_fallback_keys = set()
        self._rate_limited_until = 0
        self._rate_limit_backoff_count = 0
        self._primary_runtime = {"provider": provider}
        self._config_context_length = None

    @staticmethod
    def _is_azure_openai_url(url):
        return False

    @staticmethod
    def _is_direct_openai_url(url):
        return False

    def _provider_model_requires_responses_api(self, model, provider=None):
        return False

    def _anthropic_prompt_cache_policy(self, **kw):
        return (False, False)

    def _ensure_lmstudio_runtime_loaded(self):
        pass

    def _buffer_status(self, message):
        pass

    def _try_activate_fallback(self, reason=None):
        from agent.chat_completion_helpers import try_activate_fallback
        return try_activate_fallback(self, reason)


@pytest.fixture()
def captured_resolution(monkeypatch):
    import builtins

    recorded = {}
    real_import = builtins.__import__

    def fake_resolve(provider, model=None, **kw):
        recorded["provider"] = provider
        recorded["model"] = model
        raise RuntimeError(SENTINEL)

    def guarded(name, *a, **kw):
        mod = real_import(name, *a, **kw)
        if name == "agent.auxiliary_client":
            mod.resolve_provider_client = fake_resolve
        return mod

    class Cap(logging.Handler):
        def __init__(self):
            super().__init__()
            self.lines = []

        def emit(self, record):
            self.lines.append(record.getMessage())

    cap = Cap()
    lg = logging.getLogger("agent.chat_completion_helpers")
    lg.addHandler(cap)
    lg.setLevel(logging.DEBUG)
    builtins.__import__ = guarded
    try:
        yield recorded, cap.lines
    finally:
        builtins.__import__ = real_import
        lg.removeHandler(cap)


def _hit(lines):
    return [ln for ln in lines if SENTINEL in ln]


def test_pinned_entry_fires_for_primary_model(captured_resolution, monkeypatch):
    recorded, lines = captured_resolution
    monkeypatch.setattr(sys, "path", sys.path)
    agent = _Agent("stealth/ox-alpha")
    agent._try_activate_fallback()
    assert _hit(lines), "same-model primary should reach client construction"
    assert recorded["provider"] == "openrouter"
    assert recorded["model"] == "stealth/ox-alpha"


def test_pinned_entry_fires_for_match_model(captured_resolution):
    recorded, lines = captured_resolution
    agent = _Agent(
        "x-preview-f-free",
        provider="opencode-free",
        base_url="https://opencode.ai/zen/v1",
    )
    agent._try_activate_fallback()
    assert _hit(lines), "match_model name should be accepted"
    assert recorded["provider"] == "openrouter"


def test_pinned_entry_skips_other_models(captured_resolution):
    _, lines = captured_resolution
    agent = _Agent(
        "claude-opus-5",
        provider="custom",
        base_url="https://agentrouter.example/v1",
    )
    activated = agent._try_activate_fallback()
    assert activated is False
    assert not _hit(lines), "different-model primary must never reach resolution"
    assert agent.provider == "custom", "provider must be untouched"
    assert agent._fallback_index >= len(agent._fallback_chain)


def test_unpinned_chain_unaffected(captured_resolution):
    """Regression: chains without require_same_model behave as before."""
    recorded, lines = captured_resolution
    agent = _Agent("claude-opus-5", provider="custom",
                   base_url="https://x.example/v1")
    agent._fallback_chain = [{"provider": "openrouter", "model": "some/other"}]
    agent._try_activate_fallback()
    assert _hit(lines), "unpinned entry must still fire for any model"
    assert recorded["model"] == "some/other"
