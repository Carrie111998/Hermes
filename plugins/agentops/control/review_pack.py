"""Executable validation for the read-only runtime review pack."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from plugins.agentops.control.observer_models import TargetKind


class ManifestValidationError(ValueError):
    pass


@dataclass(frozen=True)
class CollectorSpec:
    id: str
    entry: str
    capabilities: tuple[str, ...]
    target_kinds: tuple[str, ...]
    max_bytes: int
    max_items: int
    deadline_seconds: float
    rate_limit_seconds: float


@dataclass(frozen=True)
class AssertionSpec:
    id: str
    severity: str
    mandatory: bool


@dataclass(frozen=True)
class ReviewPack:
    pack_id: str
    version: str
    authority_mode: str
    collectors: dict[str, CollectorSpec]
    assertions: dict[str, AssertionSpec]
    target_kinds: tuple[str, ...]
    retention_days: int

    @property
    def mandatory_assertion_ids(self) -> frozenset[str]:
        return frozenset(item.id for item in self.assertions.values() if item.mandatory)

    def validate_collector(self, collector_id: str, target_kind: TargetKind | str) -> CollectorSpec:
        kind = target_kind.value if isinstance(target_kind, TargetKind) else str(target_kind)
        spec = self.collectors.get(collector_id)
        if spec is None or kind not in spec.target_kinds:
            raise ManifestValidationError("collector target binding rejected")
        return spec


def _entry_exists(entry: str) -> None:
    if not isinstance(entry, str) or entry.count(":") != 1:
        raise ManifestValidationError("collector entry invalid")
    module, symbol = entry.split(":", 1)
    if not module.startswith("plugins.agentops.") or not symbol.isidentifier():
        raise ManifestValidationError("collector entry invalid")
    try:
        if not hasattr(importlib.import_module(module), symbol):
            raise ManifestValidationError("collector entry missing")
    except (ImportError, AttributeError) as exc:
        raise ManifestValidationError("collector entry missing") from exc


def load_review_pack(path: Path | None = None) -> ReviewPack:
    manifest = Path(path) if path else Path(__file__).resolve().parents[1] / "review_packs/runtime_core/manifest.yaml"
    try:
        data: dict[str, Any] = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ManifestValidationError("manifest unreadable") from exc
    if data.get("schema_version") != 2 or data.get("authority_mode") != "observe_only":
        raise ManifestValidationError("manifest authority/schema rejected")
    execution = data.get("execution", {})
    if execution.get("no_write") is not True or execution.get("action_execution") != "disabled":
        raise ManifestValidationError("manifest execution is not read-only")
    pack = data.get("pack", {})
    inputs = data.get("inputs", {})
    retention = inputs.get("retention_days")
    kinds = tuple(str(item) for item in data.get("target_kinds", ()))
    if not pack.get("id") or not pack.get("version") or not kinds or not isinstance(retention, int) or retention <= 0:
        raise ManifestValidationError("manifest metadata incomplete")
    collectors: dict[str, CollectorSpec] = {}
    for item in inputs.get("collectors", ()):
        try:
            spec = CollectorSpec(
                id=str(item["id"]), entry=str(item["entry"]),
                capabilities=tuple(str(value) for value in item["capabilities"]),
                target_kinds=tuple(str(value) for value in item["target_kinds"]),
                max_bytes=int(item.get("max_bytes", 0)), max_items=int(item.get("max_items", 0)),
                deadline_seconds=float(item["deadline_seconds"]), rate_limit_seconds=float(item["rate_limit_seconds"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ManifestValidationError("collector budget incomplete") from exc
        if spec.id in collectors or not spec.capabilities or not set(spec.target_kinds).issubset(set(kinds)):
            raise ManifestValidationError("collector declaration invalid")
        if spec.max_bytes < 0 or spec.max_bytes > 8 * 1024 * 1024 or spec.max_items < 0 or spec.max_items > 10000 or not (0 < spec.deadline_seconds <= 30) or spec.rate_limit_seconds < 0:
            raise ManifestValidationError("collector budget out of range")
        _entry_exists(spec.entry)
        collectors[spec.id] = spec
    assertions: dict[str, AssertionSpec] = {}
    for item in data.get("assertions", ()):
        assertion = AssertionSpec(str(item["id"]), str(item["severity"]), bool(item["mandatory"]))
        if assertion.id in assertions:
            raise ManifestValidationError("duplicate assertion")
        assertions[assertion.id] = assertion
    if not assertions:
        raise ManifestValidationError("manifest assertions missing")
    return ReviewPack(str(pack["id"]), str(pack["version"]), "observe_only", collectors, assertions, kinds, retention)
