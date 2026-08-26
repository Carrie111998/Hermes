"""DeepInfra provider profile.

DeepInfra is an OpenAI-compatible inference gateway that hosts 100+ open
models (Step, GLM, Kimi, DeepSeek, MiniMax, Nemotron, Mistral, Qwen, …) as
well as image-gen / TTS / STT / embedding endpoints. The chat surface is
wired in through this profile; non-chat surfaces are wired in through
their respective plugin subsystems (``plugins/image_gen/deepinfra`` and
the TTS/STT dispatchers in ``tools/``).
"""

import logging

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)


# Preference order for the auto-discovered vision default, most preferred
# first. An entry that the catalog does not offer is simply skipped, so a
# retired or renamed id degrades to catalog order instead of killing vision.
#
# WHY a preference at all: the catalog is served in DeepInfra's own
# popularity/recency ranking, so "first chat+vision model" is a MOVING
# TARGET. DeepInfra reorders or adds a model and Hermes silently starts
# sending images somewhere else, with no log line naming the change.
#
# Measured 2026-08-25, that first slot held Qwen/Qwen3.5-397B-A17B -- a
# REASONING model. It answered every probe correctly, but it spent 492-823
# output tokens thinking per image where a dedicated VL model spent 26-33,
# took 19-35s instead of 1.5-2.7s, and cost ~8x more (~$2.63 vs ~$0.33 per
# 1000 calls at ~1500 input tokens). Reasoning also makes a model
# budget-sensitive in a way callers do not expect: at a tight max_tokens it
# returns EMPTY content with finish_reason "length" and NO error, having
# spent the whole budget thinking. The in-tree vision tools pass 2000 and
# 4000 (tools/vision_tools.py) so they clear it comfortably today, but
# nothing enforces that, and a caller that trimmed the budget would get
# blank answers with nothing to diagnose from.
#
# Qwen3-VL-*-Instruct are dedicated vision-language models with no reasoning
# phase. The 235B is preferred over the cheaper 30B / Gemma / Mistral
# options because all five answered every probe correctly -- the probes did
# NOT discriminate quality, so the larger model is the safer default at a
# price still far below the incumbent. Revisit with a harder eval before
# trading further down.
#
# Measured at the pathological budget, max_tokens=16 on a solid-colour PNG:
# Qwen3.5-397B returned content='' / finish_reason='length' / 16 output
# tokens; Qwen3-VL-235B returned 'Red' / 'stop' / 2 tokens. So preferring a
# non-reasoning VL model REMOVES the empty-answer failure mode rather than
# merely staying clear of it.
_VISION_MODEL_PREFERENCE = (
    "Qwen/Qwen3-VL-235B-A22B-Instruct",
)

# Last vision model this process reported. Discovery re-runs on every vision
# availability check (only the underlying catalog HTTP fetch is cached), so
# an unconditional log here would be per-call noise. Reporting only on CHANGE
# gives one line at selection -- and a second line if the answer ever moves
# mid-process, which is exactly the drift worth seeing.
_last_reported_vision_model = None


def _report_vision_model(model_id, eligible_count, from_preference):
    """Log the discovered vision default once, and again only if it changes."""
    global _last_reported_vision_model
    if model_id == _last_reported_vision_model:
        return
    _last_reported_vision_model = model_id
    logger.info(
        "DeepInfra vision default: %s (%s; %d vision-capable chat model%s "
        "in catalog)",
        model_id,
        "preferred" if from_preference
        else "NOT in preference list -- fell back to catalog order",
        eligible_count,
        "" if eligible_count == 1 else "s",
    )


class _DeepInfraProfile(ProviderProfile):
    """DeepInfra profile with live vision-default discovery.

    Owns its own vision default so shared vision resolution in
    ``agent/auxiliary_client.py`` stays provider-agnostic (a
    ``default_vision_model()`` hook call instead of an ``if provider ==
    "deepinfra"`` branch reaching into the catalog helpers).
    """

    def default_vision_model(self):  # type: ignore[override]
        """Preferred vision-capable *chat* model from the live catalog, or None.

        Selection is preference-ordered, not catalog-ordered: the first entry
        of :data:`_VISION_MODEL_PREFERENCE` that the catalog actually offers
        wins, and catalog order is only the fallback. See that tuple for why.

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

        eligible = []
        for item in items or []:
            metadata = item.get("metadata") or {}
            tags = metadata.get("tags") if isinstance(metadata, dict) else None
            if isinstance(tags, list) and "vision" in tags:
                model_id = item.get("id")
                if model_id:
                    eligible.append(model_id)
        if not eligible:
            return None

        # Match case-insensitively but return the catalog's own spelling --
        # that string is sent to the API, so it must be the id DeepInfra
        # published, not the one written in the preference tuple.
        by_lower = {str(m).lower(): m for m in eligible}
        chosen = None
        for preferred in _VISION_MODEL_PREFERENCE:
            chosen = by_lower.get(preferred.lower())
            if chosen:
                break
        from_preference = chosen is not None
        if chosen is None:
            # Every preferred id has been retired or renamed upstream. Fall
            # back to catalog order rather than returning None: a working but
            # unpreferred vision backend beats no vision at all, and the log
            # line below says which case this was.
            chosen = eligible[0]

        _report_vision_model(chosen, len(eligible), from_preference)
        return chosen


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
