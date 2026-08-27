"""OpenAI Codex (Responses API) provider profile."""

from providers import register_provider
from providers.base import ProviderProfile


class CodexProfile(ProviderProfile):
    """Codex — adds plan-usage reporting on top of the plain profile."""

    def fetch_usage(
        self,
        *,
        credential=None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ):
        """Codex rate-limit windows, percent-based.

        Wraps the fetcher that already backs ``/usage``. Passing the
        aggregator's resolved token matters: ``_resolve_codex_usage_credentials``
        short-circuits on an explicit key, so supplying one keeps this off the
        credential pool entirely (the hook's contract — see
        ``ProviderProfile.fetch_usage``).
        """
        from agent.account_usage import _fetch_codex_account_usage
        from agent.provider_usage_types import from_account_snapshot

        token = getattr(credential, "runtime_api_key", None) or getattr(
            credential, "access_token", None
        )
        resolved_base = (
            base_url
            or getattr(credential, "runtime_base_url", None)
            or getattr(credential, "base_url", None)
        )

        return from_account_snapshot(
            _fetch_codex_account_usage(base_url=resolved_base, api_key=token),
            provider="openai-codex",
            display_name="Codex",
        )


openai_codex = CodexProfile(
    name="openai-codex",
    aliases=("codex", "openai_codex"),
    api_mode="codex_responses",
    env_vars=(),  # OAuth external — no API key
    base_url="https://chatgpt.com/backend-api/codex",
    auth_type="oauth_external",
    # Rolling windows measured in hours; a 5-minute cache is plenty.
    usage_ttl=300,
)

register_provider(openai_codex)
