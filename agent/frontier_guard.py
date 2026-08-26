"""Fail-loud requested-vs-served model check for frontier-required calls.

Implements Layer A of the HF-04 remediation (silent frontier downgrade): when a
caller explicitly marks a call ``frontier_required``, compare the model class it
asked for against the model class that actually served the turn, and surface a
caller-visible warning when a frontier request was served by a non-frontier
model.

Design constraints this module encodes (from the approved IGN-192 design doc
``hf04-a-design`` rev 1):

* **Caller-supplied, per-call.** ``frontier_required`` is an explicit flag on the
  call, never inferred from task content. Inference would make correctness depend
  on a second, separately-fallible classifier — the exact silent-failure surface
  HF-04 already burned us on. Default is ``False``: opt-in, so no existing caller
  changes behaviour.
* **Non-blocking.** A downgrade produces a warning in the turn result, not an
  exception. A hard-block mode is a separate, later decision.
* **One shared feature flag**, read per-call so disabling takes effect on the very
  next call with no restart. Both the API-server call path (IGN-196) and the
  CLI/cron call path (IGN-197) read this same flag — do not add a second one.
* **Fail loud, not silent.** If the check is switched on but the classifier from
  IGN-195 cannot be imported, that is reported (once) rather than quietly
  degrading into a no-op, because a no-op check is indistinguishable from a
  clean result.

Rollback: set ``HERMES_FRONTIER_DOWNGRADE_CHECK`` to a falsey value (or unset
it). ``git revert`` of the implementing commit is the documented backstop if the
flag mechanism itself misbehaves.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Shared feature-flag env var. Read per-call, never cached at process start.
FRONTIER_DOWNGRADE_CHECK_ENV = "HERMES_FRONTIER_DOWNGRADE_CHECK"

_TRUTHY = {"1", "true", "yes", "on"}

#: Candidate homes for the shared classifier delivered by IGN-195 (A1). A1 owns
#: the final location; this list is tried in order so A3 keeps working wherever
#: A1 lands it. Preferred/first entry is the one proposed to A1.
_MODEL_CLASS_IMPORT_CANDIDATES = (
    ("agent.model_classification", "model_class"),
    ("agent.model_class", "model_class"),
    ("hermes_cli.model_classification", "model_class"),
    ("model_classification", "model_class"),
)

# Module-level latch so a missing classifier is reported once per process
# instead of once per turn.
_classifier_missing_reported = False


def frontier_downgrade_check_enabled() -> bool:
    """Return whether the shared frontier-downgrade check is switched on.

    Reads the environment on every call (never cached) so that flipping the flag
    off takes effect on the next call without a restart.
    """
    return (os.environ.get(FRONTIER_DOWNGRADE_CHECK_ENV) or "").strip().lower() in _TRUTHY


def _resolve_model_class():
    """Return IGN-195's ``model_class`` callable, or ``None`` if not present yet."""
    for module_name, attr in _MODEL_CLASS_IMPORT_CANDIDATES:
        try:
            module = __import__(module_name, fromlist=[attr])
        except Exception:
            continue
        fn = getattr(module, attr, None)
        if callable(fn):
            return fn
    return None


def classify_model(model: Any) -> Optional[str]:
    """Classify ``model`` via the shared classifier. ``None`` when unavailable."""
    if not isinstance(model, str) or not model.strip():
        return None
    fn = _resolve_model_class()
    if fn is None:
        return None
    try:
        result = fn(model.strip())
    except Exception:
        logger.warning(
            "frontier downgrade check: model_class(%r) raised; treating class as unknown",
            model,
            exc_info=True,
        )
        return None
    return result if isinstance(result, str) and result else None


