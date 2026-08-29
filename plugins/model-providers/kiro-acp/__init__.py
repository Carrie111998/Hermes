"""Kiro ACP provider profile.

kiro-acp uses an external ACP subprocess — NOT the standard HTTP
transport. api_mode="chat_completions" is the Hermes routing surface;
the ACP client speaks JSON-RPC over stdio to `kiro-cli acp`.
The profile captures auth + endpoint metadata for registry migration.
"""

from providers import register_provider
from providers.base import ProviderProfile


class KiroACPProfile(ProviderProfile):
    """Kiro ACP — external process, no REST models endpoint."""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Model listing is the short verified Kiro picker catalog."""
        return None


kiro_acp = KiroACPProfile(
    name="kiro-acp",
    aliases=("kiro", "kiro-cli", "kiro-agent"),
    api_mode="chat_completions",
    env_vars=(),  # Managed by the Kiro CLI login
    base_url="acp://kiro",
    auth_type="external_process",
    display_name="Kiro ACP",
    description="Kiro ACP (Spawns kiro-cli acp; uses your Kiro login)",
)

register_provider(kiro_acp)
