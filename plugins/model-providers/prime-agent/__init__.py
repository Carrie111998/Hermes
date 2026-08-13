"""Prime Agent provider: local Prime Agent ACP subprocess."""

from providers import register_provider
from providers.base import ProviderProfile


class PrimeAgentProfile(ProviderProfile):
    def fetch_models(self, *, api_key=None, base_url=None, timeout=8.0):
        return ["deepseek-v4-pro", "deepseek-v4-flash", "gpt-5.6-luna"]


prime_agent = PrimeAgentProfile(
    name="prime-agent",
    aliases=("prime",),
    api_mode="chat_completions",
    display_name="Prime Agent",
    description="Prime Agent (local ACP bridge for Codex and DeepSeek)",
    base_url="acp://prime-agent",
    auth_type="external_process",
    supports_health_check=False,
    fallback_models=("deepseek-v4-pro", "deepseek-v4-flash", "gpt-5.6-luna"),
)

register_provider(prime_agent)
