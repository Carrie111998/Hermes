"""Native Anthropic provider profile."""

import json
import logging
import urllib.request

from hermes_cli.urllib_security import open_credentialed_url
from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)


class AnthropicProfile(ProviderProfile):
    """Native Anthropic — uses x-api-key header, not Bearer."""


    def fetch_usage(
        self,
        *,
        credential=None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ):
        """Anthropic subscription windows (5-hour + weekly), percent-based.

        Wraps the fetcher that already backs ``/usage``. The endpoint accepts
        an OAuth token ONLY (a plain API key 401s), and that rule already lives
        in ``_fetch_anthropic_account_usage`` / ``_is_oauth_token`` — so this
        is the one plugin that lets the existing resolver pick the token
        instead of using the aggregator's credential. A key-only login
        correctly surfaces as "no usage", not as a failure.
        """
        from agent.account_usage import _fetch_anthropic_account_usage
        from agent.provider_usage_types import from_account_snapshot

        return from_account_snapshot(
            _fetch_anthropic_account_usage(),
            provider="anthropic",
            display_name="Claude",
        )

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Anthropic uses x-api-key header and anthropic-version."""
        if not api_key:
            return None
        try:
            req = urllib.request.Request("https://api.anthropic.com/v1/models")
            req.add_header("x-api-key", api_key)
            req.add_header("anthropic-version", "2023-06-01")
            req.add_header("Accept", "application/json")
            with open_credentialed_url(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            return [
                m["id"]
                for m in data.get("data", [])
                if isinstance(m, dict) and "id" in m
            ]
        except Exception as exc:
            logger.debug("fetch_models(anthropic): %s", exc)
            return None


anthropic = AnthropicProfile(
    name="anthropic",
    aliases=("claude", "claude-oauth", "claude-code"),
    api_mode="anthropic_messages",
    env_vars=("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"),
    base_url="https://api.anthropic.com",
    auth_type="api_key",
    default_aux_model="claude-haiku-4-5-20251001",
    # Rolling 5-hour window: minute polling would tell you nothing new.
    usage_ttl=300,
)

register_provider(anthropic)
