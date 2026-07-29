"""Kimi OAuth provider profile — reuse Kimi Code CLI OAuth tokens.

The Kimi Code CLI stores its OAuth2 tokens at
``~/.kimi-code/credentials/kimi-code.json``.  This provider reads those
tokens and exposes them as a Hermes provider, so Kimi subscription users
(K3, K2.7 Coding, etc.) don't need a separate platform API key.
"""

from providers import register_provider
from providers.base import ProviderProfile

kimi_oauth = ProviderProfile(
    name="kimi-oauth",
    aliases=(
        "kimi",
        "kimi-oauth-code",
        "kimi-oauth-cli",
        "kimi-code-oauth",
    ),
    display_name="Kimi Code (OAuth)",
    description="Kimi Code via OAuth tokens from Kimi Code CLI — no API key required",
    signup_url="https://kimi.moonshot.cn/",
    env_vars=(),  # OAuth — tokens in ~/.kimi-code/credentials/kimi-code.json, not env
    base_url="https://api.kimi.com/coding/v1",
    auth_type="oauth_external",
    default_max_tokens=65536,
)

register_provider(kimi_oauth)
