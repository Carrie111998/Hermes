"""Planning Files canonical state and Planning→Plane projection helpers.

This module deliberately treats Markdown planning files as the canonical agent
working state.  Plane receives only a compact projection for humans, and human
Plane edits are converted into proposed planning changes instead of mutating the
planning files silently.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

import yaml

TASK_PLAN_SCHEMA = "hermes_task_plan.v1"
FINDINGS_SCHEMA = "hermes_findings.v1"
PROGRESS_SCHEMA = "hermes_progress.v1"
PLANE_PROJECTION_SCHEMA = "hermes_plan_projection.v1"
PROJECTION_STATE_SCHEMA = "hermes_plan_projection_state.v1"

TASK_PLAN_FILE = "task_plan.md"
FINDINGS_FILE = "findings.md"
PROGRESS_FILE = "progress.md"
PROJECTION_STATE_FILE = ".hermes_plan_projection.json"
PROPOSED_CHANGES_FILE = "proposed_plane_changes.md"


class PlanningSchemaError(ValueError):
    """Raised when a planning file is missing required frontmatter."""


class PlaneProjectionClient(Protocol):
    """Minimal Plane adapter surface used by the projection component.

    Production adapters can translate these calls into Plane issue updates or
    comments.  Tests use a fake client, keeping this module independent from any
    specific Plane SDK or credential shape.
    """

    def read_projection(self, project_key: str) -> Mapping[str, Any] | None:
        """Return the current Plane projection payload for *project_key*."""

    def update_projection(self, project_key: str, projection: Mapping[str, Any]) -> None:
        """Replace the compact Plane projection for *project_key*."""


@dataclass(frozen=True)
class PlanningFinding:
    id: str
    title: str
    status: str = "open"
    severity: str | None = None
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanningBlocker:
    id: str
    title: str
    status: str = "open"
    owner: str | None = None
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanningState:
    project_key: str
    project_name: str
    status: str
    current_phase: str
    phases: tuple[Mapping[str, Any], ...]
    next_action: str | None
    resume_point: str | None
    blockers: tuple[PlanningBlocker, ...] = ()
    findings: tuple[PlanningFinding, ...] = ()
    evidence_links: tuple[str, ...] = ()
    source_files: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SyncResult:
    project_key: str
    projection_hash: str
    updated_plane: bool
    proposed_planning_change: bool
    proposal_path: Path | None = None
    reason: str = ""


def canonical_json(data: Mapping[str, Any]) -> str:
    """Return deterministic JSON for hashing and comparisons."""

    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def projection_hash(projection: Mapping[str, Any]) -> str:
    """Hash a Plane projection payload after deterministic normalization."""

    return hashlib.sha256(canonical_json(projection).encode("utf-8")).hexdigest()


def read_planning_state(planning_dir: str | Path) -> PlanningState:
    """Read and validate canonical planning Markdown files from *planning_dir*."""

    root = Path(planning_dir)
    task_meta, _task_body = _read_frontmatter(root / TASK_PLAN_FILE)
    findings_meta, _findings_body = _read_frontmatter(root / FINDINGS_FILE)
    progress_meta, _progress_body = _read_frontmatter(root / PROGRESS_FILE)

    _require_schema(root / TASK_PLAN_FILE, task_meta, TASK_PLAN_SCHEMA)
    _require_schema(root / FINDINGS_FILE, findings_meta, FINDINGS_SCHEMA)
    _require_schema(root / PROGRESS_FILE, progress_meta, PROGRESS_SCHEMA)

    project_key = _required_str(task_meta, "project_key", root / TASK_PLAN_FILE)
    project_name = str(task_meta.get("project_name") or project_key)
    current_phase = _required_str(task_meta, "current_phase", root / TASK_PLAN_FILE)
    status = str(task_meta.get("status") or "active")
    phases = _sequence_of_mappings(task_meta.get("phases"), "phases", root / TASK_PLAN_FILE)
    next_action = _optional_str(progress_meta.get("next_action")) or _optional_str(task_meta.get("next_action"))
    resume_point = _optional_str(progress_meta.get("resume_point"))

    blockers = tuple(_parse_blocker(item, root / FINDINGS_FILE) for item in _as_list(findings_meta.get("blockers")))
    findings = tuple(_parse_finding(item, root / FINDINGS_FILE) for item in _as_list(findings_meta.get("findings")))
    evidence_links = _dedupe_strs(
        [
            *_as_list(task_meta.get("evidence_links")),
            *_as_list(findings_meta.get("evidence_links")),
            *(evidence for blocker in blockers for evidence in blocker.evidence),
            *(evidence for finding in findings for evidence in finding.evidence),
            *_as_list(progress_meta.get("evidence_links")),
        ]
    )

    return PlanningState(
        project_key=project_key,
        project_name=project_name,
        status=status,
        current_phase=current_phase,
        phases=tuple(phases),
        next_action=next_action,
        resume_point=resume_point,
        blockers=blockers,
        findings=findings,
        evidence_links=tuple(evidence_links),
        source_files={
            "task_plan": str((root / TASK_PLAN_FILE).resolve()),
            "findings": str((root / FINDINGS_FILE).resolve()),
            "progress": str((root / PROGRESS_FILE).resolve()),
        },
    )


def build_plane_projection(state: PlanningState) -> dict[str, Any]:
    """Derive the compact, human-visible Plane projection for *state*."""

    active_blockers = [blocker for blocker in state.blockers if blocker.status != "resolved"]
    active_findings = [finding for finding in state.findings if finding.status != "closed"]
    projection = {
        "schema": PLANE_PROJECTION_SCHEMA,
        "project_key": state.project_key,
        "project_name": state.project_name,
        "status": state.status,
        "current_phase": state.current_phase,
        "next_action": state.next_action,
        "resume_point": state.resume_point,
        "blockers": [
            {
                "id": blocker.id,
                "title": blocker.title,
                "status": blocker.status,
                "owner": blocker.owner,
                "evidence": list(blocker.evidence),
            }
            for blocker in active_blockers
        ],
        "findings": [
            {
                "id": finding.id,
                "title": finding.title,
                "status": finding.status,
                "severity": finding.severity,
                "evidence": list(finding.evidence),
            }
            for finding in active_findings
        ],
        "evidence_links": list(state.evidence_links),
        "source_files": dict(state.source_files),
    }
    projection["source_hash"] = projection_hash(projection)
    return projection


def sync_planning_to_plane(
    planning_dir: str | Path,
    client: PlaneProjectionClient,
    *,
    propose_human_plane_changes: bool = True,
) -> SyncResult:
    """Project canonical planning files to Plane idempotently.

    If Plane has diverged from the last projection hash recorded locally, the
    function writes a proposed planning change and does not overwrite Plane.
    That preserves the one-way default while making human Plane edits visible to
    planning workers.
    """

    root = Path(planning_dir)
    state = read_planning_state(root)
    projection = build_plane_projection(state)
    current_hash = str(projection["source_hash"])
    local_projection_state = _read_projection_state(root)
    remote_projection = client.read_projection(state.project_key)

    if _plane_has_human_change(remote_projection, local_projection_state):
        proposal_path = None
        if propose_human_plane_changes:
            proposal_path = write_plane_change_proposal(root, remote_projection, projection)
        return SyncResult(
            project_key=state.project_key,
            projection_hash=current_hash,
            updated_plane=False,
            proposed_planning_change=proposal_path is not None,
            proposal_path=proposal_path,
            reason="plane_changed_by_human",
        )

    if local_projection_state.get("last_applied_hash") == current_hash:
        return SyncResult(
            project_key=state.project_key,
            projection_hash=current_hash,
            updated_plane=False,
            proposed_planning_change=False,
            reason="projection_hash_unchanged",
        )

    if remote_projection and remote_projection.get("source_hash") == current_hash:
        _write_projection_state(root, current_hash)
        return SyncResult(
            project_key=state.project_key,
            projection_hash=current_hash,
            updated_plane=False,
            proposed_planning_change=False,
            reason="plane_already_current",
        )

    client.update_projection(state.project_key, projection)
    _write_projection_state(root, current_hash)
    return SyncResult(
        project_key=state.project_key,
        projection_hash=current_hash,
        updated_plane=True,
        proposed_planning_change=False,
        reason="plane_projection_updated",
    )


def write_plane_change_proposal(
    planning_dir: str | Path,
    remote_projection: Mapping[str, Any] | None,
    local_projection: Mapping[str, Any],
) -> Path:
    """Record a human Plane edit as a proposed planning change."""

    root = Path(planning_dir)
    path = root / PROPOSED_CHANGES_FILE
    remote = dict(remote_projection or {})
    remote_hash = projection_hash(remote) if remote else "missing"
    marker = f"<!-- plane-change:{remote_hash} -->"
    existing = path.read_text(encoding="utf-8") if path.exists() else "# Proposed Plane Changes\n\n"
    if marker in existing:
        return path

    section = "\n".join(
        [
            marker,
            f"## Plane change proposal {datetime.now(timezone.utc).isoformat()}",
            "",
            "Plane changed outside the Planning Files projection. Review this proposal and update task_plan.md, findings.md, or progress.md if the human-visible change should become canonical planning state.",
            "",
            "### Plane readback",
            "```json",
            json.dumps(remote, indent=2, ensure_ascii=False, sort_keys=True),
            "```",
            "",
            "### Current planning projection",
            "```json",
            json.dumps(local_projection, indent=2, ensure_ascii=False, sort_keys=True),
            "```",
            "",
        ]
    )
    path.write_text(existing.rstrip() + "\n\n" + section, encoding="utf-8")
    return path


def _read_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    if not path.is_file():
        raise PlanningSchemaError(f"Missing planning file: {path}")
    text = path.read_text(encoding="utf-8-sig")
    if not text.startswith("---\n"):
        raise PlanningSchemaError(f"{path.name} must start with YAML frontmatter")
    end = text.find("\n---", 4)
    if end == -1:
        raise PlanningSchemaError(f"{path.name} has unterminated YAML frontmatter")
    raw_frontmatter = text[4:end]
    body = text[end + 4 :].lstrip("\n")
    loaded = yaml.safe_load(raw_frontmatter) or {}
    if not isinstance(loaded, dict):
        raise PlanningSchemaError(f"{path.name} frontmatter must be a mapping")
    return loaded, body


def _require_schema(path: Path, frontmatter: Mapping[str, Any], expected: str) -> None:
    actual = frontmatter.get("schema")
    if actual != expected:
        raise PlanningSchemaError(f"{path.name} schema must be {expected!r}, got {actual!r}")


def _required_str(frontmatter: Mapping[str, Any], key: str, path: Path) -> str:
    value = frontmatter.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PlanningSchemaError(f"{path.name} frontmatter requires non-empty {key!r}")
    return value.strip()


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _sequence_of_mappings(value: Any, key: str, path: Path) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise PlanningSchemaError(f"{path.name} frontmatter {key!r} must be a list of mappings")
    return value


def _parse_blocker(item: Any, path: Path) -> PlanningBlocker:
    if not isinstance(item, Mapping):
        raise PlanningSchemaError(f"{path.name} blockers must be mappings")
    return PlanningBlocker(
        id=_required_str(item, "id", path),
        title=_required_str(item, "title", path),
        status=str(item.get("status") or "open"),
        owner=_optional_str(item.get("owner")),
        evidence=tuple(_dedupe_strs(_as_list(item.get("evidence")))),
    )


def _parse_finding(item: Any, path: Path) -> PlanningFinding:
    if not isinstance(item, Mapping):
        raise PlanningSchemaError(f"{path.name} findings must be mappings")
    return PlanningFinding(
        id=_required_str(item, "id", path),
        title=_required_str(item, "title", path),
        status=str(item.get("status") or "open"),
        severity=_optional_str(item.get("severity")),
        evidence=tuple(_dedupe_strs(_as_list(item.get("evidence")))),
    )


def _dedupe_strs(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _read_projection_state(planning_dir: Path) -> dict[str, Any]:
    path = planning_dir / PROJECTION_STATE_FILE
    if not path.exists():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise PlanningSchemaError(f"{path.name} must contain a JSON object")
    return loaded


def _write_projection_state(planning_dir: Path, source_hash: str) -> None:
    path = planning_dir / PROJECTION_STATE_FILE
    payload = {
        "schema": PROJECTION_STATE_SCHEMA,
        "last_applied_hash": source_hash,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def _plane_has_human_change(
    remote_projection: Mapping[str, Any] | None,
    local_projection_state: Mapping[str, Any],
) -> bool:
    if not remote_projection:
        return False
    last_applied_hash = local_projection_state.get("last_applied_hash")
    remote_hash = remote_projection.get("source_hash")
    if last_applied_hash is None:
        return False
    return remote_hash != last_applied_hash


__all__ = [
    "FINDINGS_SCHEMA",
    "PLANE_PROJECTION_SCHEMA",
    "PROGRESS_SCHEMA",
    "TASK_PLAN_SCHEMA",
    "PlaneProjectionClient",
    "PlanningSchemaError",
    "PlanningState",
    "SyncResult",
    "build_plane_projection",
    "canonical_json",
    "projection_hash",
    "read_planning_state",
    "sync_planning_to_plane",
    "write_plane_change_proposal",
]
