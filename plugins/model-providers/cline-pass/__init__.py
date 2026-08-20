"""ClinePass provider profile — curated open-weight models via cline.bot.

ClinePass is a flat $9.99/month subscription from Cline (cline.bot) that
provides 2-5x usage on popular open coding models compared to standard API
rate.  The API is OpenAI-compatible Chat Completions at api.cline.bot/api/v1.

Auth: CLINE_API_KEY (Bearer token), created at
  https://app.cline.bot/dashboard/settings/api-keys
"""

from __future__ import annotations

from hermes_cli import __version__ as _HERMES_VERSION
from providers import register_provider
from providers.base import ProviderProfile

_ATTRIBUTION_HEADERS = {
    "HTTP-Referer": "https://hermes-agent.nousresearch.com",
    "X-Title": "Hermes Agent",
    "User-Agent": f"HermesAgent/{_HERMES_VERSION}",
}


cline_pass = ProviderProfile(
    name="cline-pass",
    aliases=("clinepass", "cline_pass"),
    env_vars=("CLINE_API_KEY",),
    base_url="https://api.cline.bot/api/v1",
    default_headers=dict(_ATTRIBUTION_HEADERS),
    default_aux_model="cline-pass/deepseek-v4-flash",
)

register_provider(cline_pass)
