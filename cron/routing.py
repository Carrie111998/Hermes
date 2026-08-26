"""Deterministic model/reasoning routing for durable cron jobs.

This module is deliberately independent from the UI and transport layers.  Job
creation records the deterministic policy decision; execution resolves that
record against the active profile's configured credentials and fails closed when
the selected route is not eligible.  No LLM is used for classification.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import logging
import os
import re
from typing import Any, Mapping

logger = logging.getLogger(__name__)

ROUTING_VERSION = 1
ROUTING_SLOTS = ("deterministic", "interpretation", "synthesis", "critical")
_DEFAULT_REASONING = {
    "deterministic": "none",
    "interpretation": "low",
    "synthesis": "medium",
    "critical": "high",
}


class CronRoutingError(RuntimeError):
    """A selected cron route cannot be executed safely."""

    def __init__(self, message: str, *, audit: Mapping[str, Any] | None = None) -> None:
        self.audit = dict(audit or {})
        super().__init__(message)


@dataclass(frozen=True)
class CronRoutingClassification:
    """Stable, JSON-friendly result of the cron complexity classifier."""

    slot: str
    score: int
    scores: dict[str, int]
    signals: dict[str, Any]
    reason: str


@dataclass(frozen=True)
class CronRoute:
    """Effective route selected for one cron fire."""

    slot: str
    model: str
    provider: str
    reasoning_effort: str | None
    base_url: str | None
    runtime: dict[str, Any]
    audit: dict[str, Any]


_RISK_RE = re.compile(
    r"\b(?:prod(?:uction)?|deploy|release|publish|payment|billing|credential|"
    r"secret|password|permission|access|rotate|delete|remove|refund|"
    r"financial|invoice|legal|external\s+send|"
    r"(?:api|access|auth(?:entication)?|bearer|oauth|refresh|service)\s+tokens?)\b",
    re.IGNORECASE,
)
# A risk word in an explicit prohibition is not an instruction to perform the
# risky action. Keep the check clause-local so a later positive action in the
# same prompt still wins (e.g. "do not deploy; deploy after approval").
_RISK_NEGATION_RE = re.compile(
    r"\b(?:no|not|never|without|don['’]?t|do\s+not|does\s+not|did\s+not|"
    r"cannot|can['’]?t|avoid|prevent|read[- ]only|sem|não|nao|nunca|"
    r"jamais|evite|evitar|somente\s+leitura|apenas\s+leitura)\b",
    re.IGNORECASE,
)
_MULTI_STEP_RE = re.compile(
    r"\b(?:then|after(?:wards)?|before|first|next|finally|compare|pipeline|"
    r"step\s*\d|one\s+by\s+one|in\s+order|plan)\b",
    re.IGNORECASE,
)
_SEMANTIC_OUTPUTS = frozenset(
    {"plan", "report", "digest", "summary", "synthesis", "recommendation", "draft"}
)
_INTERPRETATION_OUTPUTS = frozenset({"alert", "status", "classification", "decision"})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[str] = []
    for item in value:
        item_text = _text(item)
        if item_text and item_text not in result:
            result.append(item_text)
    return result


def _schedule_kind(schedule: Any) -> str:
    if isinstance(schedule, Mapping):
        return _text(schedule.get("kind")).lower()
    return ""


def _schedule_minutes(schedule: Any) -> int | None:
    if not isinstance(schedule, Mapping):
        return None
    try:
        value = int(schedule.get("minutes"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _has_risk_signal(text: str) -> bool:
    """Return whether a prompt contains a positive, actionable risk signal."""
    for match in _RISK_RE.finditer(text):
        # Stop at sentence/line boundaries. A negation in an earlier clause
        # must not suppress a later independent risky instruction.
        clause_start = max(
            text.rfind(delimiter, 0, match.start())
            for delimiter in (".", "!", "?", "\n", ";")
        ) + 1
        prefix = text[clause_start : match.start()]
        if _RISK_NEGATION_RE.search(prefix):
            continue
        return True
    return False


def classify_cron_job(
    *,
    prompt: str | None = None,
    skills: Any = None,
    enabled_toolsets: Any = None,
    schedule: Any = None,
    frequency: Any = None,
    risk: Any = None,
    output_type: str | None = None,
    context_from: Any = None,
    monitor_script: str | None = None,
    monitor_url: str | None = None,
    script: str | None = None,
    no_agent: bool = False,
    multiple_steps: Any = None,
) -> CronRoutingClassification:
    """Classify a job using only normalized, deterministic input features.

    The classifier is intentionally conservative: risk wins over all other
    signals, and context/multi-step semantic work wins over simple monitoring.
    ``no_agent`` is represented as a skipped classification by
    :func:`build_cron_routing_record`, so callers never spend routing work on a
    script-only job.
    """
    prompt_text = _text(prompt)
    skill_list = _string_list(skills)
    toolset_list = _string_list(enabled_toolsets)
    output = _text(output_type).lower()
    context_list = _string_list(context_from)
    schedule_kind = _schedule_kind(schedule)
    interval_minutes = _schedule_minutes(schedule)
    if interval_minutes is None:
        try:
            interval_minutes = int(frequency) if frequency is not None else None
        except (TypeError, ValueError):
            interval_minutes = None

    inferred_risk = bool(risk) if isinstance(risk, bool) else _has_risk_signal(prompt_text)
    inferred_multi_step = (
        bool(multiple_steps)
        if isinstance(multiple_steps, bool)
        else bool(_MULTI_STEP_RE.search(prompt_text) or ";" in prompt_text or "\n" in prompt_text)
    )
    monitor = bool(_text(monitor_script) or _text(monitor_url))
    high_frequency = bool(interval_minutes is not None and interval_minutes <= 15)
    multiple_skills = len(skill_list) > 1
    multiple_toolsets = len(toolset_list) > 2
    semantic_output = output in _SEMANTIC_OUTPUTS
    interpretation_output = output in _INTERPRETATION_OUTPUTS

    scores = {slot: 0 for slot in ROUTING_SLOTS}
    scores["deterministic"] += 1 if _text(script) and not prompt_text else 0
    scores["deterministic"] += 1 if output in {"fixed", "verbatim"} else 0
    scores["interpretation"] += 3 if monitor else 0
    scores["interpretation"] += 2 if interpretation_output else 0
    scores["interpretation"] += 1 if high_frequency else 0
    scores["synthesis"] += 4 if context_list else 0
    scores["synthesis"] += 3 if multiple_skills else 0
    scores["synthesis"] += 2 if multiple_toolsets else 0
    scores["synthesis"] += 3 if semantic_output else 0
    scores["synthesis"] += 2 if inferred_multi_step else 0
    scores["critical"] += 10 if inferred_risk else 0
    scores["critical"] += 2 if output == "action" else 0
    scores["critical"] += 1 if inferred_multi_step and inferred_risk else 0

    # Risk is an explicit safety override, not merely another weighted signal:
    # a production/credential/payment action must remain critical even when a
    # semantic prompt accumulates more synthesis signals. For non-risk jobs,
    # stable precedence is part of the audit contract; a tie cannot depend on
    # dict insertion order or a provider/model list returned by the network.
    precedence = {"critical": 4, "synthesis": 3, "interpretation": 2, "deterministic": 1}
    slot = (
        "critical"
        if inferred_risk
        else max(ROUTING_SLOTS, key=lambda candidate: (scores[candidate], precedence[candidate]))
    )
    if scores[slot] == 0:
        slot = "interpretation"
        scores[slot] = 1

    signals = {
        "risk": inferred_risk,
        "multiple_steps": inferred_multi_step,
        "multiple_skills": multiple_skills,
        "multiple_toolsets": multiple_toolsets,
        "context_from": bool(context_list),
        "monitor": monitor,
        "high_frequency": high_frequency,
        "schedule_kind": schedule_kind,
        "output_type": output or None,
        "script": bool(_text(script)),
    }
    reason_parts = [key for key, enabled in signals.items() if enabled is True]
    reason = ", ".join(reason_parts) if reason_parts else "baseline semantic task"
    return CronRoutingClassification(
        slot=slot,
        score=scores[slot],
        scores=scores,
        signals=signals,
        reason=reason,
    )


def _cron_config(config: Mapping[str, Any]) -> Mapping[str, Any]:
    cron = config.get("cron") if isinstance(config, Mapping) else None
    return cron if isinstance(cron, Mapping) else {}


def _model_config(config: Mapping[str, Any]) -> tuple[str, str, str | None]:
    model_cfg = config.get("model") if isinstance(config, Mapping) else None
    if isinstance(model_cfg, str):
        return _text(model_cfg), "", None
    if isinstance(model_cfg, Mapping):
        default = model_cfg.get("default") or model_cfg.get("model") or model_cfg.get("name")
        if isinstance(default, Mapping):
            default_model = default.get("model") or default.get("default") or default.get("name")
            default_provider = default.get("provider")
        else:
            default_model = default
            default_provider = None
        return (
            _text(default_model),
            _text(model_cfg.get("provider") or default_provider),
            _text(model_cfg.get("base_url")) or None,
        )
    return "", "", None


def _slot_policy(config: Mapping[str, Any], slot: str) -> Mapping[str, Any]:
    cron = _cron_config(config)
    routing = cron.get("routing") if isinstance(cron, Mapping) else None
    slots = routing.get("slots") if isinstance(routing, Mapping) else None
    if isinstance(slots, Mapping) and isinstance(slots.get(slot), Mapping):
        return slots[slot]
    return {}


def _load_active_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        loaded = load_config()
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _canonical_effort(value: Any, *, default: str | None = None) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    from hermes_constants import parse_reasoning_effort

    parsed = parse_reasoning_effort(value)
    if parsed is None:
        raise ValueError(f"Invalid cron routing reasoning_effort: {value!r}")
    if parsed.get("enabled") is False:
        return "none"
    return _text(parsed.get("effort")) or default


def _assignment_for_job(job: Mapping[str, Any], config: Mapping[str, Any], slot: str) -> dict[str, Any]:
    cron = _cron_config(config)
    policy = _slot_policy(config, slot)
    global_model, global_provider, global_base_url = _model_config(config)

    def _specific_provider(value: Any) -> str:
        provider = _text(value)
        return "" if provider.lower() in {"", "auto"} else provider

    requested_model = _text(job.get("model")) or _text(policy.get("model"))
    if not requested_model:
        requested_model = _text(job.get("model_snapshot"))
    if not requested_model:
        requested_model = _text(cron.get("model")) or global_model
    if not requested_model:
        requested_model = _text(os.getenv("HERMES_MODEL"))

    requested_provider = _specific_provider(job.get("provider")) or _specific_provider(policy.get("provider"))
    if not requested_provider:
        requested_provider = _specific_provider(cron.get("model_provider")) or _specific_provider(global_provider)
    if not requested_provider:
        requested_provider = _text(job.get("provider_snapshot"))

    base_url = _text(job.get("base_url")) or _text(policy.get("base_url"))
    if not base_url:
        base_url = _text(global_base_url)

    explicit_effort = job.get("reasoning_effort")
    effort = _canonical_effort(
        explicit_effort,
        default=_canonical_effort(
            policy.get("reasoning_effort"),
            default=_DEFAULT_REASONING[slot],
        ),
    )
    return {
        "model": requested_model,
        "provider": requested_provider,
        "base_url": base_url or None,
        "reasoning_effort": effort,
    }


def build_cron_routing_record(
    job: Mapping[str, Any],
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the persisted routing policy for a newly-created/updated job."""
    raw_slot = _text(job.get("routing_slot") or job.get("cron_slot"))
    if raw_slot and raw_slot.lower() not in ROUTING_SLOTS:
        raise ValueError(f"Unknown cron routing slot: {raw_slot!r}")
    if bool(job.get("no_agent")):
        return {
            "version": ROUTING_VERSION,
            "mode": "no_agent",
            "slot": None,
            "classification": {"skipped": True, "reason": "no_agent script job"},
            "overrides": {},
        }

    cfg = config if isinstance(config, Mapping) else _load_active_config()
    if raw_slot:
        slot = raw_slot.lower()
        classification = classify_cron_job(
            prompt=_text(job.get("prompt")),
            skills=job.get("skills") or job.get("skill"),
            enabled_toolsets=job.get("enabled_toolsets"),
            schedule=job.get("schedule"),
            context_from=job.get("context_from"),
            monitor_script=job.get("monitor_script"),
            monitor_url=job.get("monitor_url"),
            script=job.get("script"),
            no_agent=False,
        )
        classification_reason = f"explicit slot override ({slot}); classifier={classification.slot}"
    else:
        classification = classify_cron_job(
            prompt=_text(job.get("prompt")),
            skills=job.get("skills") or job.get("skill"),
            enabled_toolsets=job.get("enabled_toolsets"),
            schedule=job.get("schedule"),
            context_from=job.get("context_from"),
            monitor_script=job.get("monitor_script"),
            monitor_url=job.get("monitor_url"),
            script=job.get("script"),
            no_agent=False,
        )
        slot = classification.slot
        classification_reason = classification.reason

    assignment = _assignment_for_job(job, cfg, slot)
    explicit_overrides = {
        key: _text(job.get(key))
        for key in ("model", "provider", "base_url", "routing_slot", "cron_slot")
        if _text(job.get(key))
    }
    return {
        "version": ROUTING_VERSION,
        "mode": "agent",
        "slot": slot,
        "classification": {
            "skipped": False,
            **asdict(classification),
            "reason": classification_reason,
        },
        # These are the creation-time policy inputs. They keep a later global
        # chat-model switch from silently changing a durable job.
        "requested_model": assignment["model"] or None,
        "requested_provider": assignment["provider"] or None,
        "requested_base_url": assignment["base_url"],
        "reasoning_effort": assignment["reasoning_effort"],
        "overrides": explicit_overrides,
        "fallback_policy": "fail_closed",
    }


