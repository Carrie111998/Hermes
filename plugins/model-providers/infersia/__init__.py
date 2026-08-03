"""Infersia provider profile.

Infersia serves open-weight models on dedicated GPUs through an
OpenAI-compatible chat-completions endpoint, and publishes the exact
quantisation, hardware and measured latency behind every model.

Address models by their catalogue ID, e.g. ``deepseek/deepseek-v4-flash-0731``
or ``qwen/qwen3.6-35b-a3b``. Appending ``:free`` selects a rate-limited free
variant where one is published.

DeepSeek V4 Flash is served at its full 1,048,576-token window rather than a
reduced slice, which is the main reason to reach for this provider on
long-context work.
"""

from providers import register_provider
from providers.base import ProviderProfile


infersia = ProviderProfile(
    name="infersia",
    aliases=("infersia-ai",),
    display_name="Infersia",
    description="Infersia — open-weight models with published quantisation",
    signup_url="https://infersia.com/dashboard/keys",
    env_vars=("INFERSIA_API_KEY", "INFERSIA_BASE_URL"),
    base_url="https://api.infersia.com/v1",
    auth_type="api_key",
    # Images are accepted inside tool-result messages, verified against the
    # served vision model (Step 3.7 Flash) with a multipart tool message.
    supports_vision=True,
    # Prefix caching is automatic and unkeyed here. The endpoint tolerates a
    # ``prompt_cache_key`` field rather than honouring it, and this flag is
    # documented as opt-in for endpoints that explicitly accept it, so
    # claiming it would advertise behaviour that does not exist.
    supports_prompt_cache_key=False,
    # Auxiliary model for cheap side tasks. Qwen3 8B is the smallest thing in
    # the catalogue and the only hardcoded model id here; everything else is
    # discovered live.
    default_aux_model="qwen/qwen3-8b",
    # Deliberately empty, following the DeepInfra profile's reasoning: the
    # live catalogue at ``{base_url}/models`` is the source of truth, and it
    # returns OpenAI-shaped entries with pricing and context length. When the
    # fetch fails the picker shows nothing, which is better than routing
    # someone to a model id that has since been retired — this catalogue is
    # still changing week to week.
    fallback_models=(),
)

register_provider(infersia)
