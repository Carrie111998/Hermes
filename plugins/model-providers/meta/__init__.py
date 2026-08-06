"""Meta provider profile — Muse Spark via Meta Model API."""

from providers import register_provider
from providers.base import ProviderProfile

meta = ProviderProfile(
    name="meta",
    aliases=("meta-ai", "muse-spark"),
    display_name="Meta",
    description="Meta — Muse Spark via Meta Model API",
    signup_url="https://dev.meta.ai/",
    env_vars=("META_API_KEY",),
    base_url="https://api.meta.ai/v1",
    auth_type="api_key",
    default_aux_model="muse-spark-1.1",
    fallback_models=(
        "muse-spark-1.1",
        "muse-spark-1",
    ),
)

register_provider(meta)
