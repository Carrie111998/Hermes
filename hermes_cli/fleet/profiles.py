"""Ordered, fail-closed lane profiles.

Profiles describe allowed targets; current qualification evidence must still
prove every model, effort, auth, billing, and execution control before use.
"""

from __future__ import annotations

from .types import AdapterKind, LaneProfile


def ordered_profiles() -> tuple[LaneProfile, ...]:
    return (
        LaneProfile(
            lane_id="chatgpt_codex",
            order=0,
            adapter_kind=AdapterKind.NATIVE_PROVIDER,
            provider_id="openai-codex",
            ordered_models=("gpt-5.4", "gpt-5.3-codex"),
            supported_efforts=("low", "medium", "high", "xhigh"),
            capabilities=frozenset({"workspace_read", "workspace_write", "shell"}),
            allowed_auth_kinds=frozenset({"oauth_subscription"}),
            fast_mode_supported=False,
            fast_off_verifiable=True,
        ),
        LaneProfile(
            lane_id="claude_code",
            order=1,
            adapter_kind=AdapterKind.EXTERNAL_CLI,
            provider_id="claude-code-subscription",
            ordered_models=("claude-opus-4-8", "claude-sonnet-4-6"),
            supported_efforts=("low", "medium", "high", "max"),
            capabilities=frozenset({"workspace_read", "workspace_write", "shell"}),
            allowed_auth_kinds=frozenset({"cli_subscription"}),
            executable="claude",
            fast_mode_supported=True,
            # No default is assumed: qualification must establish an explicit
            # fast-off control before a future profile revision can enable it.
            fast_off_verifiable=False,
        ),
        LaneProfile(
            lane_id="grok",
            order=2,
            adapter_kind=AdapterKind.NATIVE_PROVIDER,
            provider_id="xai-oauth",
            ordered_models=("grok-4.3",),
            supported_efforts=("low", "high"),
            capabilities=frozenset({"workspace_read", "workspace_write", "shell"}),
            allowed_auth_kinds=frozenset({"oauth_subscription"}),
            fast_mode_supported=False,
            fast_off_verifiable=True,
        ),
        LaneProfile(
            lane_id="antigravity",
            order=3,
            adapter_kind=AdapterKind.EXTERNAL_CLI,
            provider_id="antigravity-subscription",
            ordered_models=(),
            supported_efforts=(),
            capabilities=frozenset(),
            allowed_auth_kinds=frozenset({"cli_subscription"}),
            implemented=False,
            fast_off_verifiable=False,
        ),
        LaneProfile(
            lane_id="kimi",
            order=4,
            adapter_kind=AdapterKind.EXTERNAL_CLI,
            provider_id="kimi-subscription",
            ordered_models=(),
            supported_efforts=(),
            capabilities=frozenset(),
            allowed_auth_kinds=frozenset({"cli_subscription"}),
            implemented=False,
            fast_off_verifiable=False,
        ),
    )


def profile_map() -> dict[str, LaneProfile]:
    return {profile.lane_id: profile for profile in ordered_profiles()}
