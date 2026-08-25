"""Tests for ordered provider fallback chain (salvage of PR #1761).

Extends the single-fallback tests in test_fallback_model.py to cover
the new list-based ``fallback_providers`` config format and chain
advancement through multiple providers.
"""

from unittest.mock import MagicMock, patch

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from run_agent import AIAgent, _pool_may_recover_from_rate_limit


def _make_agent(fallback_model=None):
    """Create a minimal AIAgent with optional fallback config."""
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_model,
        )
        agent.client = MagicMock()
        return agent


def _mock_client(base_url="https://openrouter.ai/api/v1", api_key="fb-key"):
    mock = MagicMock()
    mock.base_url = base_url
    mock.api_key = api_key
    return mock


# ── Chain initialisation ──────────────────────────────────────────────────


class TestFallbackChainInit:
    def test_no_fallback(self):
        agent = _make_agent(fallback_model=None)
        assert agent._fallback_chain == []
        assert agent._fallback_index == 0
        assert agent._fallback_model is None

    def test_single_dict_backwards_compat(self):
        fb = {"provider": "openai", "model": "gpt-4o"}
        agent = _make_agent(fallback_model=fb)
        assert agent._fallback_chain == [fb]
        assert agent._fallback_model == fb

    def test_list_of_providers(self):
        fbs = [
            {"provider": "openai", "model": "gpt-4o"},
            {"provider": "zai", "model": "glm-4.7"},
        ]
        agent = _make_agent(fallback_model=fbs)
        assert len(agent._fallback_chain) == 2
        assert agent._fallback_model == fbs[0]

    def test_invalid_entries_filtered(self):
        fbs = [
            {"provider": "openai", "model": "gpt-4o"},
            {"provider": "", "model": "glm-4.7"},
            {"provider": "zai"},
            "not-a-dict",
        ]
        agent = _make_agent(fallback_model=fbs)
        assert len(agent._fallback_chain) == 1
        assert agent._fallback_chain[0]["provider"] == "openai"

    def test_empty_list(self):
        agent = _make_agent(fallback_model=[])
        assert agent._fallback_chain == []
        assert agent._fallback_model is None

    def test_invalid_dict_no_provider(self):
        agent = _make_agent(fallback_model={"model": "gpt-4o"})
        assert agent._fallback_chain == []


# ── Chain advancement ─────────────────────────────────────────────────────


