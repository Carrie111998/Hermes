"""Tests for priority provider rotation state and cooldown helpers."""

from __future__ import annotations

import time

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


class TestProviderRotationState:
    def test_marks_provider_exhausted_and_skips_until_cooldown_expires(self, tmp_path):
        """A capacity failure should persist cooldown state for matching provider/model."""
        from agent.provider_rotation import ProviderRotationState

        token = set_hermes_home_override(tmp_path)
        try:
            state = ProviderRotationState.load()
            state.mark_unavailable(
                provider="openai-codex",
                model="gpt-5.3-codex",
                reason="rate_limit",
                cooldown_seconds=60,
                now=1000.0,
            )

            reloaded = ProviderRotationState.load()
            assert reloaded.is_unavailable("openai-codex", "gpt-5.3-codex", now=1010.0)
            assert not reloaded.is_unavailable("openai-codex", "gpt-5.3-codex", now=1061.0)
        finally:
            reset_hermes_home_override(token)

    def test_filters_unavailable_entries_but_keeps_available_order(self, tmp_path):
        """Rotation should preserve user priority while removing cooled-down entries."""
        from agent.provider_rotation import ProviderRotationState, filter_available_entries

        token = set_hermes_home_override(tmp_path)
        try:
            state = ProviderRotationState.load()
            state.mark_unavailable(
                provider="openai-codex",
                model="gpt-5.3-codex",
                reason="billing",
                cooldown_seconds=3600,
                now=1000.0,
            )
            chain = [
                {"provider": "openai-codex", "model": "gpt-5.3-codex"},
                {"provider": "anthropic", "model": "claude-sonnet-4-6"},
                {"provider": "google-gemini-cli", "model": "gemini-3-pro-preview"},
            ]

            assert filter_available_entries(chain, now=1200.0) == chain[1:]
        finally:
            reset_hermes_home_override(token)

    def test_reset_removes_provider_state(self, tmp_path):
        """Manual reset should make a provider immediately eligible again."""
        from agent.provider_rotation import ProviderRotationState

        token = set_hermes_home_override(tmp_path)
        try:
            state = ProviderRotationState.load()
            state.mark_unavailable(
                provider="anthropic",
                model="claude-sonnet-4-6",
                reason="rate_limit",
                cooldown_seconds=3600,
                now=time.time(),
            )
            assert state.reset("anthropic", "claude-sonnet-4-6") == 1
            assert not ProviderRotationState.load().is_unavailable(
                "anthropic", "claude-sonnet-4-6", now=time.time()
            )
        finally:
            reset_hermes_home_override(token)

    def test_same_provider_model_different_base_urls_do_not_share_cooldown(self, tmp_path):
        """Endpoint identity is part of rotation state for custom providers."""
        from agent.provider_rotation import ProviderRotationState

        token = set_hermes_home_override(tmp_path)
        try:
            state = ProviderRotationState.load()
            state.mark_unavailable(
                provider="custom",
                model="claude-opus-4-7",
                base_url="https://proxy-one.example/v1",
                reason="rate_limit",
                cooldown_seconds=3600,
                now=1000.0,
            )
            reloaded = ProviderRotationState.load()
            assert reloaded.is_unavailable(
                "custom",
                "claude-opus-4-7",
                base_url="https://proxy-one.example/v1",
                now=1200.0,
            )
            assert not reloaded.is_unavailable(
                "custom",
                "claude-opus-4-7",
                base_url="https://proxy-two.example/v1",
                now=1200.0,
            )
        finally:
            reset_hermes_home_override(token)

    def test_stale_instance_merge_keeps_concurrent_records(self, tmp_path):
        """Two sessions loaded before either write must not clobber each other."""
        from agent.provider_rotation import ProviderRotationState

        token = set_hermes_home_override(tmp_path)
        try:
            state_a = ProviderRotationState.load()
            state_b = ProviderRotationState.load()
            state_a.mark_unavailable(
                provider="openai-codex",
                model="gpt-5.3-codex",
                reason="rate_limit",
                cooldown_seconds=3600,
                now=1000.0,
            )
            state_b.mark_unavailable(
                provider="anthropic",
                model="claude-sonnet-4-6",
                reason="billing",
                cooldown_seconds=3600,
                now=1001.0,
            )

            reloaded = ProviderRotationState.load()
            assert reloaded.is_unavailable("openai-codex", "gpt-5.3-codex", now=1200.0)
            assert reloaded.is_unavailable("anthropic", "claude-sonnet-4-6", now=1200.0)
        finally:
            reset_hermes_home_override(token)
