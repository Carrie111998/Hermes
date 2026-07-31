"""Kwilo-specific capability admission and semantic evidence gates.

The core Kanban lifecycle remains generic.  This module is deliberately
board-scoped: only the ``kwilo`` board and only tasks created on or after the
activation boundary are governed.  Legacy rows have no ``task_semantics`` row
and retain completion-only dependency behaviour.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import yaml
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

KWILO_BOARD = "kwilo"
DEFAULT_ACTIVATED_AT = "2026-07-22T15:44:52Z"
AUTHORIZED_PHASE_PROFILES = {
    "deterministic-verification": "kwilo-patch",
    "sentinel-review": "kwilo-sentinel",
    "tess-qa": "kwilo-tess",
}
CURRENT_WORKFLOW_VERSION = "3.0.0"
LEGACY_WORKFLOW_VERSIONS = frozenset({"", "2.0.0"})

VALID_PHASES = frozenset({
    "intake", "canonicalisation", "implementation",
    "deterministic-verification", "sentinel-review",
    "tess-qa", "merge-readiness", "release-readiness", "merge", "deployment",
    "operational-uat", "publication", "campaign-closure",
})
VALID_VERDICTS = frozenset({
    "pass", "fail", "changes-requested", "blocked", "needs-input",
    "not-applicable", "not-evaluated",
})
VERDICT_REQUIRED_PHASES = frozenset({
    "deterministic-verification", "sentinel-review",
    "tess-qa", "merge-readiness", "release-readiness", "operational-uat", "campaign-closure",
})
REVISION_BOUND_PHASES = frozenset({
    "implementation", "deterministic-verification",
    "sentinel-review", "tess-qa", "merge-readiness", "release-readiness", "merge",
    "deployment", "operational-uat", "publication", "campaign-closure",
})
VALID_CANDIDATE_KINDS = frozenset({
    "commit-sha", "uncommitted-tree-digest", "deployed-artifact-digest",
})
VALID_LINK_KINDS = frozenset({
    "completion", "evidence-gate", "supervision", "informational",
})
WORKSPACE_KIND_BY_CLASS = {
    "isolated-worktree": "worktree",
    "isolated-read-only-worktree": "worktree",
    "declared-dirty-continuation": "dir",
    "shared-read-only": "dir",
    "owned-scratch": "scratch",
    "owned-test-fixture": "scratch",
    "isolated-tooling-copy": "scratch",
}
_REQUIRED_CONTRACT_FIELDS = (
    "requester", "authoriser", "acceptance_owner", "role", "lane",
    "repository", "mode", "workspace_class", "required_toolsets",
    "required_parent_skills", "required_connectors",
    "connector_read_or_mutate_scope", "allowed_side_effects",
    "prohibited_actions", "canonical_source", "evidence_destination",
    "phase", "base_revision", "candidate_identity",
)
_RISK_CONTRACT_FIELDS = (
    "workflow_version", "risk_tier", "change_categories",
    "required_gate_phases",
)
_MAX_EVIDENCE_ITEMS = 256
_MAX_EVIDENCE_TEXT = 2048
_MAX_ENVIRONMENT_JSON = 16384
_EVIDENCE_FIELDS = frozenset({
    "phase", "verdict", "candidate_identity", "environment", "checks",
    "blockers", "unresolved_acceptance_rows", "canonical_links",
    "supersedes_evidence_ids", "invalidates_evidence_ids",
})
_CHECK_FIELDS = frozenset({
    "executed", "passed_count", "failed_count", "skipped_count",
    "skipped_required_count", "skipped_policy_safe", "host_attested",
    "canonical_check_links",
})

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS task_semantics (
    task_id                     TEXT PRIMARY KEY,
    phase                       TEXT NOT NULL,
    repository_id               TEXT,
    project_id                  TEXT,
    requester                   TEXT NOT NULL,
    authoriser                  TEXT NOT NULL,
    acceptance_owner            TEXT NOT NULL,
    canonical_source            TEXT NOT NULL,
    evidence_destination        TEXT NOT NULL,
    workspace_class             TEXT,
    candidate_kind              TEXT,
    candidate_value             TEXT,
    candidate_paths_digest      TEXT,
    environment_json            TEXT,
    authority_consumed_json     TEXT,
    side_effects_performed_json TEXT,
    contract_json               TEXT NOT NULL,
    dispatch_readback_digest    TEXT,
    dispatch_readback_at        INTEGER,
    dispatch_readback_by        TEXT,
    updated_at                  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS task_evidence (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id                     TEXT NOT NULL,
    run_id                      INTEGER,
    producer_profile            TEXT,
    producer_task_id            TEXT,
    phase                       TEXT NOT NULL,
    verdict                     TEXT NOT NULL,
    candidate_kind              TEXT,
    candidate_value             TEXT,
    candidate_paths_digest      TEXT,
    environment_json            TEXT,
    checks_json                 TEXT,
    blockers_json               TEXT,
    unresolved_acceptance_json  TEXT,
    canonical_links_json        TEXT,
    supersedes_json             TEXT,
    invalidates_json            TEXT,
    created_at                  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_evidence_task
    ON task_evidence(task_id, id);
CREATE INDEX IF NOT EXISTS idx_task_evidence_candidate
    ON task_evidence(candidate_kind, candidate_value);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Install additive semantic tables and typed-link columns."""
    conn.executescript(SCHEMA_SQL)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(task_links)")}
    additions = (
        ("link_kind", "link_kind TEXT NOT NULL DEFAULT 'completion'"),
        ("required_phase", "required_phase TEXT"),
        ("required_verdict", "required_verdict TEXT"),
        ("require_candidate_match", "require_candidate_match INTEGER NOT NULL DEFAULT 0"),
    )
    for name, definition in additions:
        if name not in columns:
            conn.execute(f"ALTER TABLE task_links ADD COLUMN {definition}")
    evidence_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(task_evidence)")
    }
    for name, definition in (
        ("producer_profile", "producer_profile TEXT"),
        ("producer_task_id", "producer_task_id TEXT"),
    ):
        if name not in evidence_columns:
            conn.execute(f"ALTER TABLE task_evidence ADD COLUMN {definition}")
    semantics_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(task_semantics)")
    }
    for name, definition in (
        ("dispatch_readback_digest", "dispatch_readback_digest TEXT"),
        ("dispatch_readback_at", "dispatch_readback_at INTEGER"),
        ("dispatch_readback_by", "dispatch_readback_by TEXT"),
    ):
        if name not in semantics_columns:
            conn.execute(f"ALTER TABLE task_semantics ADD COLUMN {definition}")


def _database_path(conn: sqlite3.Connection) -> Optional[Path]:
    row = conn.execute("PRAGMA database_list").fetchone()
    value = row[2] if row else ""
    return Path(value).resolve() if value else None


def is_kwilo_board(conn: sqlite3.Connection, board: Optional[str] = None) -> bool:
    if board is not None:
        return str(board).strip().lower() == KWILO_BOARD
    path = _database_path(conn)
    if path is not None:
        return path.parent.name.lower() == KWILO_BOARD
    env_board = os.environ.get("HERMES_KANBAN_BOARD", "").strip().lower()
    return env_board == KWILO_BOARD if env_board else False


def _governance_dir() -> Path:
    override = os.environ.get("HERMES_KWILO_GOVERNANCE_DIR", "").strip()
    if override:
        return Path(override)
    root = os.environ.get("HERMES_HOME", "").strip()
    if root:
        home = Path(root)
        if home.parent.name == "profiles":
            home = home.parent.parent
    else:
        home = Path.home() / ".hermes"
    return home / "governance" / "kwilo"


def _bound_path(value: str) -> Path:
    """Resolve activation-bound paths independently of process cwd."""
    path = Path(value.strip())
    if path.is_absolute():
        return path
    root = os.environ.get("HERMES_HOME", "").strip()
    if root:
        home = Path(root)
        if home.parent.name == "profiles":
            home = home.parent.parent
        return home / path
    governance = _governance_dir()
    return governance.parent.parent / path


def _profiles_root() -> Path:
    root = os.environ.get("HERMES_HOME", "").strip()
    if root:
        home = Path(root)
        if home.parent.name == "profiles":
            home = home.parent.parent
        return home / "profiles"
    governance_override = os.environ.get("HERMES_KWILO_GOVERNANCE_DIR", "").strip()
    if governance_override:
        return Path(governance_override).resolve().parent.parent / "profiles"
    return Path.home() / ".hermes" / "profiles"


