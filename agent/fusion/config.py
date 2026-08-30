"""Fusion v2 configuration, rosters, and model-pool resolution."""

from __future__ import annotations

import re
from typing import Any

from .models import FusionParticipantSpec, FusionRequest

EQUAL_PEER_ROLE = "Equal peer participant"
EQUAL_PEER_FOCUS = (
    "full-task analysis, repo evidence, risks, implementation sequence, test strategy, "
    "and convergence questions"
)

BUILTIN_ROSTERS: dict[str, list[dict[str, str]]] = {
    # Built-in rosters are deliberately neutral: model diversity creates the
    # different perspectives, not assigned human-style roles such as architect,
    # critic, skeptic, or tester. Custom rosters may still supply labels, but
    # the system prompt keeps every participant equal in status and rights.
    "planning": [
        {"slug": "participant", "role": EQUAL_PEER_ROLE, "focus": EQUAL_PEER_FOCUS},
        {"slug": "participant", "role": EQUAL_PEER_ROLE, "focus": EQUAL_PEER_FOCUS},
        {"slug": "participant", "role": EQUAL_PEER_ROLE, "focus": EQUAL_PEER_FOCUS},
    ],
    "review": [
        {"slug": "participant", "role": EQUAL_PEER_ROLE, "focus": EQUAL_PEER_FOCUS},
        {"slug": "participant", "role": EQUAL_PEER_ROLE, "focus": EQUAL_PEER_FOCUS},
        {"slug": "participant", "role": EQUAL_PEER_ROLE, "focus": EQUAL_PEER_FOCUS},
    ],
    "mixed": [
        {"slug": "participant", "role": EQUAL_PEER_ROLE, "focus": EQUAL_PEER_FOCUS},
        {"slug": "participant", "role": EQUAL_PEER_ROLE, "focus": EQUAL_PEER_FOCUS},
        {"slug": "participant", "role": EQUAL_PEER_ROLE, "focus": EQUAL_PEER_FOCUS},
    ],
    "fast": [
        {"slug": "participant", "role": EQUAL_PEER_ROLE, "focus": EQUAL_PEER_FOCUS},
        {"slug": "participant", "role": EQUAL_PEER_ROLE, "focus": EQUAL_PEER_FOCUS},
    ],
}

DEFAULT_MODEL_POOL: list[dict[str, str]] = [
    {
        "slug": "glm-max",
        "provider": "zai",
        "model": "glm-5.2",
        "reasoning_effort": "xhigh",
    },
    {
        "slug": "deepseek-pro-max",
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "reasoning_effort": "xhigh",
    },
    {
        "slug": "codex-default",
        "provider": "openai-codex",
        "model": "gpt-5.5",
        "api_mode": "codex_responses",
        "reasoning_effort": "xhigh",
    },
]

DEFAULT_FUSION_CONFIG: dict[str, Any] = {
    "default_roster": "planning",
    "rosters": {},
    "model_pool": DEFAULT_MODEL_POOL,
    "timeout_seconds": 300,
    "min_successful_participants": 2,
    "participants": 3,
    "allow_single_participant": False,
    "min_distinct_models": 2,
    "require_heterogeneous_models": True,
    "allow_homogeneous_models": False,
    "debate_rounds": 5,
    "convergence_rounds": 5,
    "reasoning_effort": "xhigh",
    "spike_worktrees": True,
}


class ModelDiversityError(ValueError):
    """Raised before execution when Fusion cannot build a heterogeneous roster."""

    status = "model_diversity_error"


def load_fusion_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config()
        except Exception:
            config = {}
    raw = config.get("fusion", {}) if isinstance(config, dict) else {}
    merged = dict(DEFAULT_FUSION_CONFIG)
    if isinstance(raw, dict):
        merged.update(raw)
    merged["rosters"] = {**BUILTIN_ROSTERS, **(merged.get("rosters") or {})}
    if "model_pool" not in merged or merged.get("model_pool") is None:
        merged["model_pool"] = list(DEFAULT_MODEL_POOL)
    return merged


