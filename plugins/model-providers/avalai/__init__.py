"""AvalAI provider profile.

AvalAI (avalai.ir) is an OpenAI-compatible AI gateway that fronts models
from OpenAI, Anthropic, Google, DeepSeek and other vendors behind a single
API key and endpoint. Chat inference and the model catalog are served from
``https://api.avalai.ir/v1`` with standard Bearer auth.
"""

from providers import register_provider
from providers.base import ProviderProfile


avalai = ProviderProfile(
    name="avalai",
    aliases=("aval-ai", "avalai-ir"),
    display_name="AvalAI",
    description="AvalAI — OpenAI-compatible gateway for OpenAI, Anthropic, Gemini and open models",
    signup_url="https://chat.avalai.ir/platform/api-keys",
    env_vars=("AVALAI_API_KEY", "AVALAI_BASE_URL"),
    base_url="https://api.avalai.ir/v1",
    auth_type="api_key",
    # The catalog spans upstream vendors with different output limits;
    # omit a provider-wide default so AvalAI applies its per-model cap.
    default_max_tokens=None,
    # Cheap/fast model for auxiliary tasks (compression, session search,
    # vision fallback). Aux resolution is synchronous, so one model is
    # pinned here; everything else is discovered live from /models.
    default_aux_model="gpt-4o-mini",
    # ``fallback_models`` deliberately empty — the gateway's catalog
    # changes as upstream vendors ship models, so the live ``/models``
    # fetch is the source of truth. When the live fetch fails, the picker
    # shows no options rather than routing to a retired model.
    fallback_models=(),
)

register_provider(avalai)
