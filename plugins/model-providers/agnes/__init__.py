"""Agnes AI provider profile."""

from providers import register_provider
from providers.base import ProviderProfile

agnes = ProviderProfile(
    name="agnes",
    display_name="Agnes AI",
    description="Agnes AI (agnes-2.0-flash)",
    signup_url="https://agnes-ai.com",
    env_vars=("AGNES_API_KEY", "AGNES_BASE_URL"),
    base_url="https://apihub.agnes-ai.com/v1",
    fallback_models=("agnes-2.0-flash",),
)

register_provider(agnes)