def _coerce_int(value: Any, default: int, *, floor: int, ceiling: int | None = None) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    n = max(floor, n)
    if ceiling is not None:
        n = min(ceiling, n)
    return n


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _normalize_reasoning(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def normalize_request(request: FusionRequest, config: dict[str, Any] | None = None) -> FusionRequest:
    cfg = load_fusion_config(config)
    roster = request.roster or str(cfg.get("default_roster") or "planning")
    participants = _coerce_int(
        request.participants or cfg.get("participants"),
        int(cfg.get("participants") or 3),
        floor=1,
        ceiling=8,
    )
    timeout = _coerce_int(
        request.timeout_seconds or cfg.get("timeout_seconds"),
        int(cfg.get("timeout_seconds") or 300),
        floor=30,
    )
    min_success = _coerce_int(
        request.min_successful_participants or cfg.get("min_successful_participants"),
        int(cfg.get("min_successful_participants") or 2),
        floor=1,
        ceiling=participants,
    )
    min_distinct = _coerce_int(
        request.min_distinct_models or cfg.get("min_distinct_models"),
        int(cfg.get("min_distinct_models") or 2),
        floor=1,
        ceiling=participants,
    )
    debate_rounds = _coerce_int(request.debate_rounds or cfg.get("debate_rounds"), int(cfg.get("debate_rounds") or 1), floor=0, ceiling=5)
    convergence_rounds = _coerce_int(request.convergence_rounds or cfg.get("convergence_rounds"), int(cfg.get("convergence_rounds") or 2), floor=1, ceiling=5)
    return FusionRequest(
        mode=(request.mode or "plan").strip().lower(),
        task=request.task.strip(),
        participants=participants,
        roster=roster,
        timeout_seconds=timeout,
        repo_path=request.repo_path,
        output_root=request.output_root,
        min_successful_participants=min_success,
        allow_single_participant=bool(request.allow_single_participant or cfg.get("allow_single_participant", False)),
        model_specs=list(request.model_specs or []),
        min_distinct_models=min_distinct,
        allow_homogeneous_models=bool(request.allow_homogeneous_models or cfg.get("allow_homogeneous_models", False)),
        debate_rounds=debate_rounds,
        convergence_rounds=convergence_rounds,
        reasoning_effort=_normalize_reasoning(request.reasoning_effort) or _normalize_reasoning(cfg.get("reasoning_effort")),
        spike_worktrees=bool(request.spike_worktrees and _coerce_bool(cfg.get("spike_worktrees", True))),
    )


def _safe_slug(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_.-]+", "-", (text or "").strip().lower()).strip("-._")
    return text or "model"


def parse_model_spec(value: str) -> dict[str, str]:
    """Parse compact CLI model specs: provider:model[@reasoning]."""
    raw = (value or "").strip()
    if not raw:
        raise ValueError("empty model spec")
    reasoning = ""
    if "@" in raw:
        raw, reasoning = raw.rsplit("@", 1)
    if ":" not in raw:
        raise ValueError(f"Fusion model spec must be provider:model, got: {value}")
    provider, model = raw.split(":", 1)
    provider = provider.strip()
    model = model.strip()
    if not provider or not model:
        raise ValueError(f"Fusion model spec must include provider and model, got: {value}")
    return {
        "slug": _safe_slug(f"{provider}-{model}"),
        "provider": provider,
        "model": model,
        "reasoning_effort": reasoning.strip().lower() if reasoning.strip() else "",
    }


def _normalize_model_pool(raw_pool: Any, request: FusionRequest) -> list[dict[str, str]]:
    source = request.model_specs if request.model_specs else raw_pool
    pool: list[dict[str, str]] = []
    if not isinstance(source, list):
        return pool
    for idx, raw in enumerate(source):
        if isinstance(raw, str):
            entry = parse_model_spec(raw)
        elif isinstance(raw, dict):
            provider = str(raw.get("provider") or "").strip()
            model = str(raw.get("model") or "").strip()
            if not provider or not model:
                continue
            entry = {
                "slug": str(raw.get("slug") or _safe_slug(f"{provider}-{model}")),
                "provider": provider,
                "model": model,
                "api_mode": str(raw.get("api_mode") or "").strip(),
                "reasoning_effort": str(raw.get("reasoning_effort") or raw.get("reasoning") or "").strip().lower(),
            }
        else:
            continue
        entry.setdefault("slug", f"model-{idx + 1}")
        pool.append(entry)
    return pool


def _runtime_pair(entry: dict[str, str]) -> str:
    return f"{entry.get('provider', '').strip().lower()}:{entry.get('model', '').strip().lower()}"


def model_diversity_summary(specs: list[FusionParticipantSpec], required: int | None = None) -> dict[str, Any]:
    pairs = [spec.runtime_label for spec in specs]
    distinct = sorted({pair.lower() for pair in pairs if not pair.endswith(":inherit") and not pair.startswith("inherit:")})
    return {
        "required_distinct_models": required,
        "distinct_count": len(distinct),
        "distinct_provider_models": distinct,
        "participants": [
            {
                "slug": spec.slug,
                "role": spec.role,
                "provider": spec.provider,
                "model": spec.model,
                "api_mode": spec.api_mode,
                "reasoning_effort": spec.reasoning_effort,
                "runtime_label": spec.runtime_label,
            }
            for spec in specs
        ],
    }


def participant_specs_for_request(
    request: FusionRequest,
    config: dict[str, Any] | None = None,
) -> list[FusionParticipantSpec]:
    cfg = load_fusion_config(config)
    roster_defs = cfg.get("rosters", {}).get(request.roster)
    if not isinstance(roster_defs, list) or not roster_defs:
        raise ValueError(f"Unknown or empty Fusion roster: {request.roster}")

    model_pool = _normalize_model_pool(cfg.get("model_pool"), request)
    allow_homogeneous = bool(request.allow_homogeneous_models or _coerce_bool(cfg.get("allow_homogeneous_models", False)))
    require_heterogeneous = _coerce_bool(cfg.get("require_heterogeneous_models", True)) and not allow_homogeneous
    required_distinct = 1 if request.participants <= 1 else max(2, request.min_distinct_models)
    required_distinct = min(required_distinct, request.participants)

    if require_heterogeneous and len({_runtime_pair(e) for e in model_pool}) < required_distinct:
        configured = ", ".join(_runtime_pair(e) for e in model_pool) or "none"
        raise ModelDiversityError(
            "Fusion requires at least "
            f"{required_distinct} distinct provider:model pairs before execution; configured: {configured}. "
            "Set fusion.model_pool or pass --models provider:model,...; use --allow-homogeneous only for debugging."
        )

    specs: list[FusionParticipantSpec] = []
    used_slugs: set[str] = set()
    generic_slugs = {"participant", "peer", "fusion-peer", "model"}
    for idx in range(request.participants):
        raw = roster_defs[idx % len(roster_defs)]
        if not isinstance(raw, dict):
            raise ValueError(f"Invalid participant spec at roster index {idx % len(roster_defs)}")
        model_entry = model_pool[idx % len(model_pool)] if model_pool else {}
        raw_slug = str(raw.get("slug") or raw.get("name") or "participant").strip().lower()
        model_slug = str(model_entry.get("slug") or "").strip() or None
        if model_slug and raw_slug in generic_slugs:
            base_slug = model_slug
        else:
            base_slug = raw_slug or f"participant-{idx + 1}"
        slug = base_slug if idx < len(roster_defs) else f"{base_slug}-{idx + 1}"
        if slug in used_slugs:
            slug = f"{slug}-{idx + 1}"
        used_slugs.add(slug)
        specs.append(
            FusionParticipantSpec(
                slug=slug.replace(" ", "-"),
                role=str(raw.get("role") or slug),
                focus=str(raw.get("focus") or "general review"),
                model=model_entry.get("model") or raw.get("model"),
                provider=model_entry.get("provider") or raw.get("provider"),
                api_mode=model_entry.get("api_mode") or raw.get("api_mode"),
                reasoning_effort=model_entry.get("reasoning_effort") or raw.get("reasoning_effort") or request.reasoning_effort,
                model_slug=model_slug,
                requested_provider=model_entry.get("provider") or raw.get("provider"),
                requested_model=model_entry.get("model") or raw.get("model"),
            )
        )

    if require_heterogeneous:
        summary = model_diversity_summary(specs, required_distinct)
        if int(summary["distinct_count"]) < required_distinct:
            raise ModelDiversityError(
                "Fusion participant roster resolved to "
                f"{summary['distinct_count']} distinct provider:model pairs; required {required_distinct}. "
                "Adjust fusion.model_pool or --models."
            )
    return specs
