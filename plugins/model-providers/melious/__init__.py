"""Melious provider profile.

Melious is an OpenAI-compatible inference gateway running open-weight models on
European infrastructure (GDPR / TTDSG). It serves ~50 tool-calling chat models
alongside embedding, image, audio, and guardrail endpoints; only the chat
surface is wired in here.

Everything provider-specific in this file exists because of one endpoint:
``GET /v1/models?include_meta=true``. On top of the plain OpenAI catalog it
returns a ``_meta`` block per model carrying ``type``, ``capabilities``,
``context_length``, and ``pricing``. That single payload lets the profile
(a) keep non-chat models out of the picker, (b) resolve the cheap auxiliary
tier from live pricing instead of a hardcoded id, and (c) resolve a vision
default without a name-check branch in shared code — so all three hooks below
read one cached fetch.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.melious.ai/v1"

# Catalog TTL. The endpoint is documented as cached 5 min server-side, so a
# matching client TTL adds no staleness while keeping the synchronous aux- and
# vision-resolution paths off the network on every agent turn.
_CATALOG_TTL_SECONDS = 300.0

# Metadata keys we depend on. Named here so a rename upstream shows up as one
# diff rather than string literals scattered across three hooks.
_META = "_meta"
_TYPE_CHAT = "chat"
_CAP_TOOLS = "function_calling"
_CAP_VISION = "vision"


class _MeliousProfile(ProviderProfile):
    """Melious profile driven by the live ``?include_meta=true`` catalog.

    The overrides all share :meth:`_catalog`, a TTL-cached fetch of the
    annotated model list. Each one degrades to the generic behaviour when the
    metadata is unavailable — a proxy that only implements plain
    ``/v1/models``, a network failure, or an ``_meta`` schema change upstream.
    """

    # Cache is per-instance state on a module-level singleton, so a user plugin
    # that replaces this profile gets its own cache rather than inheriting a
    # populated one.
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._catalog_lock = threading.Lock()
        self._catalog_cache: list[dict[str, Any]] | None = None
        self._catalog_fetched_at = 0.0

    # ── shared catalog fetch ──────────────────────────────────────────────

    def _fetch_catalog(
        self, *, api_key: str, base_url: str, timeout: float
    ) -> list[dict[str, Any]] | None:
        """GET ``{base_url}/models?include_meta=true``, or None on any failure.

        Uses the same credential-safe opener the base class uses so a redirect
        cannot leak the key to another origin.
        """
        import urllib.request

        from hermes_cli.urllib_security import open_credentialed_url

        url = base_url.rstrip("/") + "/models?include_meta=true"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("Accept", "application/json")
        for key, value in self.default_headers.items():
            req.add_header(key, value)

        try:
            with open_credentialed_url(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode())
        except Exception as exc:
            logger.debug("melious catalog fetch failed: %s", exc)
            return None

        items = payload if isinstance(payload, list) else payload.get("data", [])
        return [m for m in items if isinstance(m, dict) and m.get("id")]

    def _catalog(
        self, *, api_key: str | None = None, timeout: float = 8.0
    ) -> list[dict[str, Any]] | None:
        """Return the annotated catalog for the *default* endpoint, TTL-cached.

        Deliberately keyed to nothing: the cache only ever holds the default
        ``base_url``'s catalog. Callers that pass a custom base URL bypass this
        entirely (see :meth:`fetch_models`), so one slot cannot serve a proxy's
        answer to a caller expecting the real one.

        Single-flight: the fetch happens while holding the lock, and every
        waiter re-checks freshness after acquiring it. Parallel subagents each
        resolve an aux and vision model as they start, so on a cold process the
        release-then-fetch shape sent one ``/v1/models`` request per thread —
        24 for 24 threads, measured — instead of one. Holding the lock across
        the request costs a waiter at most ``timeout`` seconds, which it would
        have spent on its own duplicate request anyway.
        """
        key = (api_key or "").strip()
        if not key:
            return None

        def _fresh() -> bool:
            return (
                self._catalog_cache is not None
                and (time.monotonic() - self._catalog_fetched_at) < _CATALOG_TTL_SECONDS
            )

        with self._catalog_lock:
            if _fresh():
                return self._catalog_cache

            items = self._fetch_catalog(
                api_key=key, base_url=self.base_url, timeout=timeout
            )
            if items is None:
                # Leave any previous (stale) entry alone rather than caching the
                # failure — the next caller gets to retry, and a stale catalog
                # still beats none.
                return self._catalog_cache

            self._catalog_cache = items
            self._catalog_fetched_at = time.monotonic()
            return items

    @staticmethod
    def _capabilities(entry: dict[str, Any]) -> dict[str, Any]:
        meta = entry.get(_META)
        caps = meta.get("capabilities") if isinstance(meta, dict) else None
        return caps if isinstance(caps, dict) else {}

    @classmethod
    def _is_chat(cls, entry: dict[str, Any]) -> bool:
        """True when the entry is a chat model.

        An entry with no ``_meta.type`` is kept: if the annotation ever goes
        away, an unfiltered list beats an empty picker.
        """
        meta = entry.get(_META)
        declared = meta.get("type") if isinstance(meta, dict) else None
        return declared is None or declared == _TYPE_CHAT

    @classmethod
    def _does_tools(cls, entry: dict[str, Any]) -> bool:
        """True only when the entry explicitly advertises tool calling.

        Strict on purpose. ``function_calling`` is densely annotated — every
        chat model in the catalog carries it bar one — so "unstated" is far
        more likely to mean "not supported" than "annotation missing", and the
        one unstated model (a VL checkpoint) genuinely cannot call tools.
        ``fetch_models`` re-widens if this ever empties the list, so strictness
        here costs nothing when the annotation degrades.
        """
        return cls._capabilities(entry).get(_CAP_TOOLS) is True

    @classmethod
    def _does_vision(cls, entry: dict[str, Any]) -> bool:
        """True when the entry accepts image input.

        Reads ``_meta.input_modalities`` rather than
        ``capabilities.vision``: the capability flag is set on only a handful
        of the models that actually take images, while the modality list is
        populated consistently. Both are accepted so a later backfill of the
        flag can't narrow this.
        """
        meta = entry.get(_META)
        if not isinstance(meta, dict):
            return False
        modalities = meta.get("input_modalities")
        if isinstance(modalities, list) and "image" in modalities:
            return True
        return cls._capabilities(entry).get(_CAP_VISION) is True

    @classmethod
    def _price(cls, entry: dict[str, Any]) -> float | None:
        """Blended EUR/Mtok cost, or None when the entry isn't priced.

        Input and output are weighted equally. Auxiliary work — compression
        summaries, vision descriptions, session-search digests — reads a large
        prompt and writes a short answer, so a pure output-price sort would
        pick a model that is expensive to feed. Equal weighting is the
        conservative middle rather than a tuned guess.
        """
        meta = entry.get(_META)
        pricing = meta.get("pricing") if isinstance(meta, dict) else None
        if not isinstance(pricing, dict):
            return None
        try:
            in_cost = float(pricing["input_cost_per_million_eur"])
            out_cost = float(pricing["output_cost_per_million_eur"])
        except (KeyError, TypeError, ValueError):
            return None
        return in_cost + out_cost

    def _cheapest(self, items: list[dict[str, Any]], *, vision: bool = False) -> str:
        """Cheapest priced chat+tools model id, or "" when none qualifies."""
        best_id, best_price = "", None
        for entry in items:
            if not (self._is_chat(entry) and self._does_tools(entry)):
                continue
            if vision and not self._does_vision(entry):
                continue
            price = self._price(entry)
            if price is None:
                continue
            if best_price is None or price < best_price:
                best_id, best_price = str(entry["id"]), price
        return best_id

    # ── hooks ─────────────────────────────────────────────────────────────

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Chat, tool-calling model ids only.

        The plain ``/v1/models`` list mixes embedding, image, audio, and
        guardrail models in with chat, so the unfiltered catalog would offer
        an embedding model as an agent's chat model. Filtering here rather
        than in ``hermes_cli/models.py`` keeps the provider quirk inside the
        provider (DeepInfra has the same problem and solves it with a branch
        in shared code).

        A caller-supplied ``base_url`` that differs from the default means the
        user pointed ``MELIOUS_BASE_URL`` at a proxy or gateway, which need not
        implement ``?include_meta=true`` — those requests go straight to the
        generic OpenAI-shaped path.
        """
        caller_base = (base_url or "").strip()
        if caller_base and caller_base.rstrip("/") != self.base_url.rstrip("/"):
            return super().fetch_models(
                api_key=api_key, base_url=base_url, timeout=timeout
            )

        items = self._catalog(api_key=api_key, timeout=timeout)
        if items is None:
            # No annotated catalog (no key, network failure, non-JSON body).
            # The generic path still returns the unfiltered list, which beats
            # an empty picker.
            return super().fetch_models(
                api_key=api_key, base_url=base_url, timeout=timeout
            )

        chat = [entry for entry in items if self._is_chat(entry)]
        agentic = [str(e["id"]) for e in chat if self._does_tools(e)]
        if agentic:
            return agentic

        # Strict tool-calling filter matched nothing. Rather than hand back an
        # empty picker, widen one step at a time: every chat model, then the
        # generic unfiltered catalog. Reachable only if Melious stops
        # annotating ``function_calling`` (or renames it), which is exactly
        # when a hard filter would otherwise look like an outage.
        logger.debug(
            "melious: no model advertised %s — falling back to all chat models",
            _CAP_TOOLS,
        )
        if chat:
            return [str(e["id"]) for e in chat]
        return super().fetch_models(api_key=api_key, base_url=base_url, timeout=timeout)

    def resolve_aux_model(self, *, vision: bool = False) -> str:
        """Cheapest live tool-calling chat model, or "".

        ``default_aux_model`` is a constant in source and therefore rots the
        moment Melious retires that id. Melious publishes per-model pricing in
        the catalog, so the cheap tier can track upstream instead. Contract
        from the base class: cheap to call (TTL-cached), never raises, returns
        "" so the caller falls through to ``default_aux_model``.
        """
        try:
            from agent.secret_scope import get_secret

            api_key = (get_secret("MELIOUS_API_KEY") or "").strip()
            if not api_key:
                return ""
            items = self._catalog(api_key=api_key)
            if not items:
                return ""
            return self._cheapest(items, vision=vision)
        except Exception:
            logger.debug("melious resolve_aux_model failed", exc_info=True)
            return ""

    def default_vision_model(self) -> str | None:
        """Cheapest live chat model that does both vision and tool calling.

        Load-bearing rather than decorative: ``default_aux_model`` is a
        Hermes 4 model, and the Hermes 4 family is text-only — so without this
        hook vision side tasks would be routed to a model that cannot see.
        Key-gated so a box with no ``MELIOUS_API_KEY`` never pays a round-trip.
        """
        try:
            from agent.secret_scope import get_secret

            api_key = (get_secret("MELIOUS_API_KEY") or "").strip()
            if not api_key:
                return None
            items = self._catalog(api_key=api_key)
            if not items:
                return None
            return self._cheapest(items, vision=True) or None
        except Exception:
            logger.debug("melious default_vision_model failed", exc_info=True)
            return None


