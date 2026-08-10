"""Meta Model API (Muse Spark) provider profile.

Meta's Model API (``https://api.meta.ai/v1``) serves the Muse Spark reasoning
model family over OpenAI-compatible surfaces. The Responses API
(``/v1/responses``) is the wire that engages prompt caching: Muse reports
0 cached tokens on ``/v1/chat/completions`` even with the retention hint,
while ``/v1/responses`` sustains 93-99% cache hits on agentic workloads
(measured). Hermes routes the host through ``codex_responses`` automatically
via ``host_mandated_api_mode`` (hermes_cli/providers.py) and sends the
``prompt_cache_retention: 24h`` opt-in hint via
``agent/transports/codex._default_prompt_cache_retention_for_request`` —
this profile declares the provider so it surfaces in ``hermes setup`` /
``hermes tools`` / the model picker like any other bundled provider.

Key facts:
  - env var: ``META_API_KEY`` (contributor-tier keys authorize only
    ``muse-spark-1.2-contributor``; standard-tier keys also authorize plain
    ``muse-spark-1.2`` / ``1.1``).
  - reasoning effort: Muse accepts ``minimal|low|medium|high|xhigh``; there is
    no ``none`` (the API rejects it), so leave the global reasoning config at a
    valid level when using this provider.
  - rate limits: contributor tier 60 req/min / 2M tok/min; standard 3,000
    req/min / 4M tok/min (per team, not per key).
"""

from providers import register_provider
from providers.base import ProviderProfile

meta = ProviderProfile(
    name="meta",
    aliases=("meta-ai", "muse", "meta-model-api"),
    api_mode="codex_responses",
    env_vars=("META_API_KEY",),
    display_name="Meta Model API",
    description="Meta Model API — Muse Spark (Responses API)",
    signup_url="https://dev.meta.ai/",
    fallback_models=(
        "muse-spark-1.2-contributor",
        "muse-spark-1.2",
        "muse-spark-1.1",
    ),
    base_url="https://api.meta.ai/v1",
    default_aux_model="muse-spark-1.2-contributor",
)

register_provider(meta)
