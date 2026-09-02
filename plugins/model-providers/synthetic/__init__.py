"""Synthetic provider profile.

Synthetic (synthetic.new) is an OpenAI-compatible inference gateway that
serves always-on models (GLM, Kimi, Qwen, Nemotron, gpt-oss) behind stable
``syn:`` routing aliases plus pinned ``hf:`` model IDs. Bearer auth via
``SYNTHETIC_API_KEY``; docs at https://dev.synthetic.new/docs/api/overview.

``syn:`` aliases (syn:large:text, syn:small:text, ...) are the recommended
default: they always route to the latest recommended model for a category,
so the profile pins ``syn:large:text`` as its aux default instead of a
concrete catalog ID that would rot when Synthetic rotates models.
"""

from providers import register_provider
from providers.base import ProviderProfile


synthetic = ProviderProfile(
    name="synthetic",
    aliases=("synthetic-new", "syntheticnew", "synthetic.new"),
    display_name="Synthetic",
    description="Synthetic — OpenAI-compatible API, syn: aliases route to latest models",
    signup_url="https://synthetic.new/",
    env_vars=("SYNTHETIC_API_KEY", "SYNTHETIC_BASE_URL"),
    base_url="https://api.synthetic.new/openai/v1",
    auth_type="api_key",
    # syn: aliases are Synthetic's recommended way to address models — they
    # never 404 when older models are rotated out (unlike pinned hf: IDs).
    default_aux_model="syn:small:text",
    fallback_models=(
        "syn:large:text",
        "syn:small:text",
        "syn:large:vision",
        "syn:small:vision",
    ),
)

register_provider(synthetic)