def _runtime_is_eligible(runtime: Mapping[str, Any]) -> tuple[bool, str]:
    if runtime.get("healthy") is False or runtime.get("available") is False:
        return False, "provider runtime reported unhealthy/unavailable"
    provider = _text(runtime.get("provider")).lower()
    if not provider:
        return False, "runtime did not identify a provider"
    # A normal provider must expose a resolved credential or a concrete local
    # endpoint. This rejects catalog-only entries and empty auth resolution.
    if not _text(runtime.get("api_key")) and not _text(runtime.get("base_url")):
        return False, "provider has neither a resolved credential nor an endpoint"

    # ``resolve_runtime_provider`` is the canonical credential/endpoint
    # resolver. A pool object returned in the runtime is already selected by
    # that resolver; probing a freshly-loaded pool here would mutate/prune
    # shared auth state and can disagree with the entry actually selected for
    # the call. Only inspect an explicitly supplied runtime pool.
    pool = runtime.get("credential_pool")
    if pool is not None:
        try:
            if pool.has_credentials() and not pool.has_available():
                return False, f"all credentials for provider {provider!r} are exhausted or unavailable"
        except Exception as exc:
            return False, f"credential availability could not be verified: {exc}"
    return True, "eligible"


def resolve_cron_route(
    job: Mapping[str, Any],
    config: Mapping[str, Any] | None = None,
) -> CronRoute:
    """Resolve one persisted policy against the active profile runtime.

    There is deliberately no fallback loop here.  The selected provider/model
    pair is atomic; an unavailable route raises :class:`CronRoutingError` and
    the scheduler must not construct an agent or spend tokens.
    """
    assert not bool(job.get("no_agent")), "no_agent jobs do not have an inference route"
    cfg = config if isinstance(config, Mapping) else _load_active_config()
    policy = job.get("routing")
    if not isinstance(policy, Mapping):
        policy = build_cron_routing_record(job, cfg)
    if _text(policy.get("mode")).lower() == "no_agent":
        raise AssertionError("no_agent jobs do not have an inference route")

    slot = _text(policy.get("slot")).lower()
    if slot not in ROUTING_SLOTS:
        raise CronRoutingError(
            f"Cron routing fail-closed: invalid persisted slot {slot!r}",
            audit={"status": "blocked", "slot": slot, "reason": "invalid_slot"},
        )
    assignment = {
        "model": _text(policy.get("requested_model")),
        "provider": _text(policy.get("requested_provider")),
        "base_url": _text(policy.get("requested_base_url")) or None,
        "reasoning_effort": policy.get("reasoning_effort"),
    }
    if not assignment["model"] or not assignment["provider"]:
        fallback_assignment = _assignment_for_job(job, cfg, slot)
        assignment = {
            key: assignment[key] or fallback_assignment[key]
            for key in assignment
        }

    requested_model = _text(assignment["model"])
    requested_provider = _text(assignment["provider"])
    audit = {
        "version": ROUTING_VERSION,
        "status": "blocked",
        "slot": slot,
        "requested_model": requested_model or None,
        "requested_provider": requested_provider or None,
        "reasoning_effort": assignment["reasoning_effort"],
        "fallback_policy": "fail_closed",
        "family_switch": False,
    }
    if not requested_model:
        raise CronRoutingError(
            "Cron routing fail-closed: no eligible model is configured "
            f"for slot {slot!r}; the job was not executed.",
            audit=audit | {"reason": "missing_model_assignment"},
        )

    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        provider_is_dynamic = not requested_provider or requested_provider.lower() == "auto"
        runtime_kwargs: dict[str, Any] = {
            # A missing provider means the profile has no explicit provider
            # assignment (for example a bare model config). Let the canonical
            # runtime resolver choose its configured/eligible default once,
            # without opening the legacy fallback chain. A concrete provider
            # remains an exact family pin.
            "requested": None if provider_is_dynamic else requested_provider,
            "target_model": requested_model,
        }
        if assignment["base_url"]:
            runtime_kwargs["explicit_base_url"] = assignment["base_url"]
        runtime = resolve_runtime_provider(**runtime_kwargs)
    except Exception as exc:
        raise CronRoutingError(
            "Cron routing fail-closed: selected model/provider is not eligible "
            f"for slot {slot!r}; no fallback or family switch was attempted: {exc}",
            audit=audit | {"reason": "runtime_resolution_failed"},
        ) from exc

    effective_provider = _text(runtime.get("provider")).lower()
    try:
        from hermes_cli.models import normalize_provider

        requested_provider_family = normalize_provider(requested_provider)
    except Exception:
        requested_provider_family = requested_provider.lower()
    # Named custom providers are addressed as ``custom:<name>`` but the
    # transport intentionally reports the generic ``custom`` provider. Keep
    # that canonical family equivalence while still pinning the actual
    # endpoint/credential selected by the resolver.
    if requested_provider_family.lower().startswith("custom:"):
        requested_provider_family = "custom"
    if requested_provider_family.lower() != "custom":
        try:
            from hermes_cli.auth import resolve_provider

            if resolve_provider(requested_provider) == "custom":
                requested_provider_family = "custom"
        except Exception:
            pass
    if not provider_is_dynamic and effective_provider != requested_provider_family.lower():
        raise CronRoutingError(
            "Cron routing fail-closed: runtime provider changed the selected "
            f"family from {requested_provider!r} to {effective_provider!r}; "
            "no silent family switch is allowed.",
            audit=audit | {
                "reason": "family_switch",
                "effective_provider": effective_provider or None,
            },
        )
    eligible, eligibility_reason = _runtime_is_eligible(runtime)
    if not eligible:
        raise CronRoutingError(
            "Cron routing fail-closed: selected model/provider is unavailable "
            f"for slot {slot!r} ({eligibility_reason}); no inference call was made.",
            audit=audit | {"reason": eligibility_reason},
        )

    runtime_model = _text(runtime.get("model"))
    if runtime_model and runtime_model != requested_model:
        raise CronRoutingError(
            "Cron routing fail-closed: runtime changed the selected model "
            f"from {requested_model!r} to {runtime_model!r}; "
            "no silent model switch is allowed.",
            audit=audit | {
                "reason": "model_switch",
                "effective_model": runtime_model,
                "effective_provider": effective_provider,
            },
        )

    effective_runtime = dict(runtime)
    effective_runtime["model"] = requested_model
    resolved_audit = audit | {
        "status": "resolved",
        "effective_model": requested_model,
        "effective_provider": effective_provider,
        "eligibility": eligibility_reason,
    }
    return CronRoute(
        slot=slot,
        model=requested_model,
        provider=effective_provider,
        reasoning_effort=_canonical_effort(
            assignment["reasoning_effort"],
            default=_DEFAULT_REASONING[slot],
        ),
        base_url=_text(runtime.get("base_url")) or assignment["base_url"],
        runtime=effective_runtime,
        audit=resolved_audit,
    )


__all__ = [
    "CronRoute",
    "CronRoutingClassification",
    "CronRoutingError",
    "ROUTING_SLOTS",
    "ROUTING_VERSION",
    "build_cron_routing_record",
    "classify_cron_job",
    "resolve_cron_route",
]
