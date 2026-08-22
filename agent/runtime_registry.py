"""Read-only, fail-closed loader for Hermes runtime registries.

The loader deliberately has no write or promotion side effects.  A returned
:class:`RegistrySnapshot` owns a frozen copy of every parsed payload, so later
changes on disk cannot change an agent/session's control-plane inputs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from hermes_constants import get_hermes_home

SUPPORTED_MANIFEST_SCHEMA_VERSION = "hermes-workflow-registry/1.0"
SUPPORTED_PROMOTION_STATES = frozenset(
    {"DRAFT", "READY_FOR_REVIEW", "APPROVED", "PUBLISHED"}
)
PRODUCTION_PROMOTION_STATES = frozenset({"APPROVED", "PUBLISHED"})
DEFAULT_REGISTRY_RELATIVE_PATH = Path("workflows") / "production-registry"
EXEMPT_FILES = frozenset({"README.md"})

# These are the behavior payloads in the registry contract.  The values are
# the required top-level section in each JSON payload.  ``schema_version`` is
# checked separately for every JSON payload.
_REQUIRED_PAYLOAD_SECTIONS = {
    "route-policy.json": "default_route",
    "workflow-templates.json": "templates",
    "execution-roles.json": "roles",
    "model-policies.json": "policies",
    "model-profiles.json": "profiles",
    "capability-contracts.json": "contracts",
}
_REQUIRED_PAYLOAD_FILES = frozenset(
    {*_REQUIRED_PAYLOAD_SECTIONS, "semantic-router-prompt.md"}
)
_REGISTRY_VERSION_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\.(?P<revision>\d+)$")
_PAYLOAD_SCHEMA_RE = re.compile(r"^\d+\.\d+$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SUPPORTED_PAYLOAD_SCHEMA_VERSIONS = {
    "route-policy.json": "1.0",
    "workflow-templates.json": "1.1",
    "execution-roles.json": "1.0",
    "model-policies.json": "1.2",
    "model-profiles.json": "1.1",
    "capability-contracts.json": "1.0",
}


class RegistryLoadError(ValueError):
    """Raised whenever a registry cannot be loaded without guessing.

    ``code`` and ``path`` are stable machine-readable fields for a future CLI;
    the message intentionally contains only registry-relative paths and safe
    validation metadata.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_registry",
        path: str | None = None,
    ) -> None:
        self.code = code
        self.path = path
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class BaselineTestResult:
    """One deterministic baseline check result.

    Baseline runners can supply these results to a promotion gate without
    coupling the registry loader to a model/provider benchmark implementation.
    """

    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class BaselineReport:
    """Immutable aggregate returned by :func:`run_baseline_tests`."""

    results: tuple[BaselineTestResult, ...]
    passed: bool


BaselineCheck = Callable[[], BaselineTestResult | bool]


def run_baseline_tests(checks: Iterable[BaselineCheck] | None) -> BaselineReport:
    """Run supplied baseline checks and fail closed on a check exception.

    The loader does not invent or silently skip B01--B08 benchmark checks.
    Callers provide the checks for the environment under test; every check is
    represented in the returned immutable report, including failures.
    """

    results: list[BaselineTestResult] = []
    for index, check in enumerate(checks or (), start=1):
        try:
            outcome = check()
        except Exception as exc:  # pragma: no cover - exercised by callers
            results.append(
                BaselineTestResult(
                    name=f"baseline-{index}",
                    passed=False,
                    detail=type(exc).__name__,
                )
            )
            continue
        if isinstance(outcome, BaselineTestResult):
            results.append(outcome)
        else:
            results.append(
                BaselineTestResult(name=f"baseline-{index}", passed=bool(outcome))
            )
    frozen = tuple(results)
    return BaselineReport(results=frozen, passed=bool(frozen) and all(r.passed for r in frozen))


def run_registry_integrity_baseline(
    root: str | Path | None = None,
    *,
    mode: str = "preview",
) -> BaselineReport:
    """Run executable registry/config integrity checks only.

    This is deliberately not a provider or model benchmark.  It validates the
    control-plane artifact that the loader can actually consume, its immutable
    bundle shape, and its manifest hash coverage.  Any failed prerequisite is
    represented as a failed result instead of being silently omitted.
    """

    snapshot: RegistrySnapshot | None = None

    def load_check() -> BaselineTestResult:
        nonlocal snapshot
        try:
            snapshot = load_registry(root, mode=mode)
        except RegistryLoadError as exc:
            return BaselineTestResult("registry-load", False, exc.code)
        return BaselineTestResult("registry-load", True, "registry schema/hash/reference integrity")

    def bundle_check() -> BaselineTestResult:
        if snapshot is None:
            return BaselineTestResult("registry-bundle", False, "registry-load did not pass")
        required = {
            "route_policy",
            "workflow_templates",
            "execution_roles",
            "model_policies",
            "model_profiles",
            "capability_contracts",
            "semantic_router_prompt",
        }
        missing = required - set(snapshot.bundle)
        if missing:
            return BaselineTestResult("registry-bundle", False, "missing bundle sections")
        return BaselineTestResult("registry-bundle", True, "required runtime bundle sections present")

    def hashes_check() -> BaselineTestResult:
        if snapshot is None:
            return BaselineTestResult("registry-hashes", False, "registry-load did not pass")
        declared = {
            entry["path"]: entry["sha256"]
            for entry in snapshot.manifest["files"]
        }
        if declared != dict(snapshot.payload_hashes):
            return BaselineTestResult("registry-hashes", False, "manifest hash coverage mismatch")
        return BaselineTestResult("registry-hashes", True, "manifest hash coverage verified")

    return run_baseline_tests((load_check, bundle_check, hashes_check))


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    """Immutable registry state bound to one agent/session initialization."""

    root: Path
    registry_version: str
    promotion_state: str
    manifest_sha256: str
    payload_hashes: Mapping[str, str]
    bundle: Mapping[str, Any]
    loaded_at: datetime
    source: str
    is_candidate: bool
    manifest: Mapping[str, Any]

    @property
    def version(self) -> str:
        """Compatibility alias for consumers that use ``version``."""

        return self.registry_version

    @property
    def manifest_hash(self) -> str:
        """Compatibility alias for the manifest digest."""

        return self.manifest_sha256


