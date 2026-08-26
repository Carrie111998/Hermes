"""Fail-closed in-process model routing for Kanban dispatch.

This module deliberately has no subprocess or model-catalog dependency: the
persisted config is the policy authority and every route is reproducible from
its snapshot.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
from typing import Any, Mapping

import yaml


class PolicyError(ValueError):
    """The configured routing policy cannot approve a requested action."""


@dataclass(frozen=True)
class ModelRoutingPolicy:
    enabled: bool
    tiers: Mapping[str, tuple[dict[str, str], ...]]
    classification: Mapping[str, Mapping[str, Any]]

    @classmethod
    def load(cls, config_path: Path | None = None) -> "ModelRoutingPolicy":
        path = config_path or Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "config.yaml"
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise PolicyError(f"model_routing config unavailable: {exc}") from exc
        value = raw.get("model_routing")
        if not isinstance(value, dict) or value.get("enabled") is not True:
            raise PolicyError("model_routing is not enabled")
        raw_tiers = value.get("tiers")
        if not isinstance(raw_tiers, dict):
            raise PolicyError("model_routing.tiers is required")
        tiers: dict[str, tuple[dict[str, str], ...]] = {}
        for tier, candidates in raw_tiers.items():
            name = str(tier).strip().upper()
            if not name or not isinstance(candidates, list):
                raise PolicyError(f"invalid candidates for tier {tier!r}")
            approved: list[dict[str, str]] = []
            for item in candidates:
                if not isinstance(item, dict):
                    continue
                provider = str(item.get("provider") or "").strip()
                model = str(item.get("model") or "").strip()
                if provider and model:
                    approved.append({"provider": provider, "model": model})
            tiers[name] = tuple(approved)
        raw_classification = value.get("classification", {})
        if not isinstance(raw_classification, dict):
            raise PolicyError("model_routing.classification must be a mapping")
        classification = {
            str(priority).strip().upper(): spec
            for priority, spec in raw_classification.items()
            if isinstance(spec, dict)
        }
        return cls(True, tiers, classification)

    def classify(self, *, priority: str | None = None, tags: list[str] | None = None) -> str:
        explicit = (priority or "").strip().upper()
        if explicit:
            if explicit not in {"P0", "P1", "P2", "P3", "P4"}:
                raise PolicyError(f"unknown routing priority {priority!r}")
            return explicit
        tag_set = {str(tag).strip().casefold() for tag in (tags or [])}
        for candidate, spec in self.classification.items():
            configured = {str(tag).strip().casefold() for tag in spec.get("tags", [])}
            if configured and configured & tag_set:
                return candidate
        return "P1"

    def resolve(self, *, priority: str | None = None, tags: list[str] | None = None) -> dict[str, str]:
        resolved_priority = self.classify(priority=priority, tags=tags)
        if resolved_priority == "P0":
            return {"priority": "P0", "tier": "deterministic", "provider": "", "model": ""}
        spec = self.classification.get(resolved_priority)
        if not spec:
            raise PolicyError(f"no routing rule for {resolved_priority}")
        tier = str(spec.get("tier") or "").strip().upper()
        candidates = self.tiers.get(tier, ())
        if not candidates:
            raise PolicyError(f"no approved model resource for {resolved_priority}/{tier or 'unset'}")
        candidate = candidates[0]
        return {"priority": resolved_priority, "tier": tier, **candidate}

    def validate_override(self, provider: str | None, model: str | None) -> dict[str, str]:
        provider = (provider or "").strip()
        model = (model or "").strip()
        if not provider or not model:
            raise PolicyError("override requires provider and model")
        for tier, candidates in self.tiers.items():
            if any(c["provider"] == provider and c["model"] == model for c in candidates):
                return {"tier": tier, "provider": provider, "model": model}
        raise PolicyError(f"override {provider}/{model} is not an approved candidate")


def policy_for_task(task: Any) -> tuple[ModelRoutingPolicy, dict[str, str]]:
    policy = ModelRoutingPolicy.load()
    tags = list(getattr(task, "skills", None) or [])
    route = policy.resolve(priority=getattr(task, "routing_priority", None), tags=tags)
    return policy, route
