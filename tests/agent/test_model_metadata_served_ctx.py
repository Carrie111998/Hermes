"""Tests for _query_ollama_served_ctx and the /api/ps cap on /api/show values.

/api/show reports the GGUF training maximum and cannot see
OLLAMA_CONTEXT_LENGTH -- a server env var, absent from both model_info and
parameters. When a server is started with a smaller window than the weights
support, every /api/show-derived context length overstates the real limit, and
requests sized to it come back with finish_reason="length" on the first token.

/api/ps reports what each LOADED model is actually being served at, so it is
the authority whenever it has an answer.

All tests use synthetic inputs -- no filesystem or live server required.
"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clear_local_ctx_probe_cache():
    """Reset the in-process probe TTL cache around every test.

    _query_ollama_served_ctx memoizes per (model, server_url) for a short TTL.
    Cases below return different /api/ps bodies for the same pair, so a stale
    entry would leak across them.
    """
    import agent.model_metadata as _mm

    _mm._LOCAL_CTX_PROBE_CACHE.clear()
    yield
    _mm._LOCAL_CTX_PROBE_CACHE.clear()


def _resp(status_code, body):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body
    return resp


def _ps(*models):
    return {"models": [dict(m) for m in models]}


class TestQueryOllamaServedCtx:
    """_query_ollama_served_ctx against a mocked /api/ps."""

    def test_returns_served_context_for_loaded_model(self):
        from agent.model_metadata import _query_ollama_served_ctx

        client = MagicMock()
        client.get.return_value = _resp(
            200, _ps({"name": "gpt-oss:20b", "context_length": 32768})
        )
        with patch("httpx.Client") as C:
            C.return_value.__enter__.return_value = client
            assert (
                _query_ollama_served_ctx("gpt-oss:20b", "http://x:11434", {})
                == 32768
            )

    def test_returns_none_when_model_not_loaded(self):
        """The decisive fallback: an idle model has no knowable served ctx."""
        from agent.model_metadata import _query_ollama_served_ctx

        client = MagicMock()
        client.get.return_value = _resp(
            200, _ps({"name": "other:7b", "context_length": 8192})
        )
        with patch("httpx.Client") as C:
            C.return_value.__enter__.return_value = client
            assert (
                _query_ollama_served_ctx("gpt-oss:20b", "http://x:11434", {})
                is None
            )

    def test_matches_on_model_field_too(self):
        from agent.model_metadata import _query_ollama_served_ctx

        client = MagicMock()
        client.get.return_value = _resp(
            200, _ps({"model": "gpt-oss:20b", "context_length": 16384})
        )
        with patch("httpx.Client") as C:
            C.return_value.__enter__.return_value = client
            assert (
                _query_ollama_served_ctx("gpt-oss:20b", "http://x:11434", {})
                == 16384
            )

    def test_empty_and_error_responses_are_none(self):
        from agent.model_metadata import _query_ollama_served_ctx

        for body, status in (({}, 200), (_ps(), 200), ({}, 404)):
            import agent.model_metadata as _mm

            _mm._LOCAL_CTX_PROBE_CACHE.clear()
            client = MagicMock()
            client.get.return_value = _resp(status, body)
            with patch("httpx.Client") as C:
                C.return_value.__enter__.return_value = client
                assert (
                    _query_ollama_served_ctx("m", "http://x:11434", {}) is None
                )

    def test_nonsense_small_value_ignored(self):
        """Guards against a 0/absurd context_length clamping everything to junk."""
        from agent.model_metadata import _query_ollama_served_ctx

        client = MagicMock()
        client.get.return_value = _resp(
            200, _ps({"name": "m", "context_length": 8})
        )
        with patch("httpx.Client") as C:
            C.return_value.__enter__.return_value = client
            assert _query_ollama_served_ctx("m", "http://x:11434", {}) is None

    def test_connection_error_is_swallowed(self):
        from agent.model_metadata import _query_ollama_served_ctx

        with patch("httpx.Client", side_effect=OSError("refused")):
            assert _query_ollama_served_ctx("m", "http://x:11434", {}) is None


class TestCapCtxByServed:
    """_cap_ctx_by_served only ever lowers, and only on a real signal."""

    def _patched(self, served):
        return patch(
            "agent.model_metadata._query_ollama_served_ctx", return_value=served
        )

    def test_caps_advertised_max_down_to_served(self):
        """The reported case: advertises 131072, actually serving 32768."""
        from agent.model_metadata import _cap_ctx_by_served

        with self._patched(32768):
            assert _cap_ctx_by_served(131072, "m", "http://x", {}) == 32768

    def test_does_not_raise_when_served_is_larger(self):
        """GGUF max is the hard ceiling -- never exceed it on /api/ps's word."""
        from agent.model_metadata import _cap_ctx_by_served

        with self._patched(131072):
            assert _cap_ctx_by_served(32768, "m", "http://x", {}) == 32768

    def test_unloaded_model_keeps_advertised_value(self):
        from agent.model_metadata import _cap_ctx_by_served

        with self._patched(None):
            assert _cap_ctx_by_served(131072, "m", "http://x", {}) == 131072

    def test_none_and_zero_pass_through(self):
        from agent.model_metadata import _cap_ctx_by_served

        with self._patched(32768):
            assert _cap_ctx_by_served(None, "m", "http://x", {}) is None
            assert _cap_ctx_by_served(0, "m", "http://x", {}) == 0