melious = _MeliousProfile(
    name="melious",
    aliases=("melious-ai",),
    display_name="Melious",
    description="Melious — open-weight models on European infrastructure (GDPR/TTDSG)",
    signup_url="https://melious.ai/account/api/keys",
    # API key first, base-URL override last: auth.py, config.py, and doctor.py
    # all split this tuple on the _BASE_URL suffix.
    env_vars=("MELIOUS_API_KEY", "MELIOUS_BASE_URL"),
    base_url=_BASE_URL,
    auth_type="api_key",
    # Verified empirically against the live API, per the note in
    # tools/vision_tools.py: an image part inside a role="tool" message is
    # accepted and actually read (qwen3.5-9b described the test image).
    # It is per-model, not provider-wide — mistral-small-3.2-24b-instruct
    # takes the same image in a user message but 400s on it in a tool result.
    # Declaring True is still right: it enables the native fast path for the
    # models that work, and run_agent.py's _no_list_tool_content_models
    # records the exceptions after one failed round-trip. The blunter
    # supports_vision_tool_messages=False (Xiaomi's fix) would downgrade
    # every Melious vision model to a text summary to spare one.
    supports_vision=True,
    # The catalog reports ``max_output_tokens: null`` for all but two models —
    # no provider-wide cap to apply. An explicit user ``agent.max_tokens``
    # still passes through.
    default_max_tokens=None,
    # Static floor under resolve_aux_model(): Hermes 4 70B is the cheapest
    # Nous-family tool-calling model here, so an offline box still gets a
    # sane aux pick. Vision aux goes through default_vision_model() instead,
    # since Hermes 4 is text-only.
    default_aux_model="hermes-4-70b",
    # Shown when the live catalog fetch fails. Entry [0] is the setup default:
    # Hermes 4 405B, Nous Research's own model, which Melious serves on
    # European infrastructure. Tool-calling models only, per the field's
    # contract in providers/base.py.
    fallback_models=(
        "hermes-4-405b",
        "hermes-4-70b",
        "glm-5.2",
        "glm-5.1",
        "deepseek-v4-pro-0813",
        "kimi-k2.6",
        "minimax-m3",
        "qwen3.5-397b-a17b",
        "qwen3-coder-next",
    ),
)

register_provider(melious)