def _effective_profile_skill_names(
    profile_name: str,
    config: dict[str, Any],
    required_names: Iterable[str],
) -> set[str]:
    """Return skills the profile's CLI can actually preload."""
    profile_dir = _profiles_root() / profile_name
    try:
        from agent.skill_utils import (
            local_skill_is_loadable,
            resolve_external_skills_dirs,
        )
        from hermes_cli.skills_config import get_disabled_skills
    except ImportError as exc:
        raise ValueError(
            f"effective profile skill discovery is unavailable for {profile_name}: {exc}"
        ) from exc

    local_skills_dir = profile_dir / "skills"
    roots = [local_skills_dir]
    roots.extend(
        resolve_external_skills_dirs(
            config,
            hermes_home=profile_dir,
            local_skills_dir=local_skills_dir,
        )
    )
    disabled = get_disabled_skills(config, platform="cli")
    return {
        name
        for name in required_names
        if local_skill_is_loadable(name, roots, disabled=disabled)
    }


def _attest_effective_profile_capabilities(profile_name: str, profile: dict[str, Any]) -> None:
    config_path = _profiles_root() / profile_name / "config.yaml"
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"effective profile configuration is unavailable for {profile_name}: {exc}") from exc
    if not isinstance(config, dict):
        raise ValueError(f"effective profile configuration must be an object for {profile_name}")

    platform_toolsets = config.get("platform_toolsets")
    if not isinstance(platform_toolsets, dict):
        raise ValueError(f"effective profile CLI toolsets are unavailable for {profile_name}")
    effective_toolsets = _string_set(platform_toolsets.get("cli", []), "effective platform_toolsets.cli")
    declared_toolsets = _string_set(
        profile.get("configured_cli_toolsets", []), "profile.configured_cli_toolsets"
    )
    if effective_toolsets != declared_toolsets:
        undeclared = sorted(effective_toolsets - declared_toolsets)
        unavailable = sorted(declared_toolsets - effective_toolsets)
        raise ValueError(
            f"effective CLI toolsets do not match capability manifest for {profile_name}: "
            f"undeclared={undeclared}, unavailable={unavailable}"
        )

    mcp_servers = config.get("mcp_servers")
    if not isinstance(mcp_servers, dict):
        raise ValueError(f"effective MCP connector configuration is unavailable for {profile_name}")
    effective_connectors: set[str] = set()
    for connector, item in mcp_servers.items():
        if not isinstance(connector, str) or not connector.strip() or not isinstance(item, dict):
            raise ValueError(f"effective MCP connector configuration is malformed for {profile_name}")
        if item.get("enabled", True) is not False:
            effective_connectors.add(connector.strip())
    declared_connectors = _string_set(
        profile.get("configured_connectors", []), "profile.configured_connectors"
    )
    if effective_connectors != declared_connectors:
        undeclared = sorted(effective_connectors - declared_connectors)
        unavailable = sorted(declared_connectors - effective_connectors)
        raise ValueError(
            f"effective MCP connectors do not match capability manifest for {profile_name}: "
            f"undeclared={undeclared}, unavailable={unavailable}"
        )

    declared_skills = _string_set(
        profile.get("configured_parent_skills", []), "profile.configured_parent_skills"
    )
    unavailable_skills = sorted(
        declared_skills
        - _effective_profile_skill_names(profile_name, config, declared_skills)
    )
    if unavailable_skills:
        raise ValueError(
            f"effective parent skills unavailable for {profile_name}: "
            f"{', '.join(unavailable_skills)}"
        )


def _activation() -> dict[str, Any]:
    path = _governance_dir() / "activation.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Kwilo governance activation is unavailable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Kwilo governance activation must be a JSON object: {path}")
    return value


