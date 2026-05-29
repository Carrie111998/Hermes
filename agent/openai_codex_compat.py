"""OpenAI Codex ``output=None`` compatibility guard.

The ChatGPT Codex backend (``chatgpt.com/backend-api/codex``) emits a final
``response.completed`` whose ``response.output`` is ``None`` — the model's real
text already arrived via ``response.output_text.delta`` events; only the final
aggregate snapshot is ``None``. The openai SDK (2.31.0, and current upstream
``main``) iterates ``for output in response.output`` inside ``parse_response``
with no None-guard, raising ``TypeError: 'NoneType' object is not iterable``
while the ``responses.stream()`` accumulator processes the completion event.
``run_agent`` then classifies that ``TypeError`` as a non-retryable client
error, so every Codex/gpt-5.5 cron reports "Agent completed but produced empty
response".

This module installs a runtime guard that coerces ``response.output is None``
to ``[]`` before the SDK iterates it. ``parse_response`` then returns an
empty-output ``ParsedResponse``, and Hermes's existing stream backfill in
``run_agent._run_codex_stream`` repopulates the output from the collected
``output_text.delta`` text (or the ``response.output_item.done`` items).

Why a runtime monkeypatch instead of an SDK upgrade or a site-packages edit:

* Upstream openai-python ``main`` still has the unguarded loop, so bumping the
  SDK does not fix this (and a blind bump on a heavily-diverged fork is risky).
* Editing ``site-packages/.../_parsing/_responses.py`` in place is silently
  reverted by any ``pip install`` of ``openai``.

``parse_response`` is imported *by value* into two other modules — the
streaming accumulator (``openai.lib.streaming.responses._responses``, the
binding the codex ``.stream()`` path actually invokes) and
``openai.resources.responses.responses`` — so the guard must rebind the source
AND both importers. Call :func:`apply_codex_output_none_guard` once at import
time from every codex entry point (it is idempotent and cheap).

See memory ``codex_cron_empty_response_fix`` / ``openai_sdk_codex_output_none_patch``
(R57) and MemPalace drawer ``hermes/codex-cron-empty-response-rootcause-2026-05-28``.
"""

from __future__ import annotations

import functools
import importlib
import logging
import sys

logger = logging.getLogger(__name__)

# The source module that defines ``parse_response``.
_SOURCE_MODULE = "openai.lib._parsing._responses"

# Modules that import ``parse_response`` BY VALUE and so hold their own binding.
# The first is the ``.stream()`` accumulator (run_agent / obs.oauth_llm hit it);
# the second is the non-streaming parse path.
_IMPORTER_MODULES = (
    "openai.lib.streaming.responses._responses",
    "openai.resources.responses.responses",
)

_GUARD_FLAG = "_hermes_codex_output_none_guard"


def _guard_output_none(parse_fn):
    """Wrap ``parse_response`` so a ``response.output is None`` snapshot is
    coerced to ``[]`` before the wrapped function iterates it."""

    @functools.wraps(parse_fn)
    def guarded_parse_response(*args, **kwargs):
        response = kwargs.get("response")
        if response is not None and getattr(response, "output", None) is None:
            try:
                response.output = []
            except Exception:  # pragma: no cover - defensive for frozen models
                try:
                    object.__setattr__(response, "output", [])
                except Exception:
                    pass
        return parse_fn(*args, **kwargs)

    setattr(guarded_parse_response, _GUARD_FLAG, True)
    return guarded_parse_response


def apply_codex_output_none_guard(*, force: bool = False) -> bool:
    """Idempotently install the ``output=None`` guard on every module binding
    ``parse_response``.

    Returns ``True`` if the guard was (re)applied, ``False`` if it was already
    present (and ``force`` is ``False``) or the openai SDK layout was not
    recognised. Safe to call repeatedly and from multiple import sites.
    """
    try:
        src = importlib.import_module(_SOURCE_MODULE)
    except Exception:  # pragma: no cover - openai is always present in prod
        logger.debug("codex output=None guard: %s not importable", _SOURCE_MODULE)
        return False

    current = getattr(src, "parse_response", None)
    if current is None:  # pragma: no cover - SDK layout changed upstream
        logger.debug(
            "codex output=None guard: parse_response missing from %s", _SOURCE_MODULE
        )
        return False

    if getattr(current, _GUARD_FLAG, False) and not force:
        return False

    # Wrap the underlying (un-guarded) function, never a guard-of-a-guard.
    base = getattr(current, "__wrapped__", current)
    guarded = _guard_output_none(base)

    # Rebind the source first so any importer loaded LATER picks up the guarded
    # version automatically via its own ``from ... import parse_response``.
    src.parse_response = guarded

    for name in _IMPORTER_MODULES:
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "parse_response"):
            mod.parse_response = guarded

    logger.info(
        "Installed openai Codex output=None guard on parse_response (force=%s)", force
    )
    return True