def _fake_agent(**overrides):
    """Minimal agent stand-in for the runtime-reconcile helper."""
    from types import SimpleNamespace

    defaults = dict(
        base_url="http://127.0.0.1:11434/v1",
        model="gpt-oss:20b",
        api_key="",
        provider="ollama",
        api_mode="chat_completions",
        _ollama_num_ctx=262144,
        context_compressor=MagicMock(context_length=262144),
        tools=[{"type": "function"}],
        session_id="test-session",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestReconcileOllamaRuntimeCtx:
    """_reconcile_ollama_runtime_ctx: the finish_reason="length" repair path."""

    def _served(self, value):
        return patch(
            "agent.model_metadata._query_ollama_served_ctx", return_value=value
        )

    def test_lowers_value_and_reclamps_compressor(self):
        from agent.conversation_loop import _reconcile_ollama_runtime_ctx

        agent = _fake_agent()
        with self._served(198656):
            assert _reconcile_ollama_runtime_ctx(agent) == 198656
        assert agent._ollama_num_ctx == 198656
        agent.context_compressor.update_model.assert_called_once()
        kwargs = agent.context_compressor.update_model.call_args.kwargs
        assert kwargs["context_length"] == 198656
        assert kwargs["model"] == "gpt-oss:20b"

    def test_cold_probe_is_a_noop(self):
        """/api/ps has no answer -> keep the believed value, touch nothing."""
        from agent.conversation_loop import _reconcile_ollama_runtime_ctx

        agent = _fake_agent()
        with self._served(None):
            assert _reconcile_ollama_runtime_ctx(agent) is None
        assert agent._ollama_num_ctx == 262144
        agent.context_compressor.update_model.assert_not_called()

    def test_never_raises_the_believed_value(self):
        from agent.conversation_loop import _reconcile_ollama_runtime_ctx

        agent = _fake_agent(_ollama_num_ctx=32768)
        with self._served(131072):
            assert _reconcile_ollama_runtime_ctx(agent) is None
        assert agent._ollama_num_ctx == 32768

    def test_non_ollama_agent_never_probes(self):
        """No _ollama_num_ctx (non-Ollama provider) -> no network call at all."""
        from agent.conversation_loop import _reconcile_ollama_runtime_ctx

        agent = _fake_agent(_ollama_num_ctx=None)
        with self._served(198656) as probe:
            assert _reconcile_ollama_runtime_ctx(agent) is None
            probe.assert_not_called()

    def test_compressor_clamp_failure_keeps_corrected_value(self):
        """A compressor error must not undo the corrected runtime number."""
        from agent.conversation_loop import _reconcile_ollama_runtime_ctx

        agent = _fake_agent()
        agent.context_compressor.update_model.side_effect = RuntimeError("boom")
        with self._served(198656):
            assert _reconcile_ollama_runtime_ctx(agent) == 198656
        assert agent._ollama_num_ctx == 198656


class TestColdToLoadedSequence:
    """End-to-end mocked cold->loaded lifecycle across the real callers.

    Phase 1 (cold, the agent_init resolution): /api/show advertises the GGUF
    maximum and /api/ps lists nothing, so query_ollama_num_ctx() retains the
    maximum -- the gap called out in review of #76332.

    Phase 2 (loaded, the finish_reason="length" path): the model just
    responded, /api/ps now has an answer, and _reconcile_ollama_runtime_ctx
    lowers agent._ollama_num_ctx and re-clamps the compressor.

    Phase 3 (sub-minimum served window): after reconciliation the existing
    _ollama_context_limit_error guard fires with actionable text -- it can
    never fire while the believed value is the GGUF maximum.
    """

    GGUF_MAX = 262144
    SERVED = 198656

    def _client(self, ps_body, show_body=None):
        client = MagicMock()
        client.get.return_value = _resp(200, ps_body)
        if show_body is not None:
            client.post.return_value = _resp(200, show_body)
        return client

    def test_cold_init_then_loaded_reconcile(self):
        import agent.model_metadata as _mm
        from agent.conversation_loop import _reconcile_ollama_runtime_ctx
        from agent.model_metadata import query_ollama_num_ctx

        show_body = {
            "model_info": {"llama.context_length": self.GGUF_MAX},
            "parameters": "",
        }

        # Phase 1 -- cold: nothing loaded, init retains the GGUF maximum.
        cold = self._client(_ps(), show_body)
        with (
            patch("agent.model_metadata.detect_local_server_type", return_value="ollama"),
            patch("agent.model_metadata._local_probe_disk_get", return_value=None),
            patch("agent.model_metadata._local_probe_disk_put"),
            patch("httpx.Client") as C,
        ):
            C.return_value.__enter__.return_value = cold
            believed = query_ollama_num_ctx(
                "gpt-oss:20b", "http://127.0.0.1:11434/v1"
            )
        assert believed == self.GGUF_MAX

        # Phase 2 -- loaded: the length path re-reads /api/ps and corrects.
        _mm._LOCAL_CTX_PROBE_CACHE.clear()
        agent = _fake_agent(_ollama_num_ctx=believed)
        agent.context_compressor = MagicMock(context_length=believed)
        loaded = self._client(
            _ps({"name": "gpt-oss:20b", "context_length": self.SERVED})
        )
        with patch("httpx.Client") as C:
            C.return_value.__enter__.return_value = loaded
            assert _reconcile_ollama_runtime_ctx(agent) == self.SERVED
        assert agent._ollama_num_ctx == self.SERVED
        kwargs = agent.context_compressor.update_model.call_args.kwargs
        assert kwargs["context_length"] == self.SERVED

        # A healthy served window must NOT trip the minimum-context guard.
        from agent.conversation_loop import _ollama_context_limit_error

        assert _ollama_context_limit_error(agent, 30000) is None

    def test_loaded_subminimum_window_fires_runtime_guard(self):
        """Phase 3: a tiny served window surfaces the actionable error."""
        import agent.model_metadata as _mm
        from agent.conversation_loop import (
            _ollama_context_limit_error,
            _reconcile_ollama_runtime_ctx,
        )

        _mm._LOCAL_CTX_PROBE_CACHE.clear()
        agent = _fake_agent()
        loaded = self._client(
            _ps({"name": "gpt-oss:20b", "context_length": 32768})
        )
        with patch("httpx.Client") as C:
            C.return_value.__enter__.return_value = loaded
            assert _reconcile_ollama_runtime_ctx(agent) == 32768

        error = _ollama_context_limit_error(agent, 30000)
        assert error is not None
        assert "32,768" in error
