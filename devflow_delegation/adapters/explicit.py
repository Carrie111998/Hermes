"""Explicit agent delegation — every profile may delegate through the shared
emitter. The emitter itself enforces evidence/acceptance/target; a model can
never mark its request auto-mergeable (Stage 3 gates ignore request-supplied
authority)."""
from devflow_delegation.emitter import DelegationResult

_PASSTHROUGH_KEYS = (
    "source", "kind", "title", "problem_statement", "evidence",
    "acceptance_criteria", "target", "severity", "priority", "confidence",
    "proposed_approach", "safety_notes", "idempotency_key", "mode",
)


def delegate_explicit(emitter, request: dict) -> DelegationResult:
    kwargs = {k: request[k] for k in _PASSTHROUGH_KEYS if k in request}
    if isinstance(kwargs.get("safety_notes"), (list, tuple)):
        kwargs["safety_notes"] = tuple(kwargs["safety_notes"])
    return emitter.delegate(**kwargs)