class TestFallbackChainAdvancement:
    def test_exhausted_returns_false(self):
        agent = _make_agent(fallback_model=None)
        assert agent._try_activate_fallback() is False

    def test_advances_index(self):
        fbs = [
            {"provider": "openai", "model": "gpt-4o"},
            {"provider": "zai", "model": "glm-4.7"},
        ]
        agent = _make_agent(fallback_model=fbs)
        with patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(_mock_client(), "gpt-4o"),
        ):
            assert agent._try_activate_fallback() is True
            assert agent._fallback_index == 1
            assert agent.model == "gpt-4o"
            assert agent._fallback_activated is True

    def test_second_fallback_works(self):
        fbs = [
            {"provider": "openai", "model": "gpt-4o"},
            {"provider": "zai", "model": "glm-4.7"},
        ]
        agent = _make_agent(fallback_model=fbs)
        with patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(_mock_client(), "resolved"),
        ):
            assert agent._try_activate_fallback() is True
            assert agent.model == "gpt-4o"
            assert agent._try_activate_fallback() is True
            assert agent.model == "glm-4.7"
            assert agent._fallback_index == 2

    def test_all_exhausted_returns_false(self):
        fbs = [{"provider": "openai", "model": "gpt-4o"}]
        agent = _make_agent(fallback_model=fbs)
        with patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(_mock_client(), "gpt-4o"),
        ):
            assert agent._try_activate_fallback() is True
            assert agent._try_activate_fallback() is False

    def test_skips_unconfigured_provider_to_next(self):
        """If resolve_provider_client returns None, skip to next in chain."""
        fbs = [
            {"provider": "broken", "model": "nope"},
            {"provider": "openai", "model": "gpt-4o"},
        ]
        agent = _make_agent(fallback_model=fbs)
        with patch("agent.auxiliary_client.resolve_provider_client") as mock_rpc:
            mock_rpc.side_effect = [
                (None, None),
                (_mock_client(), "gpt-4o"),
            ]
            assert agent._try_activate_fallback() is True
            assert agent.model == "gpt-4o"
            assert agent._fallback_index == 2

    def test_skips_provider_that_raises_to_next(self):
        """If resolve_provider_client raises, skip to next in chain."""
        fbs = [
            {"provider": "broken", "model": "nope"},
            {"provider": "openai", "model": "gpt-4o"},
        ]
        agent = _make_agent(fallback_model=fbs)
        with patch("agent.auxiliary_client.resolve_provider_client") as mock_rpc:
            mock_rpc.side_effect = [
                RuntimeError("auth failed"),
                (_mock_client(), "gpt-4o"),
            ]
            assert agent._try_activate_fallback() is True
            assert agent.model == "gpt-4o"

    def test_resolves_key_env_for_fallback_provider(self):
        fbs = [
            {
                "provider": "custom",
                "model": "fallback-model",
                "base_url": "https://fallback.example/v1",
                "key_env": "MY_FALLBACK_KEY",
            }
        ]
        agent = _make_agent(fallback_model=fbs)
        with (
            patch.dict("os.environ", {"MY_FALLBACK_KEY": "env-secret"}, clear=False),
            patch(
                "agent.auxiliary_client.resolve_provider_client",
                return_value=(
                    _mock_client(
                        base_url="https://fallback.example/v1",
                        api_key="env-secret",
                    ),
                    "fallback-model",
                ),
            ) as mock_rpc,
        ):
            assert agent._try_activate_fallback() is True
            assert mock_rpc.call_args.kwargs["explicit_api_key"] == "env-secret"

    def test_provider_rotation_skips_cooled_down_fallback_entry(self, tmp_path):
        """Rotation mode should preserve priority while skipping exhausted entries."""
        fbs = [
            {"provider": "anthropic", "model": "claude-sonnet-4-6"},
            {"provider": "google-gemini-cli", "model": "gemini-3-pro-preview"},
        ]
        token = set_hermes_home_override(tmp_path)
        try:
            from agent.provider_rotation import ProviderRotationState

            ProviderRotationState.load().mark_unavailable(
                provider="anthropic",
                model="claude-sonnet-4-6",
                reason="rate_limit",
                cooldown_seconds=3600,
                now=1000.0,
            )
            agent = _make_agent(fallback_model=fbs)
            called = []

            def _resolve(provider, model=None, raw_codex=False, **kwargs):
                called.append((provider, model))
                return _mock_client(), model

            with (
                patch(
                    "hermes_cli.config.load_config",
                    return_value={"provider_rotation": {"enabled": True}},
                ),
                patch("time.time", return_value=1200.0),
                patch(
                    "agent.auxiliary_client.resolve_provider_client",
                    side_effect=_resolve,
                ),
                patch(
                    "hermes_cli.model_normalize.normalize_model_for_provider",
                    side_effect=lambda m, p: m,
                ),
            ):
                assert agent._try_activate_fallback() is True

            assert called == [("google-gemini-cli", "gemini-3-pro-preview")]
            assert agent.model == "gemini-3-pro-preview"
        finally:
            reset_hermes_home_override(token)

    def test_provider_rotation_marks_failed_provider_unavailable(self, tmp_path):
        """Durable quota evidence should persist cooldown state across sessions."""
        from agent.error_classifier import FailoverReason
        from agent.provider_rotation import ProviderRotationState

        token = set_hermes_home_override(tmp_path)
        try:
            fbs = [{"provider": "anthropic", "model": "claude-sonnet-4-6"}]
            agent = _make_agent(fallback_model=fbs)
            agent.provider = "openai-codex"
            agent.model = "gpt-5.3-codex"
            rate_limit_headers = {
                "x-ratelimit-limit-requests-1h": "800",
                "x-ratelimit-remaining-requests-1h": "0",
                "x-ratelimit-reset-requests-1h": "7200",
            }

            with (
                patch(
                    "hermes_cli.config.load_config",
                    return_value={
                        "provider_rotation": {
                            "enabled": True,
                            "cooldown_seconds_by_reason": {"rate_limit": 7200},
                        }
                    },
                ),
                patch("time.time", return_value=2000.0),
                patch("time.monotonic", return_value=2000.0),
                patch(
                    "agent.auxiliary_client.resolve_provider_client",
                    return_value=(_mock_client(), "claude-sonnet-4-6"),
                ),
            ):
                assert (
                    agent._try_activate_fallback(
                        FailoverReason.rate_limit,
                        rate_limit_headers=rate_limit_headers,
                    )
                    is True
                )

            state = ProviderRotationState.load()
            assert state.is_unavailable(
                "openai-codex",
                "gpt-5.3-codex",
                base_url="https://openrouter.ai/api/v1",
                now=9000.0,
            )
            assert not state.is_unavailable(
                "openai-codex",
                "gpt-5.3-codex",
                base_url="https://openrouter.ai/api/v1",
                now=9201.0,
            )
        finally:
            reset_hermes_home_override(token)

    def test_provider_rotation_skips_persist_for_transient_rate_limit(self, tmp_path):
        """Healthy buckets on a 429 should not write cross-session cooldown state."""
        from agent.error_classifier import FailoverReason
        from agent.provider_rotation import ProviderRotationState

        token = set_hermes_home_override(tmp_path)
        try:
            fbs = [{"provider": "anthropic", "model": "claude-sonnet-4-6"}]
            agent = _make_agent(fallback_model=fbs)
            agent.provider = "openai-codex"
            agent.model = "gpt-5.3-codex"
            rate_limit_headers = {
                "x-ratelimit-limit-requests": "200",
                "x-ratelimit-remaining-requests": "198",
                "x-ratelimit-reset-requests": "20",
                "x-ratelimit-limit-requests-1h": "800",
                "x-ratelimit-remaining-requests-1h": "790",
                "x-ratelimit-reset-requests-1h": "1800",
            }

            with (
                patch(
                    "hermes_cli.config.load_config",
                    return_value={"provider_rotation": {"enabled": True}},
                ),
                patch("time.time", return_value=2000.0),
                patch("time.monotonic", return_value=2000.0),
                patch(
                    "agent.auxiliary_client.resolve_provider_client",
                    return_value=(_mock_client(), "claude-sonnet-4-6"),
                ),
            ):
                assert (
                    agent._try_activate_fallback(
                        FailoverReason.rate_limit,
                        rate_limit_headers=rate_limit_headers,
                    )
                    is True
                )

            state = ProviderRotationState.load()
            assert not state.is_unavailable(
                "openai-codex",
                "gpt-5.3-codex",
                now=2001.0,
            )
        finally:
            reset_hermes_home_override(token)

    def test_provider_rotation_uses_fallback_at_turn_start_when_primary_is_cooling_down(self, tmp_path):
        """A new prompt should skip primary while persisted cooldown is active."""
        from agent.provider_rotation import ProviderRotationState

        token = set_hermes_home_override(tmp_path)
        try:
            fbs = [{"provider": "anthropic", "model": "claude-sonnet-4-6"}]
            agent = _make_agent(fallback_model=fbs)
            agent.provider = "openai-codex"
            agent.model = "gpt-5.3-codex"
            agent._primary_runtime["provider"] = "openai-codex"
            agent._primary_runtime["model"] = "gpt-5.3-codex"
            agent._primary_runtime["base_url"] = "https://openrouter.ai/api/v1"
            ProviderRotationState.load().mark_unavailable(
                provider="openai-codex",
                model="gpt-5.3-codex",
                base_url="https://openrouter.ai/api/v1",
                reason="rate_limit",
                cooldown_seconds=3600,
                now=1000.0,
            )

            with (
                patch(
                    "hermes_cli.config.load_config",
                    return_value={"provider_rotation": {"enabled": True}},
                ),
                patch("time.time", return_value=1200.0),
                patch(
                    "agent.auxiliary_client.resolve_provider_client",
                    return_value=(_mock_client(), "claude-sonnet-4-6"),
                ),
            ):
                assert agent._restore_primary_runtime() is True

            assert agent.provider == "anthropic"
            assert agent.model == "claude-sonnet-4-6"
            assert agent._fallback_activated is True
        finally:
            reset_hermes_home_override(token)

    def test_restore_path_respects_persisted_primary_cooldown_after_60s_gate(self, tmp_path):
        """Expired transient gate must not restore a primary still on durable cooldown."""
        from agent.provider_rotation import ProviderRotationState

        token = set_hermes_home_override(tmp_path)
        try:
            agent = _make_agent(fallback_model=[{"provider": "anthropic", "model": "claude-sonnet-4-6"}])
            agent.provider = "anthropic"
            agent.model = "claude-sonnet-4-6"
            agent.base_url = "https://api.anthropic.com/v1"
            agent._fallback_activated = True
            agent._rate_limited_until = 0
            agent._primary_runtime["provider"] = "openai-codex"
            agent._primary_runtime["model"] = "gpt-5.3-codex"
            agent._primary_runtime["base_url"] = "https://openrouter.ai/api/v1"
            ProviderRotationState.load().mark_unavailable(
                provider="openai-codex",
                model="gpt-5.3-codex",
                base_url="https://openrouter.ai/api/v1",
                reason="rate_limit",
                cooldown_seconds=3600,
                now=1000.0,
            )

            with (
                patch(
                    "hermes_cli.config.load_config",
                    return_value={"provider_rotation": {"enabled": True}},
                ),
                patch("time.time", return_value=1200.0),
            ):
                assert agent._restore_primary_runtime() is False

            assert agent.provider == "anthropic"
            assert agent.model == "claude-sonnet-4-6"
            assert agent._fallback_activated is True
        finally:
            reset_hermes_home_override(token)