def activation_epoch() -> int:
    activation = _activation()
    raw_value = activation.get("activated_at")
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise ValueError("Kwilo governance activation requires activated_at")
    raw = raw_value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid Kwilo activated_at timestamp: {raw!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Kwilo activated_at timestamp must be timezone-aware")
    return int(parsed.timestamp())


def governance_required(
    conn: sqlite3.Connection, *, board: Optional[str], created_at: int
) -> bool:
    return is_kwilo_board(conn, board) and int(created_at) >= activation_epoch()


def _is_lower_hex_digest(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _manifest() -> dict[str, Any]:
    activation = _activation()
    raw = activation.get("capability_manifest")
    if not isinstance(raw, dict):
        raise ValueError("Kwilo capability manifest binding requires path and sha256")
    path_value = raw.get("path")
    digest_value = raw.get("sha256")
    if not isinstance(path_value, str) or not path_value.strip():
        raise ValueError("Kwilo capability manifest binding requires path and sha256")
    if not isinstance(digest_value, str) or not _is_lower_hex_digest(digest_value):
        raise ValueError("Kwilo capability manifest binding requires path and sha256")
    path = _bound_path(path_value)
    expected_digest = digest_value
    try:
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != expected_digest:
            raise ValueError(f"Kwilo capability manifest digest mismatch: {path}")
        value = json.loads(payload.decode("utf-8"))
    except ValueError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Kwilo capability manifest is unavailable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Kwilo capability manifest must be a JSON object: {path}")
    return value


def _semantic_policy() -> dict[str, Any]:
    """Load and validate the activation-bound semantic policy, failing closed."""
    activation = _activation()
    raw = activation.get("semantic_policy")
    if not isinstance(raw, dict):
        raise ValueError("Kwilo semantic policy binding requires path and sha256")
    path_value = raw.get("path")
    digest_value = raw.get("sha256")
    if not isinstance(path_value, str) or not path_value.strip():
        raise ValueError("Kwilo semantic policy binding requires path and sha256")
    if not isinstance(digest_value, str) or not _is_lower_hex_digest(digest_value):
        raise ValueError("Kwilo semantic policy binding requires path and sha256")
    path = _bound_path(path_value)
    try:
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != digest_value:
            raise ValueError(f"Kwilo semantic policy digest mismatch: {path}")
        policy = json.loads(payload.decode("utf-8"))
    except ValueError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Kwilo semantic policy is unavailable: {path}: {exc}") from exc
    if not isinstance(policy, dict):
        raise ValueError(f"Kwilo semantic policy must be a JSON object: {path}")
    for field in ("phases", "verdicts", "verdict_required_for", "revision_bound_phases"):
        values = policy.get(field)
        if not isinstance(values, list) or not values or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise ValueError(f"Kwilo semantic policy field {field} must be a non-empty string list")
    readiness = policy.get("readiness")
    if not isinstance(readiness, dict):
        raise ValueError("Kwilo semantic policy readiness must be an object")
    gates = readiness.get("legacy_required_gate_phases")
    if not isinstance(gates, list) or not gates or any(
        not isinstance(phase, str) or not phase.strip() for phase in gates
    ) or len(set(gates)) != len(gates):
        raise ValueError(
            "Kwilo semantic policy readiness.legacy_required_gate_phases "
            "must be a unique non-empty string list"
        )
    phases = set(policy["phases"])
    if any(phase not in phases or phase not in AUTHORIZED_PHASE_PROFILES for phase in gates):
        raise ValueError("Kwilo semantic policy contains an unsupported readiness gate phase")
    verdict = readiness.get("required_verdict")
    if verdict != "pass" or verdict not in policy["verdicts"]:
        raise ValueError("Kwilo semantic policy readiness.required_verdict must be 'pass'")
    if readiness.get("same_candidate_required") is not True:
        raise ValueError("Kwilo semantic policy readiness must require the same candidate")
    if (
        "deterministic-verification" in gates
        and readiness.get("host_attested_deterministic_verification") is not True
    ):
        raise ValueError("Kwilo semantic policy deterministic verification must be host-attested")
    risk_policy = readiness.get("risk_policy")
    if not isinstance(risk_policy, dict):
        raise ValueError("Kwilo semantic policy readiness.risk_policy must be an object")
    gate_order = risk_policy.get("gate_order")
    if not isinstance(gate_order, list) or not gate_order or any(
        not isinstance(phase, str) or phase not in AUTHORIZED_PHASE_PROFILES
        for phase in gate_order
    ) or len(set(gate_order)) != len(gate_order):
        raise ValueError(
            "Kwilo semantic policy risk_policy.gate_order must be a unique "
            "non-empty supported gate list"
        )
    tiers = risk_policy.get("tiers")
    if not isinstance(tiers, dict) or set(tiers) != {"low", "standard", "high"}:
        raise ValueError("Kwilo semantic policy risk_policy.tiers must define low, standard and high")
    for tier, spec in tiers.items():
        tier_gates = spec.get("required_gate_phases") if isinstance(spec, dict) else None
        if not isinstance(tier_gates, list) or not tier_gates or any(
            phase not in gate_order for phase in tier_gates
        ) or len(set(tier_gates)) != len(tier_gates):
            raise ValueError(
                f"Kwilo semantic policy risk tier {tier} has invalid required_gate_phases"
            )
    categories = risk_policy.get("categories")
    if not isinstance(categories, dict) or not categories:
        raise ValueError("Kwilo semantic policy risk_policy.categories must be a non-empty object")
    for category, spec in categories.items():
        if not isinstance(category, str) or not category.strip() or not isinstance(spec, dict):
            raise ValueError("Kwilo semantic policy contains an invalid risk category")
        minimum_tier = spec.get("minimum_tier")
        category_gates = spec.get("required_gate_phases")
        if minimum_tier not in tiers:
            raise ValueError(
                f"Kwilo semantic policy risk category {category} has an invalid minimum_tier"
            )
        if not isinstance(category_gates, list) or any(
            phase not in gate_order for phase in category_gates
        ) or len(set(category_gates)) != len(category_gates):
            raise ValueError(
                f"Kwilo semantic policy risk category {category} has invalid required_gate_phases"
            )
    return policy


def readiness_gate_phases(
    contract: dict[str, Any],
    *,
    allow_legacy: bool = False,
) -> tuple[str, ...]:
    """Return the exact policy-bound readiness gates for *contract*."""
    policy = _semantic_policy()["readiness"]
    workflow_version = str(contract.get("workflow_version") or "").strip()
    if workflow_version != CURRENT_WORKFLOW_VERSION:
        if allow_legacy and workflow_version in LEGACY_WORKFLOW_VERSIONS:
            return tuple(policy["legacy_required_gate_phases"])
        raise ValueError(
            f"governance contract workflow_version must be {CURRENT_WORKFLOW_VERSION!r}"
        )

    risk_tier = str(contract.get("risk_tier") or "").strip().lower()
    risk_policy = policy["risk_policy"]
    tiers = risk_policy["tiers"]
    if risk_tier not in tiers:
        raise ValueError("governance contract risk_tier must be low, standard or high")

    raw_categories = contract.get("change_categories")
    if not isinstance(raw_categories, list) or not raw_categories or any(
        not isinstance(value, str) or not value.strip() for value in raw_categories
    ):
        raise ValueError("governance contract change_categories must be a non-empty string list")
    categories = [value.strip().lower() for value in raw_categories]
    if len(set(categories)) != len(categories):
        raise ValueError("governance contract change_categories must contain unique values")
    category_policy = risk_policy["categories"]
    unknown = sorted(set(categories) - set(category_policy))
    if unknown:
        raise ValueError(
            "governance contract contains unsupported change_categories: "
            + ", ".join(unknown)
        )

    tier_order = {"low": 0, "standard": 1, "high": 2}
    minimum_tier = max(
        (str(category_policy[category]["minimum_tier"]) for category in categories),
        key=tier_order.__getitem__,
    )
    if tier_order[risk_tier] < tier_order[minimum_tier]:
        raise ValueError(
            f"governance contract risk_tier {risk_tier!r} is below the "
            f"{minimum_tier!r} minimum for its change_categories"
        )

    required = set(tiers[risk_tier]["required_gate_phases"])
    for category in categories:
        required.update(category_policy[category]["required_gate_phases"])
    expected = tuple(phase for phase in risk_policy["gate_order"] if phase in required)

    raw_declared = contract.get("required_gate_phases")
    if not isinstance(raw_declared, list) or any(
        not isinstance(value, str) or not value.strip() for value in raw_declared
    ):
        raise ValueError("governance contract required_gate_phases must be a string list")
    declared = tuple(value.strip().lower() for value in raw_declared)
    if len(set(declared)) != len(declared):
        raise ValueError("governance contract required_gate_phases must contain unique values")
    if declared != expected:
        raise ValueError(
            "governance contract required_gate_phases must exactly match "
            f"the {risk_tier} risk policy: {', '.join(expected)}"
        )
    return expected


def _nonempty(value: Any) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _canonical_string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        raise ValueError(f"governance contract field {field} must be a list")
    normalized = [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, set):
        normalized.sort()
    return list(dict.fromkeys(normalized))


def _string_set(value: Any, field: str) -> set[str]:
    return set(_canonical_string_list(value, field))


def _candidate(value: Any, *, required: bool) -> tuple[Optional[str], Optional[str], Optional[str]]:
    if value is None and not required:
        return None, None, None
    if not isinstance(value, dict):
        raise ValueError("candidate_identity must be an object")
    kind = str(value.get("kind") or "").strip().lower()
    identity = str(value.get("value") or "").strip()
    digest = value.get("path_set_digest")
    digest = str(digest).strip() if digest is not None else None
    if kind not in VALID_CANDIDATE_KINDS:
        raise ValueError(f"candidate_identity.kind must be one of {sorted(VALID_CANDIDATE_KINDS)}")
    if not identity:
        raise ValueError("candidate_identity.value is required")
    if kind == "commit-sha" and (len(identity) != 40 or any(c not in "0123456789abcdefABCDEF" for c in identity)):
        raise ValueError("candidate_identity.value must be a full 40-character Git SHA")
    if kind in {"uncommitted-tree-digest", "deployed-artifact-digest"} and not _is_lower_hex_digest(identity):
        raise ValueError("candidate_identity.value must be a lowercase 64-character hex digest")
    if digest is not None and not _is_lower_hex_digest(digest):
        raise ValueError("candidate_identity.path_set_digest must be a lowercase 64-character hex digest")
    return kind, identity, digest or None


def validate_contract(
    assignee: Optional[str],
    contract: Any,
    *,
    allow_legacy: bool = False,
) -> dict[str, Any]:
    """Validate a task contract against the current capability manifest."""
    if not isinstance(contract, dict):
        raise ValueError("governance_contract is required for new Kwilo tasks")
    workflow_version = str(contract.get("workflow_version") or "").strip()
    legacy = allow_legacy and workflow_version in LEGACY_WORKFLOW_VERSIONS
    if not legacy and workflow_version != CURRENT_WORKFLOW_VERSION:
        raise ValueError(
            f"governance contract workflow_version must be {CURRENT_WORKFLOW_VERSION!r}"
        )
    required_fields = _REQUIRED_CONTRACT_FIELDS + (() if legacy else _RISK_CONTRACT_FIELDS)
    missing = [field for field in required_fields if not _nonempty(contract.get(field))]
    # Empty capability/prohibition lists are valid declarations.
    for field in (
        "required_toolsets", "required_parent_skills", "required_connectors",
        "allowed_side_effects", "prohibited_actions",
    ):
        if field in missing and isinstance(contract.get(field), list):
            missing.remove(field)
    phase_value = str(contract.get("phase") or "").strip().lower()
    if "candidate_identity" in missing and phase_value not in REVISION_BOUND_PHASES:
        missing.remove("candidate_identity")
    if missing:
        raise ValueError("governance_contract missing required field(s): " + ", ".join(missing))
    if not assignee:
        raise ValueError("new governed Kwilo tasks require an assignee")

    phase = phase_value
    if phase not in VALID_PHASES:
        raise ValueError(f"phase must be one of {sorted(VALID_PHASES)}")
    authorized_profile = AUTHORIZED_PHASE_PROFILES.get(phase)
    canonical_assignee = str(assignee).strip().lower()
    if authorized_profile and canonical_assignee != authorized_profile:
        raise ValueError(
            f"phase {phase!r} may only be produced by {authorized_profile}"
        )
    _candidate(contract.get("candidate_identity"), required=phase in REVISION_BOUND_PHASES)
    base_revision = str(contract.get("base_revision") or "").strip()
    if phase in REVISION_BOUND_PHASES and (
        len(base_revision) != 40 or any(c not in "0123456789abcdefABCDEF" for c in base_revision)
    ):
        raise ValueError("base_revision must be a full 40-character Git SHA")
    required_gates = readiness_gate_phases(contract, allow_legacy=allow_legacy)

    manifest = _manifest()
    profile_name = str(assignee).strip().lower()
    profile = (manifest.get("profiles") or {}).get(profile_name)
    if not isinstance(profile, dict):
        raise ValueError(f"assignee {assignee!r} has no Kwilo capability manifest entry")
    _attest_effective_profile_capabilities(profile_name, profile)
    for field in ("role", "lane"):
        expected = str(profile.get(field) or "").strip()
        actual = str(contract.get(field) or "").strip()
        if actual != expected:
            raise ValueError(f"{field} mismatch: task requires {actual!r}, profile declares {expected!r}")

    mode = str(contract.get("mode") or "").strip()
    admitted_modes = _string_set(profile.get("configured_modes", []), "profile.configured_modes")
    if mode not in admitted_modes:
        raise ValueError(f"mode {mode!r} is not admitted for profile {assignee}")

    repo = str(contract["repository"]).strip()
    admitted_repos = _string_set(profile.get("repositories", []), "profile.repositories")
    repositories = manifest.get("repositories") or {}
    if not admitted_repos and repo.lower() in {"none", "not-applicable"}:
        repo_key: Optional[str] = None
    else:
        repo_key = repo if repo in repositories else next(
            (key for key, item in repositories.items() if isinstance(item, dict) and item.get("canonical") == repo),
            None,
        )
        if not repo_key or repo_key not in admitted_repos:
            raise ValueError(f"repository {repo!r} is not admitted for profile {assignee}")
        if not str(contract.get("project_id") or "").strip():
            raise ValueError("project_id is required for Kwilo repository work")

    workspace_class = str(contract["workspace_class"]).strip()
    if workspace_class not in _string_set(profile.get("workspace_classes", []), "profile.workspace_classes"):
        raise ValueError(f"workspace_class {workspace_class!r} is not admitted for profile {assignee}")
    workspace_kind = WORKSPACE_KIND_BY_CLASS.get(workspace_class)
    if workspace_kind is None:
        raise ValueError(
            f"workspace_class {workspace_class!r} has no governed workspace-kind binding"
        )

    repository_local_root: Optional[str] = None
    if repo_key:
        repo_entry = repositories[repo_key]
        local_root = str(repo_entry.get("local_root") or "").strip()
        if not local_root:
            raise ValueError(
                f"repository {repo_key!r} has no local_root in the capability manifest"
            )
        local_root_path = Path(local_root).expanduser()
        if not local_root_path.is_absolute():
            raise ValueError(
                f"repository {repo_key!r} local_root must be absolute"
            )
        repository_local_root = str(local_root_path.resolve(strict=False))

    comparisons = (
        ("required_toolsets", "configured_cli_toolsets"),
        ("required_parent_skills", "configured_parent_skills"),
        ("required_connectors", "configured_connectors"),
    )
    canonical_capabilities: dict[str, list[str]] = {}
    for required_field, profile_field in comparisons:
        canonical_required = _canonical_string_list(
            contract.get(required_field, []), required_field
        )
        canonical_capabilities[required_field] = canonical_required
        required = set(canonical_required)
        available = _string_set(profile.get(profile_field, []), f"profile.{profile_field}")
        unavailable = sorted(required - available)
        if unavailable:
            raise ValueError(f"{required_field} unavailable for {assignee}: {', '.join(unavailable)}")

    required_connectors = _string_set(contract.get("required_connectors", []), "required_connectors")
    scopes = contract.get("connector_read_or_mutate_scope")
    if not isinstance(scopes, dict):
        raise ValueError("connector_read_or_mutate_scope must be an object")
    canonical_scopes = {str(key).strip(): str(value).strip().lower() for key, value in scopes.items()}
    if set(canonical_scopes) != required_connectors:
        raise ValueError("connector_read_or_mutate_scope must exactly declare required connectors")
    manifest_scopes = profile.get("connector_read_or_mutate_scope")
    if not isinstance(manifest_scopes, dict):
        raise ValueError(f"profile {assignee} has no connector scope capabilities")
    canonical_manifest_scopes = {
        str(key).strip(): str(value).strip().lower() for key, value in manifest_scopes.items()
    }
    for connector, scope in canonical_scopes.items():
        if scope not in {"read", "mutate"}:
            raise ValueError(f"connector {connector!r} scope must be 'read' or 'mutate'")
        if canonical_manifest_scopes.get(connector) != scope:
            raise ValueError(
                f"connector {connector!r} scope mismatch: task requires {scope!r}, "
                f"profile declares {canonical_manifest_scopes.get(connector)!r}"
            )

    allowed = _string_set(contract.get("allowed_side_effects", []), "allowed_side_effects")
    intended = _string_set(profile.get("intended_side_effects", []), "profile.intended_side_effects")
    prohibited = _string_set(profile.get("prohibited_side_effects", []), "profile.prohibited_side_effects")
    excess = sorted(allowed - intended)
    if excess:
        raise ValueError(f"allowed_side_effects exceed {assignee} authority: {', '.join(excess)}")
    conflict = sorted(allowed & prohibited)
    if conflict:
        raise ValueError(f"allowed_side_effects conflict with profile prohibitions: {', '.join(conflict)}")
    declared_prohibited = _string_set(contract.get("prohibited_actions", []), "prohibited_actions")
    omitted = sorted(prohibited - declared_prohibited)
    if omitted:
        raise ValueError(f"prohibited_actions may not omit profile prohibitions: {', '.join(omitted)}")

    canonical = dict(contract)
    canonical.update(canonical_capabilities)
    canonical["phase"] = phase
    if not legacy:
        canonical["workflow_version"] = CURRENT_WORKFLOW_VERSION
        canonical["risk_tier"] = str(contract["risk_tier"]).strip().lower()
        canonical["change_categories"] = sorted(
            str(value).strip().lower() for value in contract["change_categories"]
        )
        canonical["required_gate_phases"] = list(required_gates)
    canonical["repository"] = repo_key or "none"
    canonical["repository_id"] = (
        str(repositories[repo_key].get("canonical") or repo_key) if repo_key else "none"
    )
    canonical["repository_local_root"] = repository_local_root
    canonical["workspace_kind"] = workspace_kind
    return canonical


def _profile_for_task(assignee: Optional[str]) -> dict[str, Any]:
    profile_name = str(assignee or "").strip().lower()
    profile = (_manifest().get("profiles") or {}).get(profile_name)
    if not isinstance(profile, dict):
        raise ValueError(
            f"assignee {assignee!r} has no Kwilo capability manifest entry"
        )
    return profile


def dispatch_readback_required(
    assignee: Optional[str],
    contract: dict[str, Any],
) -> bool:
    """Whether the live profile requires a separate create→read→admit gate."""
    if str(contract.get("workflow_version") or "").strip() != CURRENT_WORKFLOW_VERSION:
        return False
    return bool(_profile_for_task(assignee).get("requires_dispatch_readback"))


def github_broker_binding(
    assignee: Optional[str],
    contract: dict[str, Any],
) -> Optional[dict[str, str]]:
    """Return the live manifest's broker identity for this repository task."""
    profile = _profile_for_task(assignee)
    persona = str(profile.get("github_broker_persona") or "").strip().lower()
    allowed = set(contract.get("allowed_side_effects") or [])
    requires_broker = "authorised-push" in allowed
    if not persona:
        if requires_broker:
            raise ValueError(
                f"profile {assignee} has GitHub side effects but no broker identity"
            )
        return None
    if any(not (char.isalnum() or char == "-") for char in persona):
        raise ValueError(f"profile {assignee} has an invalid GitHub broker identity")
    repository = str(contract.get("repository_id") or "").strip()
    if (
        not repository
        or repository.lower() in {"none", "not-applicable"}
        or repository.count("/") != 1
        or any(char in repository for char in "\r\n\x00")
    ):
        raise ValueError(
            f"profile {assignee} has GitHub broker authority without a canonical repository"
        )
    return {"persona": persona, "repository": repository}


def _dispatch_snapshot(
    conn: sqlite3.Connection,
    task_id: str,
    semantics: dict[str, Any],
    *,
    canonicalize_workspace_path: bool = True,
) -> dict[str, Any]:
    task = conn.execute(
        """
        SELECT id, title, body, assignee, workspace_kind, workspace_path,
               branch_name, project_id, tenant
          FROM tasks
         WHERE id = ?
        """,
        (task_id,),
    ).fetchone()
    if task is None:
        raise ValueError(f"task {task_id} not found")
    links = conn.execute(
        """
        SELECT parent_id, link_kind, required_phase, required_verdict,
               require_candidate_match
          FROM task_links
         WHERE child_id = ?
         ORDER BY parent_id, link_kind
        """,
        (task_id,),
    ).fetchall()
    task_snapshot = dict(task)
    workspace_path = task_snapshot.get("workspace_path")
    if canonicalize_workspace_path and isinstance(workspace_path, str):
        # Windows workspace resolution may persist the same path with native
        # backslashes after dispatch.  Separator-only canonicalisation is not
        # a contract mutation and must not invalidate a previously admitted
        # card.
        task_snapshot["workspace_path"] = workspace_path.replace("\\", "/")
    return {
        "task": task_snapshot,
        "governance_contract": semantics["contract"],
        "parents": [
            {
                **dict(link),
                "require_candidate_match": bool(link["require_candidate_match"]),
            }
            for link in links
        ],
    }


def dispatch_readback(
    conn: sqlite3.Connection,
    task_id: str,
) -> dict[str, Any]:
    """Return the immutable admission digest produced by a real DB readback."""
    row = conn.execute(
        "SELECT assignee FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"task {task_id} not found")
    semantics = get_task_semantics(conn, task_id)
    if semantics is None:
        return {
            "required": False,
            "admitted": True,
            "digest": None,
            "admitted_at": None,
            "admitted_by": None,
            "snapshot": None,
        }
    required = dispatch_readback_required(row["assignee"], semantics["contract"])
    if not required:
        return {
            "required": False,
            "admitted": True,
            "digest": None,
            "admitted_at": semantics.get("dispatch_readback_at"),
            "admitted_by": semantics.get("dispatch_readback_by"),
            "snapshot": _dispatch_snapshot(conn, task_id, semantics),
        }
    snapshot = _dispatch_snapshot(conn, task_id, semantics)
    payload = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    # Accept admissions persisted by the pre-canonicalisation runtime when
    # the only difference is the native Windows workspace separator. This is
    # a read-only compatibility check: new admissions always persist the
    # canonical digest returned above.
    legacy_payload = json.dumps(
        _dispatch_snapshot(
            conn,
            task_id,
            semantics,
            canonicalize_workspace_path=False,
        ),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    legacy_digest = hashlib.sha256(legacy_payload).hexdigest()
    admitted_digest = str(
        semantics.get("dispatch_readback_digest") or ""
    ).strip()
    compatible_digests = {digest, legacy_digest}
    return {
        "required": True,
        "admitted": admitted_digest in compatible_digests,
        "digest": digest,
        "legacy_digest": legacy_digest if legacy_digest != digest else None,
        "admitted_digest": admitted_digest or None,
        "admitted_at": semantics.get("dispatch_readback_at"),
        "admitted_by": semantics.get("dispatch_readback_by"),
        "snapshot": snapshot,
    }


def persist_dispatch_readback(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    expected_digest: str,
    actor: str,
    now: int,
) -> dict[str, Any]:
    """Persist a digest only when it came from a matching, separate readback."""
    readback = dispatch_readback(conn, task_id)
    if not readback["required"]:
        raise ValueError(f"task {task_id} does not require dispatch readback")
    expected = str(expected_digest or "").strip().lower()
    if expected != readback["digest"]:
        raise ValueError(
            "dispatch readback digest does not match the current persisted card"
        )
    actor_value = str(actor or "").strip()
    if not actor_value:
        raise ValueError("dispatch readback actor is required")
    conn.execute(
        """
        UPDATE task_semantics
           SET dispatch_readback_digest = ?,
               dispatch_readback_at = ?,
               dispatch_readback_by = ?,
               updated_at = ?
         WHERE task_id = ?
        """,
        (expected, int(now), actor_value, int(now), task_id),
    )
    return dispatch_readback(conn, task_id)


def readiness_parent_link_specs(
    conn: sqlite3.Connection,
    contract: dict[str, Any],
    parents: Iterable[str],
) -> dict[str, tuple[str, str, str, bool]]:
    """Return mandatory typed link specs for a new governed readiness task.

    This creation-time invariant deliberately does not scan or retrofit existing
    rows.  Ordinary completion parents may coexist, but can never substitute for
    exact-candidate Sentinel and Tess PASS gates.
    """
    if contract["phase"] not in {"merge-readiness", "release-readiness"}:
        return {}
    expected_candidate = _candidate(contract.get("candidate_identity"), required=True)
    required = set(readiness_gate_phases(contract, allow_legacy=True))
    found: dict[str, str] = {}
    for parent_id in parents:
        semantics = get_task_semantics(conn, parent_id)
        if semantics is None or semantics["phase"] not in required:
            continue
        producer = conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (parent_id,),
        ).fetchone()
        expected_profile = AUTHORIZED_PHASE_PROFILES[semantics["phase"]]
        actual_profile = str(producer["assignee"] or "").strip().lower() if producer else ""
        if actual_profile != expected_profile:
            raise ValueError(
                f"{semantics['phase']} evidence gate must be produced by {expected_profile}"
            )
        parent_candidate = (
            semantics["candidate_kind"],
            semantics["candidate_value"],
            semantics["candidate_paths_digest"],
        )
        if parent_candidate != expected_candidate:
            raise ValueError(
                f"{semantics['phase']} evidence gate must target the exact task candidate"
            )
        found[semantics["phase"]] = parent_id
    missing = sorted(required - set(found))
    if missing:
        raise ValueError(
            "governed readiness requires policy-bound evidence gates; missing: "
            + ", ".join(missing)
        )
    return {
        parent_id: ("evidence-gate", phase, "pass", True)
        for phase, parent_id in found.items()
    }


def insert_task_semantics(
    conn: sqlite3.Connection, task_id: str, contract: dict[str, Any], *, now: int
) -> None:
    candidate_kind, candidate_value, candidate_digest = _candidate(
        contract.get("candidate_identity"), required=contract["phase"] in REVISION_BOUND_PHASES
    )
    conn.execute(
        """INSERT INTO task_semantics (
            task_id, phase, repository_id, project_id, requester, authoriser,
            acceptance_owner, canonical_source, evidence_destination,
            workspace_class, candidate_kind, candidate_value,
            candidate_paths_digest, environment_json, authority_consumed_json,
            side_effects_performed_json, contract_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            task_id, contract["phase"], contract["repository_id"], contract.get("project_id"),
            contract["requester"], contract["authoriser"], contract["acceptance_owner"],
            contract["canonical_source"], contract["evidence_destination"],
            contract["workspace_class"], candidate_kind, candidate_value, candidate_digest,
            json.dumps(contract.get("environment"), ensure_ascii=False) if contract.get("environment") is not None else None,
            json.dumps(contract.get("authority_consumed", []), ensure_ascii=False),
            json.dumps(contract.get("side_effects_performed", []), ensure_ascii=False),
            json.dumps(contract, ensure_ascii=False), int(now),
        ),
    )


def _json_or(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def get_task_semantics(conn: sqlite3.Connection, task_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM task_semantics WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["contract"] = _json_or(result.pop("contract_json", None), {})
    result["environment"] = _json_or(result.pop("environment_json", None), None)
    result["authority_consumed"] = _json_or(result.pop("authority_consumed_json", None), [])
    result["side_effects_performed"] = _json_or(result.pop("side_effects_performed_json", None), [])
    return result


def admission_error(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT assignee, created_at, skills FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None or not is_kwilo_board(conn):
        return None
    semantics = get_task_semantics(conn, task_id)
    if semantics is None:
        # Preserve genuinely historical rows without consulting mutable
        # activation state. Post-default-cutoff rows fail closed when the
        # activation record is unavailable or when their contract is missing.
        default_epoch = int(
            datetime.fromisoformat(DEFAULT_ACTIVATED_AT.replace("Z", "+00:00")).timestamp()
        )
        if int(row["created_at"]) < default_epoch:
            return None
        try:
            if int(row["created_at"]) < activation_epoch():
                return None
        except ValueError as exc:
            return str(exc)
        return "governance contract is missing for a post-activation Kwilo task"
    try:
        validated_contract = validate_contract(
            row["assignee"], semantics["contract"], allow_legacy=True
        )
    except ValueError as exc:
        return str(exc)
    required_skills = set(validated_contract["required_parent_skills"])
    activated_skills = _json_or(row["skills"], [])
    if not isinstance(activated_skills, list):
        activated_skills = []
    activated_skill_names = {
        str(name).strip() for name in activated_skills if str(name).strip()
    }
    missing_skills = sorted(required_skills - activated_skill_names)
    if missing_skills:
        return (
            "contract-required worker skills are not activated: "
            + ", ".join(missing_skills)
        )
    try:
        readback = dispatch_readback(conn, task_id)
    except ValueError as exc:
        return str(exc)
    if readback["required"] and not readback["admitted"]:
        if readback.get("admitted_digest"):
            return "dispatch readback digest changed after admission"
        return "dispatch readback has not been admitted"
    return readiness_integrity_error(conn, task_id)


def mandatory_readiness_link_spec(
    conn: sqlite3.Connection, parent_id: str, child_id: str
) -> Optional[tuple[str, str, str, bool]]:
    """Return the immutable gate spec when an edge is a readiness invariant."""
    parent = get_task_semantics(conn, parent_id)
    child = get_task_semantics(conn, child_id)
    if (
        parent is None
        or child is None
        or child["phase"] not in {"merge-readiness", "release-readiness"}
        or parent["phase"] not in readiness_gate_phases(
            child["contract"], allow_legacy=True
        )
    ):
        return None
    parent_candidate = (
        parent["candidate_kind"], parent["candidate_value"], parent["candidate_paths_digest"],
    )
    child_candidate = (
        child["candidate_kind"], child["candidate_value"], child["candidate_paths_digest"],
    )
    if parent_candidate != child_candidate:
        return None
    return "evidence-gate", parent["phase"], "pass", True


def readiness_integrity_error(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Verify both exact-candidate gates and their authorized producers."""
    child = get_task_semantics(conn, task_id)
    if child is None or child["phase"] not in {"merge-readiness", "release-readiness"}:
        return None
    expected_candidate = (
        child["candidate_kind"], child["candidate_value"], child["candidate_paths_digest"],
    )
    required_phases = set(
        readiness_gate_phases(child["contract"], allow_legacy=True)
    )
    found: set[str] = set()
    rows = conn.execute(
        "SELECT l.*, p.assignee, s.phase, s.candidate_kind, s.candidate_value, "
        "s.candidate_paths_digest FROM task_links l "
        "JOIN tasks p ON p.id = l.parent_id "
        "JOIN task_semantics s ON s.task_id = l.parent_id "
        "WHERE l.child_id = ?",
        (task_id,),
    ).fetchall()
    for link in rows:
        phase = link["phase"]
        if phase not in required_phases:
            continue
        candidate = (
            link["candidate_kind"], link["candidate_value"], link["candidate_paths_digest"],
        )
        exact_spec = (
            link["link_kind"] == "evidence-gate"
            and link["required_phase"] == phase
            and link["required_verdict"] == "pass"
            and bool(link["require_candidate_match"])
        )
        if (
            candidate == expected_candidate
            and exact_spec
            and str(link["assignee"] or "").strip().lower() == AUTHORIZED_PHASE_PROFILES[phase]
        ):
            found.add(phase)
    missing = sorted(required_phases - found)
    if missing:
        details = ", ".join(
            f"{phase} (expected {AUTHORIZED_PHASE_PROFILES[phase]})" for phase in missing
        )
        return (
            "governed readiness requires immutable policy-bound exact-candidate gates; "
            "missing or invalid: " + details
        )
    return None


def validate_link(
    conn: sqlite3.Connection,
    parent_id: str,
    child_id: str,
    *,
    link_kind: str,
    required_phase: Optional[str],
    required_verdict: Optional[str],
    require_candidate_match: bool,
) -> tuple[str, Optional[str], Optional[str], bool]:
    kind = str(link_kind or "completion").strip().lower()
    if kind not in VALID_LINK_KINDS:
        raise ValueError(f"link_kind must be one of {sorted(VALID_LINK_KINDS)}")
    phase = str(required_phase).strip().lower() if required_phase else None
    verdict = str(required_verdict).strip().lower() if required_verdict else None
    if kind == "evidence-gate":
        if phase not in VALID_PHASES:
            raise ValueError("evidence-gate requires a valid required_phase")
        if verdict not in VALID_VERDICTS:
            raise ValueError("evidence-gate requires a valid required_verdict")
        if verdict != "pass":
            raise ValueError("new semantic dependency gates must require verdict 'pass'")
        if not require_candidate_match:
            raise ValueError("new semantic dependency gates must require exact candidate matching")
        if get_task_semantics(conn, parent_id) is None or get_task_semantics(conn, child_id) is None:
            raise ValueError("evidence-gate requires semantic contracts on parent and child")
    else:
        if phase or verdict or require_candidate_match:
            raise ValueError(f"{kind} links cannot declare semantic gate fields")
    return kind, phase, verdict, bool(require_candidate_match)


def _strict_json(value: Any, field: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"stored semantic_evidence {field} is malformed") from exc


def _stored_evidence_payload(row: sqlite3.Row) -> dict[str, Any]:
    candidate = None
    if row["candidate_kind"] is not None or row["candidate_value"] is not None:
        candidate = {
            "kind": row["candidate_kind"], "value": row["candidate_value"],
            "path_set_digest": row["candidate_paths_digest"],
        }
    return {
        "phase": row["phase"], "verdict": row["verdict"],
        "candidate_identity": candidate,
        "environment": _strict_json(row["environment_json"], "environment")
        if row["environment_json"] is not None else None,
        "checks": _strict_json(row["checks_json"], "checks"),
        "blockers": _strict_json(row["blockers_json"], "blockers"),
        "unresolved_acceptance_rows": _strict_json(
            row["unresolved_acceptance_json"], "unresolved_acceptance_rows"
        ),
        "canonical_links": _strict_json(row["canonical_links_json"], "canonical_links"),
        "supersedes_evidence_ids": _strict_json(row["supersedes_json"], "supersedes_evidence_ids"),
        "invalidates_evidence_ids": _strict_json(row["invalidates_json"], "invalidates_evidence_ids"),
    }


def dependency_satisfied(
    conn: sqlite3.Connection, parent_id: str, child_id: str
) -> tuple[bool, str]:
    link = conn.execute(
        "SELECT * FROM task_links WHERE parent_id = ? AND child_id = ?",
        (parent_id, child_id),
    ).fetchone()
    if link is None:
        return False, "missing_link"
    kind = link["link_kind"] if "link_kind" in link.keys() else "completion"
    if kind in {"supervision", "informational"}:
        return True, "non_gating_link"
    parent = conn.execute("SELECT status FROM tasks WHERE id = ?", (parent_id,)).fetchone()
    if parent is None:
        return False, "missing_parent"
    if kind == "completion":
        satisfied = parent["status"] in {"done", "archived"}
        return satisfied, "completion_satisfied" if satisfied else "parent_not_done"
    if kind != "evidence-gate":
        return False, "unknown_link_kind"
    if parent["status"] not in {"done", "archived"}:
        return False, "parent_not_done"

    semantics = get_task_semantics(conn, parent_id)
    if semantics is None:
        return False, "missing_parent_semantics"
    rows = conn.execute(
        "SELECT * FROM task_evidence WHERE task_id = ? ORDER BY id", (parent_id,),
    ).fetchall()
    inactive: set[int] = set()
    canonical_rows: list[tuple[sqlite3.Row, dict[str, Any]]] = []
    try:
        for row in rows:
            expected_producer = AUTHORIZED_PHASE_PROFILES.get(semantics["phase"])
            actual_producer = str(row["producer_profile"] or "").strip().lower()
            if (
                row["producer_task_id"] != parent_id
                or (expected_producer is not None and actual_producer != expected_producer)
            ):
                raise ValueError("stored semantic evidence producer context is invalid")
            canonical, _ = validate_evidence(
                conn, parent_id, semantics, _stored_evidence_payload(row),
                evidence_id=int(row["id"]),
            )
            inactive.update(canonical["supersedes_evidence_ids"])
            inactive.update(canonical["invalidates_evidence_ids"])
            canonical_rows.append((row, canonical))
    except ValueError:
        return False, "malformed_evidence"
    evidence = next((
        row for row, _canonical in reversed(canonical_rows)
        if row["phase"] == link["required_phase"] and int(row["id"]) not in inactive
    ), None)
    if evidence is None:
        return False, "missing_evidence"
    if evidence["verdict"] != link["required_verdict"]:
        return False, "verdict_mismatch"
    if link["require_candidate_match"]:
        child = get_task_semantics(conn, child_id)
        if child is None:
            return False, "missing_child_candidate"
        expected = (child["candidate_kind"], child["candidate_value"], child["candidate_paths_digest"])
        actual = (evidence["candidate_kind"], evidence["candidate_value"], evidence["candidate_paths_digest"])
        if None in expected[:2] or actual != expected:
            return False, "candidate_mismatch"
    return True, "evidence_gate_satisfied"


def all_dependencies_satisfied(conn: sqlite3.Connection, child_id: str) -> bool:
    links = conn.execute("SELECT parent_id FROM task_links WHERE child_id = ?", (child_id,)).fetchall()
    return all(dependency_satisfied(conn, row["parent_id"], child_id)[0] for row in links)


def completion_admission_error(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Revalidate governed authority and every dependency at completion time."""
    error = admission_error(conn, task_id)
    if error:
        return error
    for link in conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id", (task_id,),
    ).fetchall():
        satisfied, reason = dependency_satisfied(conn, link["parent_id"], task_id)
        if not satisfied:
            return (
                f"semantic dependencies are not satisfied: parent {link['parent_id']}: {reason}"
            )
    return None


def _strict_string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"semantic_evidence {field} must be a list")
    if len(value) > _MAX_EVIDENCE_ITEMS:
        raise ValueError(f"semantic_evidence {field} exceeds {_MAX_EVIDENCE_ITEMS} items")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"semantic_evidence {field} items must be non-empty strings")
        text = item.strip()
        if len(text) > _MAX_EVIDENCE_TEXT:
            raise ValueError(f"semantic_evidence {field} item exceeds {_MAX_EVIDENCE_TEXT} characters")
        result.append(text)
    return result


def _canonical_links(value: Any, field: str) -> list[str]:
    links = _strict_string_list(value, field)
    if len(set(links)) != len(links):
        raise ValueError(f"semantic_evidence {field} must contain unique links")
    for link in links:
        parsed = urlsplit(link)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError(f"semantic_evidence {field} must contain valid absolute HTTP(S) links")
    return links


def _count(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_EVIDENCE_ITEMS:
        raise ValueError(
            f"semantic_evidence checks.{field} must be an integer from 0 to {_MAX_EVIDENCE_ITEMS}"
        )
    return value


def _validate_environment(value: Any) -> Optional[dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, dict) or any(not isinstance(key, str) or not key for key in value):
        raise ValueError("semantic_evidence environment must be an object with non-empty string keys")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("semantic_evidence environment must contain finite JSON values") from exc
    if len(encoded) > _MAX_ENVIRONMENT_JSON:
        raise ValueError(
            f"semantic_evidence environment exceeds {_MAX_ENVIRONMENT_JSON} JSON characters"
        )
    return value


def _validate_checks(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("semantic_evidence checks must be an object")
    unknown = sorted(set(value) - _CHECK_FIELDS)
    if unknown:
        raise ValueError("semantic_evidence checks has unknown field(s): " + ", ".join(unknown))
    required = {
        "executed", "passed_count", "failed_count", "skipped_count",
        "host_attested", "canonical_check_links",
    }
    missing = sorted(required - set(value))
    if missing:
        raise ValueError("semantic_evidence checks missing field(s): " + ", ".join(missing))
    executed = _strict_string_list(value["executed"], "checks.executed")
    if len(set(executed)) != len(executed):
        raise ValueError("semantic_evidence checks.executed must contain unique checks")
    links = _canonical_links(value["canonical_check_links"], "checks.canonical_check_links")
    passed = _count(value["passed_count"], "passed_count")
    failed = _count(value["failed_count"], "failed_count")
    skipped = _count(value["skipped_count"], "skipped_count")
    observed = len(executed) if executed else len(links)
    if passed + failed + skipped != observed:
        raise ValueError("semantic_evidence check counts must match executed checks or canonical check links")
    host_attested = value["host_attested"]
    if not isinstance(host_attested, bool):
        raise ValueError("semantic_evidence checks.host_attested must be a boolean")
    skipped_required = _count(value.get("skipped_required_count", skipped), "skipped_required_count")
    policy_safe = value.get("skipped_policy_safe", False)
    if not isinstance(policy_safe, bool):
        raise ValueError("semantic_evidence checks.skipped_policy_safe must be a boolean")
    if skipped_required > skipped:
        raise ValueError("semantic_evidence skipped_required_count cannot exceed skipped_count")
    return {
        "executed": executed, "passed_count": passed, "failed_count": failed,
        "skipped_count": skipped, "skipped_required_count": skipped_required,
        "skipped_policy_safe": policy_safe, "host_attested": host_attested,
        "canonical_check_links": links,
    }


def _reference_ids(
    conn: sqlite3.Connection,
    task_id: str,
    semantics: dict[str, Any],
    value: Any,
    field: str,
    *,
    evidence_id: Optional[int],
) -> list[int]:
    if not isinstance(value, list):
        raise ValueError(f"semantic_evidence {field} must be a list")
    if len(value) > _MAX_EVIDENCE_ITEMS:
        raise ValueError(f"semantic_evidence {field} exceeds {_MAX_EVIDENCE_ITEMS} items")
    if any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in value):
        raise ValueError(f"semantic_evidence {field} must contain positive integer IDs")
    if len(set(value)) != len(value):
        raise ValueError(f"semantic_evidence {field} must contain unique IDs")
    expected = (
        semantics["phase"], semantics["candidate_kind"], semantics["candidate_value"],
        semantics["candidate_paths_digest"],
    )
    for reference in value:
        row = conn.execute("SELECT * FROM task_evidence WHERE id = ?", (reference,)).fetchone()
        if row is None:
            raise ValueError(f"semantic_evidence {field} refers to nonexistent evidence {reference}")
        if row["task_id"] != task_id:
            raise ValueError(f"semantic_evidence {field} refers to cross-task evidence {reference}")
        actual = (
            row["phase"], row["candidate_kind"], row["candidate_value"],
            row["candidate_paths_digest"],
        )
        if actual != expected:
            raise ValueError(f"semantic_evidence {field} refers to cross-phase/candidate evidence {reference}")
        if evidence_id is not None and reference >= evidence_id:
            raise ValueError(f"semantic_evidence {field} may only refer to earlier evidence")
    return list(value)


def _active_prior_evidence(
    conn: sqlite3.Connection,
    task_id: str,
    semantics: dict[str, Any],
    *,
    evidence_id: Optional[int],
) -> list[dict[str, Any]]:
    """Return earlier, still-active evidence for this exact phase/candidate."""
    params: list[Any] = [
        task_id,
        semantics["phase"],
        semantics["candidate_kind"],
        semantics["candidate_value"],
        semantics["candidate_paths_digest"],
    ]
    before = ""
    if evidence_id is not None:
        before = " AND id < ?"
        params.append(evidence_id)
    rows = conn.execute(
        """SELECT * FROM task_evidence
           WHERE task_id = ? AND phase = ?
             AND candidate_kind IS ? AND candidate_value IS ?
             AND candidate_paths_digest IS ?"""
        + before
        + " ORDER BY id",
        params,
    ).fetchall()

    payloads: list[tuple[int, dict[str, Any]]] = []
    inactive: set[int] = set()
    for row in rows:
        payload = _stored_evidence_payload(row)
        payloads.append((int(row["id"]), payload))
        for field in ("supersedes_evidence_ids", "invalidates_evidence_ids"):
            references = payload.get(field, [])
            if isinstance(references, list):
                inactive.update(
                    item
                    for item in references
                    if isinstance(item, int) and not isinstance(item, bool)
                )
    return [payload for row_id, payload in payloads if row_id not in inactive]


def _is_measurable_progress(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    """Compare deterministic correction signals; never infer progress with an LLM."""
    if current["verdict"] == "pass" and previous.get("verdict") != "pass":
        return True

    previous_checks = previous.get("checks")
    current_checks = current.get("checks")
    if not isinstance(previous_checks, dict) or not isinstance(current_checks, dict):
        return False

    previous_blockers = set(previous.get("blockers") or [])
    current_blockers = set(current.get("blockers") or [])
    if current_blockers < previous_blockers:
        return True

    previous_unresolved = set(previous.get("unresolved_acceptance_rows") or [])
    current_unresolved = set(current.get("unresolved_acceptance_rows") or [])
    if current_unresolved < previous_unresolved:
        return True

    previous_failed = previous_checks.get("failed_count")
    current_failed = current_checks.get("failed_count")
    if isinstance(previous_failed, int) and isinstance(current_failed, int):
        if current_failed < previous_failed:
            return True

    previous_skipped = previous_checks.get(
        "skipped_required_count", previous_checks.get("skipped_count")
    )
    current_skipped = current_checks.get(
        "skipped_required_count", current_checks.get("skipped_count")
    )
    if isinstance(previous_skipped, int) and isinstance(current_skipped, int):
        if current_skipped < previous_skipped:
            return True

    checks_not_worse = (
        isinstance(previous_failed, int)
        and isinstance(current_failed, int)
        and current_failed <= previous_failed
        and isinstance(previous_skipped, int)
        and isinstance(current_skipped, int)
        and current_skipped <= previous_skipped
    )
    if checks_not_worse:
        previous_passed = previous_checks.get("passed_count")
        current_passed = current_checks.get("passed_count")
        if (
            isinstance(previous_passed, int)
            and isinstance(current_passed, int)
            and current_passed > previous_passed
        ):
            return True
        previous_executed = set(previous_checks.get("executed") or [])
        current_executed = set(current_checks.get("executed") or [])
        if current_executed > previous_executed:
            return True
        previous_links = set(previous_checks.get("canonical_check_links") or [])
        current_links = set(current_checks.get("canonical_check_links") or [])
        if current_links > previous_links:
            return True
    return False


def _reject_repeated_no_progress(
    conn: sqlite3.Connection,
    task_id: str,
    semantics: dict[str, Any],
    evidence: dict[str, Any],
    *,
    evidence_id: Optional[int],
) -> None:
    """Stop a correction cycle that repeats an active blocker without progress."""
    if evidence["verdict"] == "pass":
        return
    current_blockers = set(evidence["blockers"])
    current_unresolved = set(evidence["unresolved_acceptance_rows"])
    for previous in reversed(
        _active_prior_evidence(
            conn, task_id, semantics, evidence_id=evidence_id
        )
    ):
        repeated_blockers = current_blockers & set(previous.get("blockers") or [])
        repeated_unresolved = current_unresolved & set(
            previous.get("unresolved_acceptance_rows") or []
        )
        if not repeated_blockers and not repeated_unresolved:
            continue
        if _is_measurable_progress(previous, evidence):
            continue
        repeated = sorted(repeated_blockers | repeated_unresolved)
        raise ValueError(
            "semantic_evidence repeats blocker(s) without measurable progress: "
            + ", ".join(repeated)
            + "; stop the correction cycle and use kanban_block with the "
            "root-cause/escalation evidence"
        )


def _validate_high_risk_sentinel_depth(
    semantics: dict[str, Any],
    *,
    phase: str,
    verdict: str,
    environment: Optional[dict[str, Any]],
) -> None:
    """Require one deep Sentinel gate instead of a separate Forge-2 gate."""
    contract = semantics.get("contract") or {}
    if (
        phase != "sentinel-review"
        or verdict != "pass"
        or contract.get("workflow_version") != CURRENT_WORKFLOW_VERSION
        or contract.get("risk_tier") != "high"
    ):
        return
    environment = environment or {}
    if environment.get("review_depth") != "high":
        raise ValueError(
            "high-risk Sentinel PASS evidence requires environment.review_depth='high'"
        )
    if environment.get("agent_adversarial_status") != "pass":
        raise ValueError(
            "high-risk Sentinel PASS evidence requires a passing fresh-agent adversarial review"
        )
    if environment.get("claude_status") not in {"pass", "unavailable"}:
        raise ValueError(
            "high-risk Sentinel PASS evidence requires claude_status 'pass' or 'unavailable'"
        )
    if (
        environment.get("claude_status") == "unavailable"
        and not str(environment.get("claude_unavailable_reason") or "").strip()
    ):
        raise ValueError(
            "high-risk Sentinel PASS evidence requires claude_unavailable_reason "
            "when Claude is unavailable"
        )


def validate_evidence(
    conn: sqlite3.Connection,
    task_id: str,
    semantics: dict[str, Any],
    evidence: Any,
    *,
    evidence_id: Optional[int] = None,
) -> tuple[dict[str, Any], tuple[str, str, Optional[str]]]:
    if not isinstance(evidence, dict):
        raise ValueError("semantic_evidence is required for this governed phase")
    unknown = sorted(set(evidence) - _EVIDENCE_FIELDS)
    if unknown:
        raise ValueError("semantic_evidence has unknown field(s): " + ", ".join(unknown))
    phase = str(evidence.get("phase") or "").strip().lower()
    verdict = str(evidence.get("verdict") or "").strip().lower()
    if phase != semantics["phase"]:
        raise ValueError(f"semantic_evidence phase must be {semantics['phase']!r}")
    if verdict not in VALID_VERDICTS:
        raise ValueError(f"semantic_evidence verdict must be one of {sorted(VALID_VERDICTS)}")
    candidate_value = evidence.get("candidate_identity")
    if candidate_value is not None:
        if not isinstance(candidate_value, dict):
            raise ValueError("semantic_evidence candidate_identity must be an object")
        candidate_unknown = sorted(
            set(candidate_value) - {"kind", "value", "path_set_digest"}
        )
        if candidate_unknown:
            raise ValueError(
                "semantic_evidence candidate_identity has unknown field(s): "
                + ", ".join(candidate_unknown)
            )
        if not isinstance(candidate_value.get("kind"), str) or not isinstance(
            candidate_value.get("value"), str
        ):
            raise ValueError("semantic_evidence candidate_identity kind and value must be strings")
        if candidate_value.get("path_set_digest") is not None and not isinstance(
            candidate_value["path_set_digest"], str
        ):
            raise ValueError(
                "semantic_evidence candidate_identity path_set_digest must be a string or null"
            )
    candidate = _candidate(evidence.get("candidate_identity"), required=phase in REVISION_BOUND_PHASES)
    expected = (semantics["candidate_kind"], semantics["candidate_value"], semantics["candidate_paths_digest"])
    if phase in REVISION_BOUND_PHASES and candidate != expected:
        raise ValueError("semantic_evidence candidate_identity must exactly match the task candidate")
    checks = _validate_checks(evidence.get("checks"))
    blockers = _strict_string_list(evidence.get("blockers"), "blockers")
    unresolved = _strict_string_list(
        evidence.get("unresolved_acceptance_rows"), "unresolved_acceptance_rows"
    )
    links = _canonical_links(evidence.get("canonical_links"), "canonical_links")
    supersedes = _reference_ids(
        conn, task_id, semantics, evidence.get("supersedes_evidence_ids", []),
        "supersedes_evidence_ids", evidence_id=evidence_id,
    )
    invalidates = _reference_ids(
        conn, task_id, semantics, evidence.get("invalidates_evidence_ids", []),
        "invalidates_evidence_ids", evidence_id=evidence_id,
    )
    if set(supersedes) & set(invalidates):
        raise ValueError("semantic_evidence supersedes and invalidates IDs must not overlap")
    if verdict == "pass":
        pass_safe = (
            checks["host_attested"] and checks["passed_count"] >= 1
            and checks["failed_count"] == 0
            and bool(checks["executed"] or checks["canonical_check_links"])
            and checks["skipped_required_count"] == 0
            and (checks["skipped_count"] == 0 or checks["skipped_policy_safe"])
            and not blockers and not unresolved and bool(links)
        )
        if not pass_safe:
            raise ValueError(
                "PASS evidence requires host-attested successful checks, no failed or required "
                "skipped checks, no blockers or unresolved acceptance rows, and valid canonical links"
            )
    environment = _validate_environment(evidence.get("environment"))
    _validate_high_risk_sentinel_depth(
        semantics,
        phase=phase,
        verdict=verdict,
        environment=environment,
    )
    canonical = dict(evidence)
    canonical.update({
        "phase": phase, "verdict": verdict, "checks": checks, "blockers": blockers,
        "unresolved_acceptance_rows": unresolved, "canonical_links": links,
        "environment": environment,
        "supersedes_evidence_ids": supersedes, "invalidates_evidence_ids": invalidates,
    })
    _reject_repeated_no_progress(
        conn, task_id, semantics, canonical, evidence_id=evidence_id
    )
    return canonical, candidate


def insert_evidence(
    conn: sqlite3.Connection,
    task_id: str,
    evidence: dict[str, Any],
    candidate: tuple[Optional[str], Optional[str], Optional[str]],
    *,
    producer_profile: str,
    run_id: Optional[int],
    now: int,
) -> int:
    candidate_kind, candidate_value, candidate_digest = candidate
    cur = conn.execute(
        """INSERT INTO task_evidence (
            task_id, run_id, producer_profile, producer_task_id,
            phase, verdict, candidate_kind, candidate_value,
            candidate_paths_digest, environment_json, checks_json, blockers_json,
            unresolved_acceptance_json, canonical_links_json, supersedes_json,
            invalidates_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            task_id, run_id, str(producer_profile).strip().lower(), task_id,
            evidence["phase"], evidence["verdict"],
            candidate_kind, candidate_value, candidate_digest,
            json.dumps(evidence.get("environment"), ensure_ascii=False) if evidence.get("environment") is not None else None,
            json.dumps(evidence.get("checks", {}), ensure_ascii=False),
            json.dumps(evidence.get("blockers", []), ensure_ascii=False),
            json.dumps(evidence.get("unresolved_acceptance_rows", []), ensure_ascii=False),
            json.dumps(evidence.get("canonical_links", []), ensure_ascii=False),
            json.dumps(evidence.get("supersedes_evidence_ids", []), ensure_ascii=False),
            json.dumps(evidence.get("invalidates_evidence_ids", []), ensure_ascii=False),
            int(now),
        ),
    )
    return int(cur.lastrowid)


def list_task_evidence(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM task_evidence WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()
    result: list[dict[str, Any]] = []
    json_fields = {
        "environment_json": "environment", "checks_json": "checks",
        "blockers_json": "blockers", "unresolved_acceptance_json": "unresolved_acceptance_rows",
        "canonical_links_json": "canonical_links", "supersedes_json": "supersedes_evidence_ids",
        "invalidates_json": "invalidates_evidence_ids",
    }
    for row in rows:
        item = dict(row)
        for source, target in json_fields.items():
            item[target] = _json_or(item.pop(source, None), None if target == "environment" else [])
        result.append(item)
    return result
