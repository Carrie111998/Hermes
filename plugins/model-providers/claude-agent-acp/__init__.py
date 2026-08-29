"""Claude Agent (Claude Code) ACP provider profile.

claude-agent-acp drives Claude Code through the ACP adapter
`@agentclientprotocol/claude-agent-acp` (Claude Agent SDK) as an external
subprocess over stdio JSON-RPC — NOT the standard REST transport. Unlike
copilot-acp it keeps a persistent, stateful ACP session across turns
(see agent/acp_subprocess_client.py). The profile captures auth + endpoint
metadata for the registry.
"""

from providers import register_provider
from providers.base import ProviderProfile


class ClaudeAgentACPProfile(ProviderProfile):
    """Claude Code via ACP — external process, no REST models endpoint."""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Model listing is handled by the ACP subprocess / Claude account."""
        return None


claude_agent_acp = ClaudeAgentACPProfile(
    name="claude-agent-acp",
    aliases=("claude-acp", "claude-agent", "claude-code-acp"),
    api_mode="chat_completions",  # ACP subprocess uses chat_completions routing
    env_vars=(),  # Auth inherited from the local Claude Code OAuth login
    base_url="acp://claude-agent",  # ACP internal scheme marker
    auth_type="external_process",
)

register_provider(claude_agent_acp)
