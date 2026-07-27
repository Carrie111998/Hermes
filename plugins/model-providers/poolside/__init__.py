"""Poolside provider profile — inference.poolside.ai.

Laguna-s-2.1 output cap: 32768 (confirmed by API: "max_tokens (65536): Input should be less than or equal to 32768").
"""

from providers import register_provider
from providers.base import ProviderProfile

poolside = ProviderProfile(
    name="poolside",
    aliases=(),
    env_vars=("POOLSIDE_API_KEY",),
    display_name="Poolside",
    description="Poolside — inference.poolside.ai",
    signup_url="https://poolside.ai/",
    base_url="https://inference.poolside.ai/v1",
    default_max_tokens=32768,
)

register_provider(poolside)