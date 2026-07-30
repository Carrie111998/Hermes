"""Telnyx provider profile.

Telnyx serves an OpenAI-compatible inference API at
``https://api.telnyx.com/v2/ai/openai`` — chat completions (streaming,
tool calling, usage on the final chunk) plus a standard ``/models``
catalog. The catalog mixes models hosted on Telnyx's own GPU
infrastructure (Kimi, GLM, MiniMax, Qwen, Llama — ``task:
"text-generation"``) with proxied frontier routes (``openai/gpt-5.x``,
``anthropic/claude-haiku-4-5``, … — ``task: "text generation"``, with a
space). Address models by their catalog ID, e.g. ``moonshotai/Kimi-K3``
or ``zai-org/GLM-5.2``.

Provider quirks, verified live (2026-07):

* ``/models`` requires Bearer auth — anonymous requests get a 401. The
  registry passes the resolved ``TELNYX_API_KEY`` into ``fetch_models``.
* Pricing in ``/models`` rows is quoted as per-1M-token strings
  (``pricing.unit == "1M_tokens"``). The unit-tagged branch in
  ``agent/model_metadata.py::_extract_pricing`` converts it to the
  per-token form the cost machinery expects.
* Error 10015: hosted models (plus a few proxied routes) reject requests
  that combine an output cap (``max_tokens`` /
  ``max_completion_tokens``) with function tools. No metadata predicts
  which models reject it. ``default_max_tokens`` stays ``None`` so the
  transport never sends a cap unless the user explicitly configures
  ``agent.max_tokens`` — auxiliary calls that do send one self-heal via
  the strip-and-retry in ``agent/auxiliary_client.py`` (the 10015 detail
  string contains "max_tokens").
* Errors use Telnyx's envelope ``{"errors": [{"code": "10015", ...}]}``
  rather than OpenAI's ``{"error": {...}}``; the detail text still
  reaches exception messages through the SDK.
"""

from providers import register_provider
from providers.base import ProviderProfile, _profile_user_agent


def _is_text_generation_task(task) -> bool:
    """True for both catalog spellings: "text-generation" and "text generation"."""
    return str(task).strip().lower().replace(" ", "-") == "text-generation"


class _TelnyxProfile(ProviderProfile):
    """Telnyx profile with task-filtered live catalog discovery."""

    def fetch_models(self, *, api_key=None, base_url=None, timeout=8.0):
        """Fetch the live catalog, keeping only text-generation models.

        The ``/models`` endpoint currently lists only chat models, but every
        row carries a ``task`` label; filtering here keeps embedding/rerank
        rows out of the chat picker if Telnyx adds them later. Rows without
        a ``task`` field pass through so a payload-shape change can't
        silently empty the picker.
        """
        effective_base = (base_url or self.base_url).rstrip("/")
        if not effective_base:
            return None

        import json
        import urllib.request

        from hermes_cli.urllib_security import open_credentialed_url

        req = urllib.request.Request(effective_base + "/models")
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", _profile_user_agent())

        try:
            with open_credentialed_url(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            items = data if isinstance(data, list) else data.get("data", [])
            return [
                m["id"]
                for m in items
                if isinstance(m, dict)
                and "id" in m
                and ("task" not in m or _is_text_generation_task(m["task"]))
            ]
        except Exception:
            return None


telnyx = _TelnyxProfile(
    name="telnyx",
    display_name="Telnyx",
    description="Telnyx — hosted open models + frontier routes, OpenAI-compatible",
    signup_url="https://portal.telnyx.com/#/app/api-keys",
    env_vars=("TELNYX_API_KEY",),
    base_url="https://api.telnyx.com/v2/ai/openai",
    auth_type="api_key",
    # Load-bearing None: an output cap combined with function tools trips
    # Telnyx error 10015 on all hosted models (see module docstring). Only
    # an explicit user ``agent.max_tokens`` ever sends a cap.
    default_max_tokens=None,
    # Auxiliary model for cheap side tasks (compression, title generation,
    # session search): cheapest modern model on the hosted tier
    # ($0.21/$1.20 per 1M tokens, 200k context as of 2026-07).
    default_aux_model="MiniMaxAI/MiniMax-M2.7",
    # Curated safety net shown in the picker when the live catalog fetch
    # fails, and the preferred ordering when it succeeds (curated-first
    # merge). All four verified live with multi-turn tool calling.
    fallback_models=(
        "moonshotai/Kimi-K3",
        "moonshotai/Kimi-K2.6",
        "zai-org/GLM-5.2",
        "MiniMaxAI/MiniMax-M3-MXFP8",
    ),
)

register_provider(telnyx)