@dataclass(frozen=True, slots=True)
class RuntimeRegistryState:
    """Stable, read-only registry state exposed by one agent instance."""

    enabled: bool
    mode: str
    status: str
    version: str | None = None
    promotion_state: str | None = None
    manifest_hash: str | None = None
    inactive_reason: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if self.inactive_reason is not None and not isinstance(
            self.inactive_reason, MappingProxyType
        ):
            object.__setattr__(
                self,
                "inactive_reason",
                MappingProxyType(dict(self.inactive_reason)),
            )

    @property
    def active(self) -> bool:
        return self.status == "active"

    @property
    def candidate(self) -> bool:
        return self.status == "candidate"

    @property
    def inactive(self) -> bool:
        return self.status == "inactive"

    @property
    def state(self) -> str:
        """Short alias for callers that name the activation state ``state``."""

        return self.status

    @property
    def reason(self) -> Mapping[str, str] | None:
        """Short alias for the structured inactive reason."""

        return self.inactive_reason

    @property
    def registry_version(self) -> str | None:
        """Compatibility alias matching :class:`RegistrySnapshot`."""

        return self.version

    @property
    def manifest_sha256(self) -> str | None:
        """Compatibility alias matching :class:`RegistrySnapshot`."""

        return self.manifest_hash


class RegistryLoader:
    """Read-only loader bound to one registry root."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = _default_registry_root() if root is None else _coerce_registry_root(root)

    def load(
        self,
        *,
        mode: str = "production",
        allow_candidate: bool = False,
    ) -> RegistrySnapshot:
        return load_registry(
            self.root,
            mode=mode,
            allow_candidate=allow_candidate,
        )


def _default_registry_root() -> Path:
    """Resolve the profile-aware default without touching registry contents."""

    return get_hermes_home() / DEFAULT_REGISTRY_RELATIVE_PATH


def _coerce_registry_root(value: str | Path) -> Path:
    """Convert a caller-supplied root without leaking path exceptions."""

    if not isinstance(value, (str, os.PathLike)):
        raise RegistryLoadError(
            "registry root must be a path string",
            code="invalid_root",
            path="root",
        )
    try:
        raw_value = os.fspath(value)
        if not isinstance(raw_value, str):
            raise TypeError("registry root path must be text")
        if "\x00" in raw_value:
            raise ValueError("NUL byte in registry root")
        return Path(raw_value).expanduser()
    except RegistryLoadError:
        raise
    except Exception as exc:
        raise RegistryLoadError(
            "registry root is not a valid path",
            code="invalid_registry_path",
            path="root",
        ) from exc


def _freeze(value: Any) -> Any:
    """Recursively turn JSON values into immutable Python values."""

    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


def _schema_error(path: str, message: str, *, code: str = "invalid_schema") -> RegistryLoadError:
    return RegistryLoadError(message, code=code, path=path)


def _require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise _schema_error(f"{path}", f"{path} must be an object")
    for key in value:
        if not isinstance(key, str) or not key:
            raise _schema_error(f"{path}", f"{path} has an invalid key")
    return value


def _require_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise _schema_error(path, f"{path} must be a non-empty string")
    return value


def _require_bool(value: Any, path: str) -> bool:
    if type(value) is not bool:
        raise _schema_error(path, f"{path} must be a boolean")
    return value


def _require_int(value: Any, path: str) -> int:
    if type(value) is not int:
        raise _schema_error(path, f"{path} must be an integer")
    return value


def _require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise _schema_error(path, f"{path} must be an array")
    return value


def _require_required_keys(value: Mapping[str, Any], path: str, keys: Iterable[str]) -> None:
    for key in keys:
        if key not in value:
            raise RegistryLoadError(
                f"missing required field {key!r} at {path}",
                code="missing_required_field",
                path=f"{path}.{key}",
            )


def _validate_string_list(value: Any, path: str, *, nonempty: bool = False) -> list[str]:
    items = _require_list(value, path)
    if nonempty and not items:
        raise _schema_error(path, f"{path} must not be empty")
    return [_require_string(item, f"{path}[{index}]") for index, item in enumerate(items)]


def _validate_string_mapping(value: Any, path: str) -> Mapping[str, Any]:
    mapping = _require_mapping(value, path)
    for key, item in mapping.items():
        _require_string(key, f"{path}.<key>")
        _require_string(item, f"{path}.{key}")
    return mapping


def _validate_route_payload(value: Mapping[str, Any], path: str) -> None:
    _require_required_keys(
        value,
        path,
        (
            "default_route",
            "level_contracts",
            "level_workflows",
            "specialized_workflows",
            "risk_gates",
            "semantic_router",
            "workflow_rank",
        ),
    )
    default_route = _require_mapping(value["default_route"], f"{path}.default_route")
    _require_required_keys(default_route, f"{path}.default_route", ("level", "risk"))
    _require_string(default_route["level"], f"{path}.default_route.level")
    _require_string(default_route["risk"], f"{path}.default_route.risk")
    _validate_string_mapping(value["level_contracts"], f"{path}.level_contracts")
    _validate_string_mapping(value["level_workflows"], f"{path}.level_workflows")
    specialized = _require_mapping(value["specialized_workflows"], f"{path}.specialized_workflows")
    for name, item in specialized.items():
        item_path = f"{path}.specialized_workflows.{name}"
        config = _require_mapping(item, item_path)
        _require_required_keys(config, item_path, ("level", "risk", "workflow", "contract"))
        for key in ("level", "risk", "workflow", "contract"):
            _require_string(config[key], f"{item_path}.{key}")
    risk_gates = _require_mapping(value["risk_gates"], f"{path}.risk_gates")
    for name, item in risk_gates.items():
        item_path = f"{path}.risk_gates.{name}"
        gate = _require_mapping(item, item_path)
        _require_required_keys(gate, item_path, ("min_workflow",))
        if gate["min_workflow"] is not None:
            _require_string(gate["min_workflow"], f"{item_path}.min_workflow")
        if "force_review" in gate:
            _require_bool(gate["force_review"], f"{item_path}.force_review")
    semantic = _require_mapping(value["semantic_router"], f"{path}.semantic_router")
    _require_required_keys(
        semantic,
        f"{path}.semantic_router",
        ("enabled", "max_calls_per_task", "timeout_ms", "model_policy", "triggers", "on_failure", "on_low_confidence"),
    )
    _require_bool(semantic["enabled"], f"{path}.semantic_router.enabled")
    for key in ("max_calls_per_task", "timeout_ms"):
        if _require_int(semantic[key], f"{path}.semantic_router.{key}") <= 0:
            raise _schema_error(f"{path}.semantic_router.{key}", "integer must be positive")
    _require_string(semantic["model_policy"], f"{path}.semantic_router.model_policy")
    _validate_string_list(semantic["triggers"], f"{path}.semantic_router.triggers")
    for key in ("on_failure", "on_low_confidence"):
        _require_string(semantic[key], f"{path}.semantic_router.{key}")
    if "rules" in semantic:
        rules = _require_mapping(semantic["rules"], f"{path}.semantic_router.rules")
        for name, item in rules.items():
            item_path = f"{path}.semantic_router.rules.{name}"
            rule = _require_mapping(item, item_path)
            _require_required_keys(rule, item_path, ("workflow",))
            _require_string(rule["workflow"], f"{item_path}.workflow")
            if "triggers" in rule:
                _validate_string_list(rule["triggers"], f"{item_path}.triggers")
            for key in ("level", "risk", "model_policy", "on_failure", "on_low_confidence"):
                if key in rule:
                    _require_string(rule[key], f"{item_path}.{key}")
    ranks = _require_mapping(value["workflow_rank"], f"{path}.workflow_rank")
    for name, rank in ranks.items():
        if _require_int(rank, f"{path}.workflow_rank.{name}") < 0:
            raise _schema_error(f"{path}.workflow_rank.{name}", "integer must not be negative")


def _validate_workflow_payload(value: Mapping[str, Any], path: str) -> None:
    templates = _require_mapping(value["templates"], f"{path}.templates")
    for name, item in templates.items():
        item_path = f"{path}.templates.{name}"
        template = _require_mapping(item, item_path)
        _require_required_keys(template, item_path, ("roles", "verify"))
        _validate_string_list(template["roles"], f"{item_path}.roles", nonempty=True)
        _require_bool(template["verify"], f"{item_path}.verify")
        for key in ("allowed_worker_roles", "policies"):
            if key in template:
                _validate_string_list(template[key], f"{item_path}.{key}")
        for key in ("worker_selection",):
            if key in template:
                _require_string(template[key], f"{item_path}.{key}")
        for key in ("review_required",):
            if key in template:
                _require_bool(template[key], f"{item_path}.{key}")
        for key in ("model_policy", "contract", "capability_contract"):
            if key in template:
                _require_string(template[key], f"{item_path}.{key}")


def _validate_roles_payload(value: Mapping[str, Any], path: str) -> None:
    roles = _require_mapping(value["roles"], f"{path}.roles")
    for name, item in roles.items():
        item_path = f"{path}.roles.{name}"
        role = _require_mapping(item, item_path)
        _require_required_keys(role, item_path, ("responsibility", "tools", "dispatch", "model_policy"))
        for key in ("responsibility", "tools", "dispatch", "model_policy"):
            _require_string(role[key], f"{item_path}.{key}")
        if "read_only" in role:
            _require_bool(role["read_only"], f"{item_path}.read_only")
        for key in ("contract", "capability_contract"):
            if key in role:
                _require_string(role[key], f"{item_path}.{key}")
        if "capability_contracts" in role:
            _validate_string_list(role["capability_contracts"], f"{item_path}.capability_contracts")


def _validate_model_policy_payload(value: Mapping[str, Any], path: str) -> None:
    policies = _require_mapping(value["policies"], f"{path}.policies")
    selection_fields = ("primary", "primary_pool", "soft_failover", "hard_failover", "failover")
    for name, item in policies.items():
        item_path = f"{path}.policies.{name}"
        policy = _require_mapping(item, item_path)
        if not any(key in policy for key in selection_fields):
            raise RegistryLoadError(
                f"missing model selection in {item_path}",
                code="missing_required_field",
                path=f"{item_path}.primary",
            )
        for key in selection_fields:
            if key not in policy:
                continue
            field_path = f"{item_path}.{key}"
            if key.endswith("pool") or key.endswith("failover") or key == "failover":
                _validate_string_list(policy[key], field_path)
            else:
                _require_string(policy[key], field_path)
        for key in ("primary_pool_mode", "router_failure_action", "exclusion", "contract", "capability_contract", "note"):
            if key in policy:
                _require_string(policy[key], f"{item_path}.{key}")
        if "degradation" in policy:
            degradation_path = f"{item_path}.degradation"
            degradation = _require_mapping(policy["degradation"], degradation_path)
            _require_required_keys(degradation, degradation_path, ("strategy", "description", "thinking_escalation"))
            for key in ("strategy", "description", "thinking_escalation"):
                _require_string(degradation[key], f"{degradation_path}.{key}")
        if "rules" in policy:
            rules = _require_mapping(policy["rules"], f"{item_path}.rules")
            for vendor, models in rules.items():
                _validate_string_list(models, f"{item_path}.rules.{vendor}")


def _validate_model_profile_payload(value: Mapping[str, Any], path: str) -> None:
    profiles = _require_mapping(value["profiles"], f"{path}.profiles")
    for name, item in profiles.items():
        item_path = f"{path}.profiles.{name}"
        profile = _require_mapping(item, item_path)
        _require_required_keys(
            profile,
            item_path,
            ("vendor_family", "supported_reasoning", "thinking_map", "context_window", "supports_tools", "supports_images"),
        )
        _require_string(profile["vendor_family"], f"{item_path}.vendor_family")
        _validate_string_list(profile["supported_reasoning"], f"{item_path}.supported_reasoning")
        thinking_map = _validate_string_mapping(profile["thinking_map"], f"{item_path}.thinking_map")
        if not thinking_map:
            raise _schema_error(f"{item_path}.thinking_map", "mapping must not be empty")
        context_window = _require_int(profile["context_window"], f"{item_path}.context_window")
        if context_window <= 0:
            raise _schema_error(f"{item_path}.context_window", "integer must be positive")
        for key in ("supports_tools", "supports_images"):
            _require_bool(profile[key], f"{item_path}.{key}")
        if "supports_vision" in profile:
            _require_bool(profile["supports_vision"], f"{item_path}.supports_vision")
        # ``supports_image_generation`` is a forward-compatible capability
        # flag introduced for image-generation-only profiles (FAL catalog).
        # It is optional so legacy fixtures authored before the field
        # existed continue to validate unchanged; a missing value is
        # treated as ``False`` by every downstream consumer.  When the
        # field IS present, however, it must be a real boolean — strings
        # / ints / ``None`` fail closed with ``invalid_schema``.
        if "supports_image_generation" in profile:
            _require_bool(
                profile["supports_image_generation"],
                f"{item_path}.supports_image_generation",
            )
        for key in ("routing_role", "capability_source", "contract", "capability_contract"):
            if key in profile:
                _require_string(profile[key], f"{item_path}.{key}")


def _validate_capability_payload(value: Mapping[str, Any], path: str) -> None:
    contracts = _require_mapping(value["contracts"], f"{path}.contracts")
    for name, item in contracts.items():
        item_path = f"{path}.contracts.{name}"
        contract = _require_mapping(item, item_path)
        _require_required_keys(contract, item_path, ("quality", "modality", "tools", "context_class", "reasoning_intent"))
        _require_string(contract["quality"], f"{item_path}.quality")
        _validate_string_list(contract["modality"], f"{item_path}.modality", nonempty=True)
        for key in ("tools", "context_class", "reasoning_intent"):
            _require_string(contract[key], f"{item_path}.{key}")
        if "output_policy" in contract:
            output_policy = _require_string(contract["output_policy"], f"{item_path}.output_policy")
            if output_policy not in {"concise_evidence", "standard", "full_evidence"}:
                raise _schema_error(
                    f"{item_path}.output_policy",
                    "output_policy must be concise_evidence, standard, or full_evidence",
                )
        for key in ("max_tokens", "timeout_ms"):
            if key in contract:
                if _require_int(contract[key], f"{item_path}.{key}") <= 0:
                    raise _schema_error(f"{item_path}.{key}", "integer must be positive")


def _parse_json(data: bytes, relative_path: str) -> Mapping[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryLoadError(
            f"invalid JSON in {relative_path}: {type(exc).__name__}",
            code="invalid_json",
            path=relative_path,
        ) from exc
    if not isinstance(value, dict):
        raise RegistryLoadError(
            f"JSON payload {relative_path} must be an object",
            code="invalid_schema",
            path=relative_path,
        )
    schema_version = value.get("schema_version")
    expected_version = _SUPPORTED_PAYLOAD_SCHEMA_VERSIONS.get(relative_path)
    if expected_version is None:
        raise RegistryLoadError(
            f"unknown behavior payload: {relative_path}",
            code="unknown_payload",
            path=relative_path,
        )
    if schema_version != expected_version:
        raise RegistryLoadError(
            f"unsupported schema_version in {relative_path}: {schema_version!r}",
            code="unsupported_schema_version",
            path=relative_path,
        )
    required_section = _REQUIRED_PAYLOAD_SECTIONS[relative_path]
    _require_required_keys(value, relative_path, ("description",))
    _require_string(value["description"], f"{relative_path}.description")
    if required_section not in value:
        raise RegistryLoadError(
            f"missing required section {required_section!r} in {relative_path}",
            code="invalid_schema",
            path=relative_path,
        )
    _require_required_keys(value, relative_path, ("schema_version",))
    _require_mapping(value[required_section], f"{relative_path}.{required_section}")
    validators = {
        "route-policy.json": _validate_route_payload,
        "workflow-templates.json": _validate_workflow_payload,
        "execution-roles.json": _validate_roles_payload,
        "model-policies.json": _validate_model_policy_payload,
        "model-profiles.json": _validate_model_profile_payload,
        "capability-contracts.json": _validate_capability_payload,
    }
    validators[relative_path](value, relative_path)
    return value


def _validate_manifest(manifest: Any) -> tuple[str, str, list[Mapping[str, Any]]]:
    if not isinstance(manifest, dict):
        raise RegistryLoadError(
            "manifest must be a JSON object",
            code="invalid_manifest",
            path="manifest.json",
        )

    schema_version = manifest.get("schemaVersion")
    if schema_version != SUPPORTED_MANIFEST_SCHEMA_VERSION:
        raise RegistryLoadError(
            f"unsupported schemaVersion: {schema_version!r}",
            code="unsupported_schema_version",
            path="manifest.json",
        )

    registry_version = manifest.get("registryVersion")
    if not isinstance(registry_version, str):
        raise RegistryLoadError(
            "registryVersion must be a string",
            code="invalid_registry_version",
            path="manifest.json",
        )
    match = _REGISTRY_VERSION_RE.fullmatch(registry_version)
    if match is None:
        raise RegistryLoadError(
            f"invalid registryVersion: {registry_version!r}",
            code="invalid_registry_version",
            path="manifest.json",
        )
    try:
        datetime.strptime(match.group("date"), "%Y-%m-%d")
    except ValueError as exc:
        raise RegistryLoadError(
            f"invalid registryVersion date: {registry_version!r}",
            code="invalid_registry_version",
            path="manifest.json",
        ) from exc

    promotion_state = manifest.get("promotionState")
    if not isinstance(promotion_state, str) or promotion_state not in SUPPORTED_PROMOTION_STATES:
        raise RegistryLoadError(
            f"unsupported promotion state: {promotion_state!r}",
            code="invalid_promotion_state",
            path="manifest.json",
        )

    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise RegistryLoadError(
            "manifest files must be a non-empty list",
            code="invalid_manifest",
            path="manifest.json",
        )
    normalized: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise RegistryLoadError(
                "manifest file entries must be objects",
                code="invalid_manifest",
                path="manifest.json",
            )
        raw_path = entry.get("path")
        digest = entry.get("sha256")
        if not isinstance(raw_path, str) or not raw_path:
            raise RegistryLoadError(
                "manifest file path must be a non-empty string",
                code="invalid_manifest",
                path="manifest.json",
            )
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise RegistryLoadError(
                f"invalid sha256 for {raw_path!r}",
                code="invalid_hash",
                path=raw_path,
            )
        manifest_path = _safe_relative_path(raw_path)
        if manifest_path not in _REQUIRED_PAYLOAD_FILES:
            raise RegistryLoadError(
                f"unknown behavior payload: {manifest_path}",
                code="unknown_payload",
                path=manifest_path,
            )
        if manifest_path in seen:
            raise RegistryLoadError(
                f"duplicate manifest path: {raw_path!r}",
                code="duplicate_path",
                path=raw_path,
            )
        seen.add(manifest_path)
        normalized.append({"path": manifest_path, "sha256": digest.lower()})

    missing_declarations = _REQUIRED_PAYLOAD_FILES - seen
    if missing_declarations:
        missing = ", ".join(sorted(missing_declarations))
        raise RegistryLoadError(
            f"manifest does not declare required payloads: {missing}",
            code="invalid_manifest",
            path="manifest.json",
        )
    return registry_version, promotion_state, normalized


def _safe_relative_path(raw_path: str) -> str:
    # Backslashes are rejected instead of normalized: accepting them would make
    # a manifest authored on one platform mean something different on another.
    if not isinstance(raw_path, str) or not raw_path:
        raise RegistryLoadError(
            "manifest payload path must be a non-empty string",
            code="invalid_manifest",
            path="manifest.json.files[].path",
        )
    if "\x00" in raw_path:
        raise RegistryLoadError(
            "manifest payload path contains NUL",
            code="invalid_path",
            path="manifest.json.files[].path",
        )
    if "\\" in raw_path:
        raise RegistryLoadError(
            f"unsafe path in manifest: {raw_path!r}",
            code="path_traversal",
            path=raw_path,
        )
    candidate = PurePosixPath(raw_path)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        raise RegistryLoadError(
            f"unsafe path in manifest: {raw_path!r}",
            code="path_traversal",
            path=raw_path,
        )
    normalized = candidate.as_posix()
    if normalized in {"", ".", "manifest.json"}:
        raise RegistryLoadError(
            f"manifest cannot declare {raw_path!r}",
            code="invalid_manifest",
            path=raw_path,
        )
    return normalized


def _read_regular_file_nofollow(
    root: Path,
    relative_path: str,
    *,
    missing_code: str,
    unsafe_code: str,
) -> bytes:
    """Read a registry file through a directory-FD walk with O_NOFOLLOW."""

    parts = PurePosixPath(relative_path).parts
    root_fd = -1
    directory_fds: list[int] = []
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        current_fd = root_fd
        for part in parts[:-1]:
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current_fd,
            )
            directory_fds.append(next_fd)
            current_fd = next_fd
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=current_fd)
        try:
            file_stat = os.fstat(fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise RegistryLoadError(
                    f"registry file is not a regular file: {relative_path}",
                    code=unsafe_code,
                    path=relative_path,
                )
            with os.fdopen(fd, "rb") as handle:
                fd = -1
                return handle.read()
        finally:
            if fd >= 0:
                os.close(fd)
    except RegistryLoadError:
        raise
    except FileNotFoundError as exc:
        raise RegistryLoadError(
            f"missing registry file: {relative_path}",
            code=missing_code,
            path=relative_path,
        ) from exc
    except OSError as exc:
        raise RegistryLoadError(
            f"unsafe registry file or path escapes registry root: {relative_path}",
            code=unsafe_code,
            path=relative_path,
        ) from exc
    finally:
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)
        if root_fd >= 0:
            os.close(root_fd)


def _validate_payload_files(
    root: Path,
    entries: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, str]]:
    payloads: dict[str, Any] = {}
    payload_hashes: dict[str, str] = {}
    declared = set()
    for entry in entries:
        relative_path = str(entry["path"])
        declared.add(relative_path)
        data = _read_regular_file_nofollow(
            root,
            relative_path,
            missing_code="missing_file",
            unsafe_code="unsafe_file",
        )
        actual_hash = hashlib.sha256(data).hexdigest()
        expected_hash = str(entry["sha256"])
        if actual_hash != expected_hash:
            raise RegistryLoadError(
                f"hash mismatch for {relative_path}",
                code="hash_mismatch",
                path=relative_path,
            )
        payload_hashes[relative_path] = actual_hash
        if relative_path.endswith(".json"):
            payloads[relative_path] = _parse_json(data, relative_path)
        elif relative_path == "semantic-router-prompt.md":
            try:
                prompt = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RegistryLoadError(
                    f"invalid UTF-8 in {relative_path}",
                    code="invalid_payload",
                    path=relative_path,
                ) from exc
            if not prompt.strip():
                raise RegistryLoadError(
                    f"empty payload: {relative_path}",
                    code="invalid_payload",
                    path=relative_path,
                )
            payloads[relative_path] = prompt
        else:
            raise RegistryLoadError(
                f"unrecognized behavior payload: {relative_path}",
                code="invalid_manifest",
                path=relative_path,
            )

    for child in root.rglob("*"):
        relative = child.relative_to(root).as_posix()
        if relative == "manifest.json" or relative in EXEMPT_FILES:
            continue
        if child.is_file() or child.is_symlink():
            if relative not in declared:
                raise RegistryLoadError(
                    f"unlisted behavior file: {relative}",
                    code="unlisted_file",
                    path=relative,
                )
    return payloads, payload_hashes


def _validate_cross_file_references(payloads: Mapping[str, Any]) -> None:
    """Validate every declared cross-file reference without skipping malformed data."""

    route = payloads["route-policy.json"]
    workflows = payloads["workflow-templates.json"]["templates"]
    roles = payloads["execution-roles.json"]["roles"]
    policies = payloads["model-policies.json"]["policies"]
    profiles = payloads["model-profiles.json"]["profiles"]
    contracts = payloads["capability-contracts.json"]["contracts"]

    def require_ref(value: str, target: Mapping[str, Any], path: str, label: str) -> None:
        if value not in target:
            raise RegistryLoadError(
                f"unknown {label} reference: {value!r}",
                code="dangling_reference",
                path=path,
            )

    default_level = route["default_route"]["level"]
    default_risk = route["default_route"]["risk"]
    require_ref(default_level, route["level_contracts"], "route-policy.json.default_route.level", "level")
    require_ref(default_risk, route["risk_gates"], "route-policy.json.default_route.risk", "risk gate")
    for level, contract in route["level_contracts"].items():
        require_ref(contract, contracts, f"route-policy.json.level_contracts.{level}", "capability contract")
    for level, workflow in route["level_workflows"].items():
        require_ref(workflow, workflows, f"route-policy.json.level_workflows.{level}", "workflow")
    for name, config in route["specialized_workflows"].items():
        item_path = f"route-policy.json.specialized_workflows.{name}"
        require_ref(config["level"], route["level_contracts"], f"{item_path}.level", "level")
        require_ref(config["risk"], route["risk_gates"], f"{item_path}.risk", "risk gate")
        require_ref(config["workflow"], workflows, f"{item_path}.workflow", "workflow")
        require_ref(config["contract"], contracts, f"{item_path}.contract", "capability contract")
    for name, gate in route["risk_gates"].items():
        if gate["min_workflow"] is not None:
            require_ref(
                gate["min_workflow"],
                workflows,
                f"route-policy.json.risk_gates.{name}.min_workflow",
                "workflow",
            )

    semantic = route["semantic_router"]
    semantic_policy = semantic["model_policy"]
    if semantic_policy != "route":
        require_ref(semantic_policy, policies, "route-policy.json.semantic_router.model_policy", "model policy")
    for name, rule in semantic.get("rules", {}).items():
        item_path = f"route-policy.json.semantic_router.rules.{name}"
        require_ref(rule["workflow"], workflows, f"{item_path}.workflow", "workflow")
        if "level" in rule:
            require_ref(rule["level"], route["level_contracts"], f"{item_path}.level", "level")
        if "risk" in rule:
            require_ref(rule["risk"], route["risk_gates"], f"{item_path}.risk", "risk gate")
        if "model_policy" in rule and rule["model_policy"] != "route":
            require_ref(rule["model_policy"], policies, f"{item_path}.model_policy", "model policy")

    for workflow_name in route["workflow_rank"]:
        require_ref(
            workflow_name,
            workflows,
            f"route-policy.json.workflow_rank.{workflow_name}",
            "workflow",
        )

    for workflow_name, template in workflows.items():
        item_path = f"workflow-templates.json.templates.{workflow_name}"
        for field in ("roles", "allowed_worker_roles"):
            for index, role in enumerate(template.get(field, [])):
                require_ref(role, roles, f"{item_path}.{field}[{index}]", "role")
        for index, policy in enumerate(template.get("policies", [])):
            if policy != "route":
                require_ref(policy, policies, f"{item_path}.policies[{index}]", "model policy")
        for field in ("contract", "capability_contract"):
            if field in template:
                require_ref(template[field], contracts, f"{item_path}.{field}", "capability contract")
        if "model_policy" in template and template["model_policy"] != "route":
            require_ref(template["model_policy"], policies, f"{item_path}.model_policy", "model policy")

    for role_name, role in roles.items():
        item_path = f"execution-roles.json.roles.{role_name}"
        policy_name = role["model_policy"]
        if policy_name != "route":
            require_ref(policy_name, policies, f"{item_path}.model_policy", "model policy")
        for field in ("contract", "capability_contract"):
            if field in role:
                require_ref(role[field], contracts, f"{item_path}.{field}", "capability contract")
        for index, contract in enumerate(role.get("capability_contracts", [])):
            require_ref(contract, contracts, f"{item_path}.capability_contracts[{index}]", "capability contract")

    for policy_name, policy in policies.items():
        item_path = f"model-policies.json.policies.{policy_name}"
        for field in ("primary", "primary_pool", "soft_failover", "hard_failover", "failover"):
            if field not in policy:
                continue
            values = [policy[field]] if field == "primary" else policy[field]
            for index, model in enumerate(values):
                if model == "dynamic":
                    continue
                model_path = f"{item_path}.{field}" if field == "primary" else f"{item_path}.{field}[{index}]"
                require_ref(model, profiles, model_path, "model profile")
        for field in ("contract", "capability_contract"):
            if field in policy:
                require_ref(policy[field], contracts, f"{item_path}.{field}", "capability contract")

    # Cross-file role gate: ``multimodal-extraction`` routes vision
    # *understanding* traffic.  Image-generation-only profiles
    # (``routing_role='image-generation-only'`` or
    # ``supports_image_generation=True``) are closed to understanding
    # traffic and have no business being named as the primary /
    # failover of a multimodal-extraction policy.  Fail closed with a
    # structured ``invalid_cross_file_role`` error pointing at the
    # offending policy entry so a future contributor can locate the
    # mismatch without spelunking through every payload.
    multimodal_policy = policies.get("multimodal-extraction")
    if multimodal_policy is not None:
        gate_path = "model-policies.json.policies.multimodal-extraction"
        for field in ("primary", "primary_pool", "soft_failover", "hard_failover", "failover"):
            if field not in multimodal_policy:
                continue
            values = (
                [multimodal_policy[field]]
                if field == "primary"
                else multimodal_policy[field]
            )
            for index, model in enumerate(values):
                if model == "dynamic":
                    continue
                if model not in profiles:
                    # Reference is dangling; the prior loop already
                    # surfaced that as ``dangling_reference``.  Don't
                    # double-report.
                    continue
                referenced = profiles[model]
                is_generation_only = referenced.get("routing_role") == "image-generation-only"
                is_image_gen = bool(referenced.get("supports_image_generation"))
                if is_generation_only or is_image_gen:
                    entry_path = (
                        f"{gate_path}.{field}"
                        if field == "primary"
                        else f"{gate_path}.{field}[{index}]"
                    )
                    raise RegistryLoadError(
                        (
                            f"profile {model!r} is image-generation-only and cannot "
                            f"serve multimodal-extraction at {entry_path}"
                        ),
                        code="invalid_cross_file_role",
                        path=entry_path,
                    )

    for profile_name, profile in profiles.items():
        item_path = f"model-profiles.json.profiles.{profile_name}"
        for field in ("contract", "capability_contract"):
            if field in profile:
                require_ref(profile[field], contracts, f"{item_path}.{field}", "capability contract")


def load_registry(
    root: str | Path | None = None,
    *,
    mode: str = "production",
    allow_candidate: bool = False,
) -> RegistrySnapshot:
    """Load and validate one immutable runtime registry snapshot.

    ``mode='production'`` accepts only APPROVED/PUBLISHED.  ``mode='preview'``
    is the explicit lab/candidate path for DRAFT and READY_FOR_REVIEW.  The
    ``allow_candidate`` keyword is retained for the plan's test/lab API and is
    equivalent to explicitly selecting preview mode.
    """

    if type(allow_candidate) is not bool:
        raise RegistryLoadError(
            "allow_candidate must be a boolean",
            code="invalid_allow_candidate",
            path="allow_candidate",
        )
    if type(mode) is not str or mode not in {"production", "preview"}:
        raise RegistryLoadError(
            f"unsupported load mode: {mode!r}",
            code="invalid_mode",
            path="mode",
        )
    normalized_mode = mode
    if allow_candidate:
        normalized_mode = "preview"

    try:
        registry_root = (
            _default_registry_root()
            if root is None
            else _coerce_registry_root(root)
        )
        resolved_root = registry_root.resolve(strict=True)
    except RegistryLoadError:
        raise
    except OSError as exc:
        raise RegistryLoadError(
            "registry root does not exist",
            code="missing_root",
            path="root",
        ) from exc
    except Exception as exc:
        raise RegistryLoadError(
            "registry root is not a valid path",
            code="invalid_registry_path",
            path="root",
        ) from exc
    if not resolved_root.is_dir():
        raise RegistryLoadError("registry root is not a directory", code="invalid_root")

    try:
        manifest_bytes = _read_regular_file_nofollow(
            resolved_root,
            "manifest.json",
            missing_code="missing_manifest",
            unsafe_code="invalid_manifest_file",
        )
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except RegistryLoadError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryLoadError(
            f"invalid JSON in manifest.json: {type(exc).__name__}",
            code="invalid_manifest",
            path="manifest.json",
        ) from exc

    registry_version, promotion_state, entries = _validate_manifest(manifest)
    if normalized_mode == "production" and promotion_state not in PRODUCTION_PROMOTION_STATES:
        raise RegistryLoadError(
            f"promotion state {promotion_state} is not allowed in production",
            code="promotion_rejected",
            path="manifest.json",
        )

    payloads, payload_hashes = _validate_payload_files(resolved_root, entries)
    _validate_cross_file_references(payloads)

    bundle = {
        "route_policy": payloads["route-policy.json"],
        "workflow_templates": payloads["workflow-templates.json"],
        "execution_roles": payloads["execution-roles.json"],
        "model_policies": payloads["model-policies.json"],
        "model_profiles": payloads["model-profiles.json"],
        "capability_contracts": payloads["capability-contracts.json"],
        "semantic_router_prompt": payloads["semantic-router-prompt.md"],
    }
    return RegistrySnapshot(
        root=resolved_root,
        registry_version=registry_version,
        promotion_state=promotion_state,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        payload_hashes=MappingProxyType(dict(payload_hashes)),
        bundle=_freeze(bundle),
        loaded_at=datetime.now(timezone.utc),
        source=normalized_mode,
        is_candidate=promotion_state not in PRODUCTION_PROMOTION_STATES,
        manifest=_freeze(manifest),
    )


def validate_promotion_state(promotion_state: str, *, mode: str = "production") -> bool:
    """Return whether a promotion state is loadable in the requested mode."""

    if type(mode) is not str or mode not in {"production", "preview"}:
        raise RegistryLoadError(
            f"unsupported load mode: {mode!r}",
            code="invalid_mode",
            path="mode",
        )
    if type(promotion_state) is not str or promotion_state not in SUPPORTED_PROMOTION_STATES:
        return False
    return mode == "preview" or promotion_state in PRODUCTION_PROMOTION_STATES


# Explicit public aliases make the loader/promotion seam easy for later CLI and
# agent-init slices to consume without introducing a second implementation.
load_runtime_registry = load_registry
validate_registry_promotion = validate_promotion_state

__all__ = [
    "BaselineReport",
    "BaselineTestResult",
    "RegistryLoadError",
    "RegistryLoader",
    "RegistrySnapshot",
    "RuntimeRegistryState",
    "load_registry",
    "load_runtime_registry",
    "run_baseline_tests",
    "run_registry_integrity_baseline",
    "validate_promotion_state",
    "validate_registry_promotion",
]
