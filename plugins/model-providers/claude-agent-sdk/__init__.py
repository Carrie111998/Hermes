"""Claude via the official claude-agent-sdk — subscription OAuth (#25267).

Unlike the ``anthropic`` provider (raw Messages API: API-key pay-per-token,
or OAuth that bills overage credits), this provider hands the whole turn to
Anthropic's ``claude-agent-sdk``, which authenticates with the **Claude
subscription** through Claude-managed CLI login storage. Hermes resolves NO
credentials for it: the SDK subprocess self-authenticates, while environment
credential and metered backend routes are rejected before startup.

Runtime: ``api_mode="claude_agent_sdk"`` — an agent-loop runtime dispatched
by an early return in run_conversation(), exactly like ``codex_app_server``.
"""

from providers import register_provider
from providers.base import ProviderProfile

claude_agent_sdk = ProviderProfile(
    name="claude-agent-sdk",
    # NB: "claude"/"claude-code"/"claude-oauth" are already claimed by the
    # `anthropic` profile — keep alias namespaces disjoint.
    aliases=("claude-sdk", "claude-code-sdk", "claude_agent_sdk"),
    display_name="Claude (Agent SDK / subscription)",
    description=(
        "Claude Code's agent loop via the official Agent SDK, billed to the "
        "Claude subscription (never a metered API key)."
    ),
    api_mode="claude_agent_sdk",
    env_vars=(),
    base_url="",
    auth_type="oauth_external",
    default_aux_model="claude-haiku-4-5-20251001",
)
register_provider(claude_agent_sdk)
