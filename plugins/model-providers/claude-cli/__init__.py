"""Official Claude CLI external-process provider profile."""

from providers import register_provider
from providers.base import ProviderProfile


class ClaudeCLIProfile(ProviderProfile):
    def fetch_models(self, *, api_key=None, base_url=None, timeout=8.0):
        del api_key, base_url, timeout
        return ["claude-opus-5"]


claude_cli = ClaudeCLIProfile(
    name="claude-cli",
    aliases=("claude_cli",),
    api_mode="claude_cli",
    env_vars=(),
    base_url="",
    auth_type="external_process",
    supports_health_check=False,
    supports_vision=False,
    fallback_models=("claude-opus-5",),
    display_name="Claude CLI",
    description="Official Claude CLI via the existing local login and policy boundary",
)

register_provider(claude_cli)