# ── Pool-rotation vs fallback gating (#11314) ────────────────────────────


def _pool(n_entries: int, has_available: bool = True):
    """Make a minimal credential-pool stand-in for rotation-room checks."""
    pool = MagicMock()
    pool.entries.return_value = [MagicMock() for _ in range(n_entries)]
    pool.has_available.return_value = has_available
    return pool


class TestPoolRotationRoom:
    def test_none_pool_returns_false(self):
        assert _pool_may_recover_from_rate_limit(None) is False

    def test_single_credential_returns_false(self):
        """With one credential that just 429'd, rotation has nowhere to go.

        The pool may still report has_available() True once cooldown expires,
        but retrying against the same entry will hit the same daily-quota
        429 and burn the retry budget.  Must fall back.
        """
        assert _pool_may_recover_from_rate_limit(_pool(1)) is False

    def test_single_credential_in_cooldown_returns_false(self):
        assert _pool_may_recover_from_rate_limit(_pool(1, has_available=False)) is False

    def test_two_credentials_available_returns_true(self):
        """With >1 credentials and at least one available, rotate instead of fallback."""
        assert _pool_may_recover_from_rate_limit(_pool(2)) is True

    def test_multiple_credentials_all_in_cooldown_returns_false(self):
        """All credentials cooling down — fall back rather than wait."""
        assert _pool_may_recover_from_rate_limit(_pool(3, has_available=False)) is False

    def test_many_credentials_available_returns_true(self):
        assert _pool_may_recover_from_rate_limit(_pool(10)) is True