def build_frontier_downgrade_warning(
    requested_model: Any,
    served_model: Any,
    *,
    frontier_required: bool = False,
) -> Optional[dict]:
    """Return a caller-visible warning dict, or ``None`` when there is nothing to say.

    Returns ``None`` — meaning "no warning" — when the call was not marked
    ``frontier_required``, when the flag is off, or when a frontier request was
    genuinely served by a frontier model.

    Returns a ``frontier_check_unavailable`` warning when the check is switched
    on for a frontier-required call but the shared classifier is missing. That is
    deliberate: a silently skipped check looks exactly like a passing one.
    """
    global _classifier_missing_reported

    if not frontier_required or not frontier_downgrade_check_enabled():
        return None

    fn = _resolve_model_class()
    if fn is None:
        if not _classifier_missing_reported:
            _classifier_missing_reported = True
            logger.warning(
                "%s is enabled but the shared model classifier (IGN-195) is not "
                "importable from any of %s — frontier-required calls cannot be "
                "checked for downgrade",
                FRONTIER_DOWNGRADE_CHECK_ENV,
                [name for name, _ in _MODEL_CLASS_IMPORT_CANDIDATES],
            )
        return {
            "type": "frontier_check_unavailable",
            "requested_model": requested_model,
            "served_model": served_model,
            "detail": "shared model classifier is not importable",
        }

    requested_class = classify_model(requested_model)
    served_class = classify_model(served_model)
    if requested_class is None:
        # The classifier exists but could not produce a verdict (it raised, or
        # the requested model is not a usable string). Same reasoning as the
        # missing-classifier branch: report it rather than let an unrun check
        # look like a passing one.
        return {
            "type": "frontier_check_unavailable",
            "requested_model": requested_model,
            "served_model": served_model,
            "detail": "requested model could not be classified",
        }
    if requested_class != "frontier":
        # Nothing was promised. A non-frontier (or unclassifiable) request cannot
        # be downgraded by definition.
        return None
    if served_class == "frontier":
        return None

    return {
        "type": "frontier_downgrade",
        "requested_model": requested_model,
        "served_model": served_model,
        "requested_class": requested_class,
        "served_class": served_class,
    }


def check_frontier_downgrade(
    result: dict,
    *,
    requested_model: Any,
    served_model: Any,
    frontier_required: bool = False,
) -> Optional[dict]:
    """Append a frontier-downgrade warning to ``result['warnings']`` if warranted.

    Non-blocking by contract: this never raises for a downgrade, and any internal
    failure is swallowed so the guard can never break a turn that would otherwise
    have succeeded. Returns the warning that was appended, or ``None``.
    """
    try:
        warning = build_frontier_downgrade_warning(
            requested_model,
            served_model,
            frontier_required=frontier_required,
        )
    except Exception:
        logger.warning("frontier downgrade check failed", exc_info=True)
        return None
    if warning is None:
        return None

    warnings = result.get("warnings")
    if not isinstance(warnings, list):
        warnings = []
        result["warnings"] = warnings
    warnings.append(warning)

    # Also log it: on the cron path there is no human reading the result dict,
    # and the session log is the transcript.
    #
    # The two warning types are distinct findings and must not share a message.
    # "Guarantee not held" is a claim that a frontier request was demonstrably
    # served by a non-frontier model. On the frontier_check_unavailable path we
    # know nothing of the kind — the check never ran, so the guarantee is
    # *unverified*, not violated. Logging the stronger sentence for the weaker
    # finding would send anyone reading the transcript hunting a downgrade that
    # may not have happened, and it hides the thing that actually needs fixing:
    # the check itself is broken.
    if warning.get("type") == "frontier_downgrade":
        logger.warning(
            "Frontier guarantee not held: requested %r (%s) but %r (%s) served this turn",
            warning.get("requested_model"),
            warning.get("requested_class") or "unknown",
            warning.get("served_model"),
            warning.get("served_class") or "unknown",
        )
    else:
        logger.warning(
            "Frontier guarantee unverified for this turn (%s): requested %r, "
            "served %r — the downgrade check could not run, so this is neither "
            "a pass nor a detected downgrade",
            warning.get("detail") or warning.get("type"),
            warning.get("requested_model"),
            warning.get("served_model"),
        )
    return warning


__all__ = [
    "FRONTIER_DOWNGRADE_CHECK_ENV",
    "frontier_downgrade_check_enabled",
    "classify_model",
    "build_frontier_downgrade_warning",
    "check_frontier_downgrade",
]
