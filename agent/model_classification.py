"""
Shared frontier/cheap model classification (IGN-195, PER2-HF-04-A1).

Single source of truth for ``model_class()`` inside the live hermes-agent tree.
Both PER2-HF-04-A call-site children import from here:

    from agent.model_classification import model_class

Membership is mirrored verbatim from the PER2-MOD-01 offline evaluator
(``per2-mod-01-routing-policy-20260822/evaluator/evaluate.py``,
``FRONTIER_MODELS``/``CHEAP_MODELS``, lines ~39-47 as of 2026-08-22). It is
copied rather than imported in either direction: the evaluator carries its own
no-network / no-live-import guarantee in its docstring, so it must not import
from this tree, and this module must not depend on a sandbox path that does not
exist on other machines. ``tests/agent/test_model_classification.py`` contains a
parity test that compares the two sets whenever the sandbox copy is reachable,
so hand-edited drift is caught rather than assumed away.

Return values are the evaluator's exact literals — ``'frontier'``,
``'benchmarked_cheap'``, ``'unknown'`` — deliberately, so that one source of
truth means one vocabulary too. Callers should test for ``== "frontier"`` and
treat everything else as not-frontier; do not compare against the bare string
``"cheap"``.

Classification is explicit membership, never inferred from the shape of a model
name: guessing a tier from a name is how silent downgrades survive review.
"""

from __future__ import annotations

FRONTIER_MODELS = {
    "gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra",
    "claude-opus-5", "claude-opus-4-8", "claude-sonnet-5",
}
CHEAP_MODELS = {
    "deepseek-v4-flash", "deepseek-v4-pro", "glm-5.2",
    "claude-haiku-4-5-20251001", "step-3.7-flash", "hy3",
    "gemini-3.6-flash", "gemini-3.7-flash",
}


def model_class(name: str | None) -> str:
    """Classify a model identifier as 'frontier', 'benchmarked_cheap', or 'unknown'.

    Accepts bare model names or provider-qualified strings
    (e.g. ``"anthropic/claude-opus-5"``); only the segment after the last ``"/"``
    is matched. ``None``, empty, and unrecognised names all return ``'unknown'``,
    which callers must treat as "not verified frontier" rather than as a pass.
    """
    if not name:
        return "unknown"
    bare = str(name).split("/")[-1].strip()
    if bare in FRONTIER_MODELS:
        return "frontier"
    if bare in CHEAP_MODELS:
        return "benchmarked_cheap"
    return "unknown"