# ── Skip-self dedup (#22548) ───────────────────────────────────────────────


class TestFallbackChainDedup:
    """A fallback chain entry that resolves to the current provider/model
    (or the same custom-provider base_url) must be skipped, not retried.
    Otherwise a misconfigured chain or two custom_providers entries pointing
    at the same shim loop the same failure. See issue #22548."""

    def test_skips_entry_matching_current_provider_and_model(self):
        """Chain has [same-as-current, real-fallback]; activate must skip
        the first and use the second."""
        fbs = [
            {"provider": "openrouter", "model": "z-ai/glm-4.7"},
            {"provider": "zai", "model": "glm-4.7"},
        ]
        agent = _make_agent(fallback_model=fbs)
        agent.provider = "openrouter"
        agent.model = "z-ai/glm-4.7"
        agent.base_url = "https://openrouter.ai/api/v1"

        called = []

        def _resolve(provider, model=None, raw_codex=False, **kwargs):
            called.append((provider, model))
            return _mock_client(), model

        with patch("agent.auxiliary_client.resolve_provider_client", side_effect=_resolve):
            with patch(
                "hermes_cli.model_normalize.normalize_model_for_provider",
                side_effect=lambda m, p: m,
            ):
                ok = agent._try_activate_fallback()

        assert ok is True
        assert called == [("zai", "glm-4.7")], (
            f"expected fallback to skip same-state entry, got call order: {called}"
        )

    def test_skips_entry_matching_current_base_url_and_model(self):
        """Two custom_providers entries pointing at the same shim URL
        with the same model should dedup even if their provider names differ."""
        fbs = [
            {
                "provider": "claude-cli-alt",
                "model": "claude-opus-4.7",
                "base_url": "http://127.0.0.1:7891/v1",
            },
            {"provider": "openrouter", "model": "anthropic/claude-opus-4.7"},
        ]
        agent = _make_agent(fallback_model=fbs)
        agent.provider = "claude-cli"
        agent.model = "claude-opus-4.7"
        agent.base_url = "http://127.0.0.1:7891/v1"

        called = []

        def _resolve(provider, model=None, raw_codex=False, **kwargs):
            called.append((provider, model))
            return _mock_client(), model

        with patch("agent.auxiliary_client.resolve_provider_client", side_effect=_resolve):
            with patch(
                "hermes_cli.model_normalize.normalize_model_for_provider",
                side_effect=lambda m, p: m,
            ):
                ok = agent._try_activate_fallback()

        assert ok is True
        assert called == [("openrouter", "anthropic/claude-opus-4.7")], (
            f"expected base_url-aware dedup, got call order: {called}"
        )

    def test_returns_false_when_only_self_matching_entries(self):
        """A chain with only self-matching entries exhausts to False."""
        fbs = [
            {"provider": "openrouter", "model": "z-ai/glm-4.7"},
        ]
        agent = _make_agent(fallback_model=fbs)
        agent.provider = "openrouter"
        agent.model = "z-ai/glm-4.7"
        agent.base_url = "https://openrouter.ai/api/v1"

        with patch("agent.auxiliary_client.resolve_provider_client") as mock_resolve:
            ok = agent._try_activate_fallback()

        assert ok is False
        mock_resolve.assert_not_called()
