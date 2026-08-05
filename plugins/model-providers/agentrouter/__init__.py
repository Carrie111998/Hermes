"""AgentRouter provider profiles (OpenAI-compatible + Anthropic Messages).

AgentRouter (https://agentrouter.org) is a relay that fronts several upstream
model families behind one API key. It exposes *two* wire protocols on the same
host, and the vendor docs are explicit that they are not interchangeable:

  - ``https://agentrouter.org/v1``  — OpenAI-compatible Chat Completions.
    Serves the GPT and GLM families (``gpt-5.6``, ``gpt-5.5``, ``glm-5.2``).
  - ``https://agentrouter.org``     — Anthropic Messages (note: no ``/v1``
    path segment; the SDK appends ``/v1/messages`` itself). Serves the Claude
    Opus family (``claude-opus-4-6``, ``claude-opus-4-7``, ``claude-opus-4-8``).

Both routes authenticate with the same ``AGENTROUTER_API_KEY`` (``ak-…``) sent
as ``Authorization: Bearer``. The Messages route therefore needs Bearer rather
than Anthropic's native ``x-api-key`` — wired in
``agent.anthropic_adapter._requires_bearer_auth``.

Hence two profiles from one plugin (same pattern as the MiniMax plugin): pick
``agentrouter`` for GPT/GLM, ``agentrouter-anthropic`` for Claude. The model
catalog is fetched live from ``/v1/models`` for both (it requires auth, so the
``fallback_models`` below cover the offline picker).

Docs: https://agentrouter.org/docs/index.html
"""

from providers import register_provider
from providers.base import ProviderProfile

# Both routes share one host. The catalog only exists on the OpenAI-compatible
# path, so the Anthropic profile points ``models_url`` at it explicitly rather
# than letting the default ``{base_url}/models`` resolve to the bare host.
_MODELS_URL = "https://agentrouter.org/v1/models"

agentrouter = ProviderProfile(
    name="agentrouter",
    aliases=("agent-router", "agentrouter-openai"),
    display_name="AgentRouter",
    description="AgentRouter — OpenAI-compatible relay (GPT, GLM)",
    signup_url="https://agentrouter.org/console/token",
    env_vars=("AGENTROUTER_API_KEY", "AGENTROUTER_BASE_URL"),
    base_url="https://agentrouter.org/v1",
    models_url=_MODELS_URL,
    auth_type="api_key",
    # default_aux_model left empty → auxiliary side tasks reuse the main model.
    # AgentRouter publishes no cheap/small model to pin one to.
    # entry [0] is the setup default; gpt-5.5 is the default AgentRouter's own
    # docs configure for every client they document.
    fallback_models=(
        "gpt-5.5",
        "gpt-5.6",
        "glm-5.2",
    ),
)

agentrouter_anthropic = ProviderProfile(
    name="agentrouter-anthropic",
    aliases=("agentrouter-claude",),
    api_mode="anthropic_messages",
    display_name="AgentRouter (Claude)",
    description="AgentRouter — Anthropic Messages relay (Claude Opus)",
    signup_url="https://agentrouter.org/console/token",
    env_vars=("AGENTROUTER_API_KEY", "AGENTROUTER_ANTHROPIC_BASE_URL"),
    # No ``/v1`` — the Anthropic SDK appends ``/v1/messages`` to this base.
    base_url="https://agentrouter.org",
    models_url=_MODELS_URL,
    auth_type="api_key",
    supports_vision=True,
    fallback_models=(
        "claude-opus-4-6",
        "claude-opus-4-8",
        "claude-opus-4-7",
    ),
)

# Register the OpenAI-compatible profile first so the shared ``agentrouter.org``
# hostname reverse-maps to ``agentrouter`` in agent/model_metadata.py (first
# writer wins there). Context windows resolve from the model id either way.
register_provider(agentrouter)
register_provider(agentrouter_anthropic)
