"""DeepInfra provider profile.

DeepInfra is an OpenAI-compatible inference gateway that hosts 100+ open
models (Step, GLM, Kimi, DeepSeek, MiniMax, Nemotron, Mistral, Qwen, …) as
well as image-gen / TTS / STT / embedding endpoints. The chat surface is
wired in through this profile; non-chat surfaces are wired in through
their respective plugin subsystems (``plugins/image_gen/deepinfra`` and
the TTS/STT dispatchers in ``tools/``).
"""

from providers import register_provider
from providers.base import ProviderProfile


class _DeepInfraProfile(ProviderProfile):
    """DeepInfra profile with live vision-default discovery.

    Owns its own vision default so shared vision resolution in
    ``agent/auxiliary_client.py`` stays provider-agnostic (a
    ``default_vision_model()`` hook call instead of an ``if provider ==
    "deepinfra"`` branch reaching into the catalog helpers).
    """

    def default_vision_model(self):  # type: ignore[override]
        """First vision-capable *chat* model from the live catalog, or None.

        Key-gated so a box without a DeepInfra credential never pays the
        catalog round-trip. Requires the ``chat`` surface tag (not just the
        ``vision`` capability) so an image-gen/edit model that merely carries
        a ``vision`` tag can't be picked as a chat-completions vision backend.

        The gate asks the SAME resolver that builds the client
        (``resolve_api_key_provider_credentials``), not ``os.environ``.

        That matters because ``os.environ`` is **not reliably populated**
        from where Hermes actually stores credentials (``~/.hermes/.env``
        and the auth pool). ``.env`` reaches the environment only via an
        explicit sync in ``hermes_cli.config``, which not every entry point
        calls -- so the answer depended on WHICH PROCESS asked. Measured
        2026-08-25 in a plain ``import agent.auxiliary_client``:
        ``get_env_value("DEEPSEEK_API_KEY")`` truthy,
        ``os.environ.get("DEEPSEEK_API_KEY")`` None.

        Process-dependent is worse than uniformly broken. The old gate could
        pass inside a long-lived gateway that had run the sync and fail in
        ``hermes setup`` moments later, so a correctly-installed DeepInfra
        key returned None here, the vision chain logged "catalog
        unreachable", and vision stayed dead while blaming the network --
        intermittently, which is the hardest shape to diagnose. Asking the
        credential resolver removes the dependency entirely.
        """
        try:
            from hermes_cli.auth import resolve_api_key_provider_credentials
            creds = resolve_api_key_provider_credentials("deepinfra")
            api_key = str(creds.get("api_key") or "").strip()
        except Exception:
            # Never let credential resolution break model discovery; treat an
            # unresolvable credential as "no key" and skip the round-trip.
            return None
        if not api_key:
            return None
        try:
            from hermes_cli.models import _fetch_deepinfra_models_by_tag
            items = _fetch_deepinfra_models_by_tag("chat")
        except Exception:
            return None
        for item in items or []:
            metadata = item.get("metadata") or {}
            tags = metadata.get("tags") if isinstance(metadata, dict) else None
            if isinstance(tags, list) and "vision" in tags:
                model_id = item.get("id")
                if model_id:
                    return model_id
        return None


deepinfra = _DeepInfraProfile(
    name="deepinfra",
    aliases=("deep-infra", "deepinfra-ai"),
    display_name="DeepInfra",
    description="DeepInfra — 100+ open models, pay-per-use",
    signup_url="https://deepinfra.com/dash/api_keys",
    env_vars=("DEEPINFRA_API_KEY", "DEEPINFRA_BASE_URL"),
    base_url="https://api.deepinfra.com/v1/openai",
    auth_type="api_key",
    # The catalog spans models with different output limits. Omitting a
    # provider-wide default lets DeepInfra apply its documented per-model cap;
    # an explicit user ``agent.max_tokens`` still passes through normally.
    default_max_tokens=None,
    # Auxiliary model — cheap/fast chat model the same provider uses for
    # side tasks (context compression, session search, web extract,
    # vision). This is the *only* hardcoded DeepInfra model in the
    # integration: aux resolution is synchronous (no time for a catalog
    # round-trip on every agent turn), so we need one explicit choice.
    # Every other surface (chat picker, image-gen, tts, stt, pricing)
    # discovers models live from
    # ``api.deepinfra.com/v1/openai/models?filter=true&sort_by=hermes``.
    default_aux_model="deepseek-ai/DeepSeek-V4-Flash",
    # ``fallback_models`` deliberately empty — the live catalog at
    # ``hermes_cli/models.py::_fetch_deepinfra_models`` is the source of
    # truth. When the live fetch fails (network/DNS), the picker shows
    # no options, which is preferable to silently routing the user to a
    # model that may have been retired upstream.
    fallback_models=(),
)

register_provider(deepinfra)
