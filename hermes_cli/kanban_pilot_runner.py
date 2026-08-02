"""Controlled manifest runner for disposable Workflow v1 Phase 1/2 pilots.

The runner deliberately owns only local execution-kernel preparation.  It does
not enable dispatch, launch workers, push branches, create PRs, mutate Projects,
merge, release, or deploy.  Those separately-authorized controller operations
must use the prepared task identities and the existing fenced runtime seams.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

from hermes_cli.kanban_execution import (
    LeafSpec,
    get_workflow_controller_state,
    register_execution_leaf,
)
from hermes_cli.kanban_workflow_runtime import materialize_context_capsule

_SCHEMA = "hermes.workflow-pilot.v1"
_LOGICAL_KEY_RE = re.compile(r"[A-Za-z0-9_.-]+/v[1-9][0-9]*\Z")


class PilotSafetyError(ValueError):
    """The pilot manifest or live repository violates a fail-closed control."""


def _text(value: object, name: str) -> str:
    result = str(value or "").strip()
    if not result or any(ord(char) < 32 for char in result):
        raise PilotSafetyError(f"{name} is required")
    return result


def _texts(value: object, name: str, *, required: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise PilotSafetyError(f"{name} must be a list")
    result = tuple(_text(item, name) for item in value)
    if required and not result:
        raise PilotSafetyError(f"{name} must not be empty")
    if len(set(result)) != len(result):
        raise PilotSafetyError(f"{name} contains duplicates")
    return result


def _relative(value: object, name: str) -> str:
    result = _text(value, name)
    path = PurePosixPath(result)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise PilotSafetyError(f"{name} must be repository-relative")
    return path.as_posix()


def _branch(value: object) -> str:
    result = _text(value, "leaf.branch")
    if (
        not result.startswith("pilot/")
        or result.startswith("-")
        or result.endswith(("/", ".", ".lock"))
        or ".." in result
        or "@{" in result
        or "\\" in result
        or any(char.isspace() or ord(char) < 32 for char in result)
        or any(char in result for char in "~^:?*[")
    ):
        raise PilotSafetyError("leaf.branch must be a safe pilot/* Git branch name")
    if any(
        not segment or segment.startswith(".") or segment.endswith((".", ".lock"))
        for segment in result.split("/")
    ):
        raise PilotSafetyError("leaf.branch must be a safe pilot/* Git branch name")
    return result


def _paths_overlap(left: str, right: str) -> bool:
    left_prefix = left.split("*", 1)[0].rstrip("/")
    right_prefix = right.split("*", 1)[0].rstrip("/")
    nested = left_prefix.startswith(right_prefix + "/") or right_prefix.startswith(
        left_prefix + "/"
    )
    wildcard_overlap = "*" in left + right and (
        left_prefix.startswith(right_prefix) or right_prefix.startswith(left_prefix)
    )
    return left == right or nested or wildcard_overlap


def _git(repo: Path, *args: str) -> str:
    try:
        return subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(repo),
                *args,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise PilotSafetyError(f"git {' '.join(args)} failed for {repo}") from exc


def assert_runner_source(repo_root: str | Path, expected_tree: str) -> None:
    """Require execution from the exact clean source tree that passed review."""

    root = Path(repo_root).resolve()
    expected = str(expected_tree or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40,64}", expected):
        raise PilotSafetyError(
            "runner source tree must be a full hexadecimal object id"
        )
    if not root.is_dir() or _git(root, "rev-parse", "--show-toplevel") != str(root):
        raise PilotSafetyError(
            "runner source root must be the exact Git repository root"
        )
    if _git(root, "status", "--porcelain=v1", "--untracked-files=normal"):
        raise PilotSafetyError("runner source is not clean")
    observed = _git(root, "rev-parse", "HEAD^{tree}").lower()
    if observed != expected:
        raise PilotSafetyError(
            f"runner is not the reviewed source tree: expected {expected}, observed {observed}"
        )


@dataclass(frozen=True)
class PilotLeaf:
    leaf_id: str
    version: int
    phase: int
    objective: str
    allowed_paths: tuple[str, ...]
    relevant_files: tuple[str, ...]
    symbols: tuple[str, ...]
    acceptance_checks: tuple[str, ...]
    depends_on: tuple[str, ...]
    exclusions: tuple[str, ...]
    hazards: tuple[str, ...]
    branch: str | None
    worktree: str | None
    dispatchable: bool
    first_evidence_seconds: int
    wall_clock_budget_seconds: int

    @property
    def logical_key(self) -> str:
        return f"{self.leaf_id}/v{self.version}"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "PilotLeaf":
        try:
            phase = int(raw.get("phase", 0))
            version = int(raw.get("version", 0))
            first_evidence = int(raw.get("first_evidence_seconds", 300))
            wall_clock = int(raw.get("wall_clock_budget_seconds", 3600))
        except (TypeError, ValueError) as exc:
            raise PilotSafetyError("leaf numeric controls must be integers") from exc
        if phase not in {1, 2} or version < 1:
            raise PilotSafetyError(
                "leaf phase must be 1 or 2 and version must be positive"
            )
        if first_evidence < 60 or wall_clock < first_evidence:
            raise PilotSafetyError("leaf evidence/wall-clock budgets are invalid")
        leaf_id = _text(raw.get("id"), "leaf.id")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", leaf_id):
            raise PilotSafetyError("leaf.id contains unsupported characters")
        branch_value = raw.get("branch")
        worktree_value = raw.get("worktree")
        branch = _branch(branch_value) if branch_value is not None else None
        worktree = (
            _relative(worktree_value, "leaf.worktree")
            if worktree_value is not None
            else None
        )
        dispatchable = bool(raw.get("dispatchable", phase == 1))
        if dispatchable and (not branch or not worktree):
            raise PilotSafetyError("dispatchable leaves require a branch and worktree")
        if not dispatchable and (branch or worktree):
            raise PilotSafetyError(
                "non-dispatchable leaves must not provision a branch or worktree"
            )
        allowed = tuple(
            _relative(item, "leaf.allowed_paths")
            for item in _texts(raw.get("allowed_paths"), "leaf.allowed_paths")
        )
        if any(any(magic in path for magic in "*?[") for path in allowed):
            raise PilotSafetyError(
                "controlled pilot allowed_paths must be exact paths, not globs"
            )
        relevant = tuple(
            _relative(item, "leaf.relevant_files")
            for item in _texts(raw.get("relevant_files"), "leaf.relevant_files")
        )
        depends = _texts(raw.get("depends_on", []), "leaf.depends_on", required=False)
        if any(not _LOGICAL_KEY_RE.fullmatch(item) for item in depends):
            raise PilotSafetyError(
                "leaf.depends_on must contain logical leaf/version keys"
            )
        return cls(
            leaf_id=leaf_id,
            version=version,
            phase=phase,
            objective=_text(raw.get("objective"), "leaf.objective"),
            allowed_paths=allowed,
            relevant_files=relevant,
            symbols=_texts(raw.get("symbols"), "leaf.symbols"),
            acceptance_checks=_texts(
                raw.get("acceptance_checks"), "leaf.acceptance_checks"
            ),
            depends_on=depends,
            exclusions=_texts(
                raw.get(
                    "exclusions",
                    ["No successor, merge, release, or deployment authority."],
                ),
                "leaf.exclusions",
            ),
            hazards=_texts(
                raw.get("hazards", ["Disposable pilot; stop on invariant failure."]),
                "leaf.hazards",
            ),
            branch=branch,
            worktree=worktree,
            dispatchable=dispatchable,
            first_evidence_seconds=first_evidence,
            wall_clock_budget_seconds=wall_clock,
        )


@dataclass(frozen=True)
class PilotPlan:
    repository: str
    issue: str
    board: str
    source_path: Path
    pin_sha: str
    worktree_root: Path
    concurrency: int
    permit: str
    leaves: tuple[PilotLeaf, ...]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "PilotPlan":
        if raw.get("schema") != _SCHEMA:
            raise PilotSafetyError(f"schema must be {_SCHEMA}")
        campaign = raw.get("campaign")
        source = raw.get("source")
        controls = raw.get("controls")
        leaves_raw = raw.get("leaves")
        if (
            not isinstance(campaign, Mapping)
            or not isinstance(source, Mapping)
            or not isinstance(controls, Mapping)
        ):
            raise PilotSafetyError("campaign, source, and controls must be objects")
        if not isinstance(leaves_raw, list):
            raise PilotSafetyError("leaves must be a list")
        leaves = tuple(
            PilotLeaf.from_mapping(item)
            for item in leaves_raw
            if isinstance(item, Mapping)
        )
        if len(leaves) != len(leaves_raw):
            raise PilotSafetyError("every leaf must be an object")
        keys = [leaf.logical_key for leaf in leaves]
        if len(set(keys)) != len(keys):
            raise PilotSafetyError("leaf logical keys must be unique")
        phase_one = [leaf for leaf in leaves if leaf.phase == 1]
        phase_two = [leaf for leaf in leaves if leaf.phase == 2]
        if len(phase_one) != 2 or len(phase_two) != 1:
            raise PilotSafetyError(
                "Workflow v1 pilot requires exactly two Phase 1 leaves and one Phase 2 leaf"
            )
        if (
            any(not leaf.dispatchable for leaf in phase_one)
            or phase_two[0].dispatchable
        ):
            raise PilotSafetyError(
                "Phase 1 leaves must be dispatchable and initial Phase 2 must be blocked"
            )
        phase_one_keys = {leaf.logical_key for leaf in phase_one}
        if set(phase_two[0].depends_on) != phase_one_keys:
            raise PilotSafetyError(
                "Phase 2 must depend on every Phase 1 leaf and no other leaf"
            )
        known = set(keys)
        if any(dep not in known for leaf in leaves for dep in leaf.depends_on):
            raise PilotSafetyError("leaf dependency references an unknown logical key")
        phases = {leaf.logical_key: leaf.phase for leaf in leaves}
        if any(
            phases[dependency] >= leaf.phase
            for leaf in leaves
            for dependency in leaf.depends_on
        ):
            raise PilotSafetyError("leaf dependencies must point to an earlier phase")
        # Conservative overlap detection: exact paths, parent/child paths, and
        # glob-bearing prefixes all fail closed rather than guessing disjointness.
        alpha_paths = phase_one[0].allowed_paths
        beta_paths = phase_one[1].allowed_paths
        for left in alpha_paths:
            for right in beta_paths:
                if _paths_overlap(left, right):
                    raise PilotSafetyError(
                        "Phase 1 allowed paths must be path-disjoint"
                    )
        try:
            concurrency = int(controls.get("concurrency", 0))
        except (TypeError, ValueError) as exc:
            raise PilotSafetyError("controls.concurrency must be an integer") from exc
        if concurrency != 2:
            raise PilotSafetyError("Workflow v1 Phase 1 concurrency must be exactly 2")
        source_value = Path(_text(source.get("path"), "source.path"))
        worktree_value = Path(
            _text(source.get("worktree_root"), "source.worktree_root")
        )
        if not source_value.is_absolute() or not worktree_value.is_absolute():
            raise PilotSafetyError("source.path and worktree_root must be absolute")
        source_path = source_value.resolve()
        worktree_root = worktree_value.resolve()
        if worktree_root == source_path:
            raise PilotSafetyError("worktree_root must differ from source.path")
        pin_sha = _text(source.get("pin_sha"), "source.pin_sha").lower()
        if not re.fullmatch(r"[0-9a-f]{40,64}", pin_sha):
            raise PilotSafetyError(
                "source.pin_sha must be a full hexadecimal object id"
            )
        repository = _text(campaign.get("repository"), "campaign.repository").lower()
        issue = _text(campaign.get("issue"), "campaign.issue")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise PilotSafetyError("campaign.repository must be a canonical owner/name")
        if not issue.isdecimal() or int(issue) < 1:
            raise PilotSafetyError("campaign.issue must be a positive issue number")
        board = _text(campaign.get("board"), "campaign.board")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", board):
            raise PilotSafetyError("campaign.board must be a safe board slug")
        return cls(
            repository=repository,
            issue=str(int(issue)),
            board=board,
            source_path=source_path,
            pin_sha=pin_sha,
            worktree_root=worktree_root,
            concurrency=concurrency,
            permit=_text(controls.get("permit"), "controls.permit"),
            leaves=leaves,
        )

    @classmethod
    def load(cls, path: str | Path) -> "PilotPlan":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PilotSafetyError("pilot manifest is not readable UTF-8 JSON") from exc
        if not isinstance(raw, Mapping):
            raise PilotSafetyError("pilot manifest root must be an object")
        return cls.from_mapping(raw)


@dataclass(frozen=True)
class PreparedPilot:
    pin_sha: str
    task_ids: Mapping[str, str]
    manifest_digest: str


def _prepare_worktree(plan: PilotPlan, leaf: PilotLeaf) -> Path:
    assert leaf.branch and leaf.worktree
    target = (plan.worktree_root / leaf.worktree).resolve()
    try:
        target.relative_to(plan.worktree_root)
    except ValueError as exc:
        raise PilotSafetyError("leaf worktree escapes worktree_root") from exc
    branch_ref = f"refs/heads/{leaf.branch}"
    branch_exists = (
        subprocess.run(
            [
                "git",
                "-C",
                str(plan.source_path),
                "show-ref",
                "--verify",
                "--quiet",
                branch_ref,
            ],
            check=False,
        ).returncode
        == 0
    )
    if (
        branch_exists
        and _git(plan.source_path, "rev-parse", f"{leaf.branch}^{{commit}}")
        != plan.pin_sha
    ):
        raise PilotSafetyError(
            f"existing pilot branch is not at the frozen pin: {leaf.branch}"
        )
    if target.exists():
        if (
            _git(target, "rev-parse", "HEAD") != plan.pin_sha
            or _git(target, "branch", "--show-current") != leaf.branch
        ):
            raise PilotSafetyError(
                f"existing worktree does not match frozen leaf: {target}"
            )
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    args = ["worktree", "add"]
    if not branch_exists:
        args.extend(["-b", leaf.branch])
    args.append(str(target))
    args.append(leaf.branch if branch_exists else plan.pin_sha)
    _git(plan.source_path, *args)
    if _git(target, "rev-parse", "HEAD") != plan.pin_sha:
        raise PilotSafetyError("new worktree did not land on the frozen pin")
    return target


def prepare_pilot(
    conn: sqlite3.Connection,
    plan: PilotPlan,
    *,
    assignee: str = "coder",
) -> PreparedPilot:
    """Validate and register a paused pilot without authorizing any launch."""

    active_board = os.environ.get("HERMES_KANBAN_BOARD", "").strip()
    if active_board != plan.board:
        raise PilotSafetyError(
            "manifest board does not match the active Kanban board database"
        )

    if not plan.source_path.is_dir() or _git(
        plan.source_path, "rev-parse", "--show-toplevel"
    ) != str(plan.source_path):
        raise PilotSafetyError("source.path must be the exact Git repository root")
    observed_pin = _git(plan.source_path, "rev-parse", "HEAD^{commit}").lower()
    if observed_pin != plan.pin_sha:
        raise PilotSafetyError(
            f"source drift: expected {plan.pin_sha}, observed {observed_pin}"
        )
    state = get_workflow_controller_state(conn)
    if state.dispatch_enabled or state.broker_ready:
        raise PilotSafetyError(
            "pilot preparation requires dispatch and broker gates to remain off"
        )

    canonical_plan = {
        "schema": _SCHEMA,
        "repository": plan.repository,
        "issue": plan.issue,
        "board": plan.board,
        "pin_sha": plan.pin_sha,
        "permit": plan.permit,
        "leaves": [leaf.__dict__ for leaf in plan.leaves],
    }
    manifest_digest = hashlib.sha256(
        json.dumps(
            canonical_plan, sort_keys=True, separators=(",", ":"), default=list
        ).encode("utf-8")
    ).hexdigest()
    task_ids: dict[str, str] = {}
    for leaf in sorted(plan.leaves, key=lambda item: (item.phase, item.logical_key)):
        parents = tuple(task_ids[key] for key in leaf.depends_on)
        workspace = (
            _prepare_worktree(plan, leaf) if leaf.dispatchable else plan.source_path
        )
        spec = LeafSpec(
            repository=plan.repository,
            campaign_issue=plan.issue,
            leaf_id=leaf.leaf_id,
            version=leaf.version,
            objective=leaf.objective,
            exclusions=leaf.exclusions,
            allowed_paths=leaf.allowed_paths,
            dependencies=parents,
            acceptance_checks=leaf.acceptance_checks,
            hazards=leaf.hazards,
            human_gates=(),
            pin_sha=plan.pin_sha,
            first_evidence_seconds=leaf.first_evidence_seconds,
            wall_clock_budget_seconds=leaf.wall_clock_budget_seconds,
        )
        expected_decisions = (
            f"Controlled pilot permit: {plan.permit}",
            f"Manifest SHA-256: {manifest_digest}",
            "Preparation does not authorize dispatch, GitHub mutation, merge, release, or deployment.",
        )
        existing = conn.execute(
            "SELECT id, body, workspace_path, branch_name FROM tasks WHERE leaf_key=?",
            (spec.leaf_key,),
        ).fetchone()
        if existing is not None:
            try:
                envelope = json.loads(existing["body"] or "")
                expected_branch = leaf.branch or _git(
                    workspace, "branch", "--show-current"
                )
                same_manifest = (
                    json.dumps(
                        envelope.get("spec"), sort_keys=True, separators=(",", ":")
                    )
                    == json.dumps(spec.payload(), sort_keys=True, separators=(",", ":"))
                    and tuple(
                        envelope.get("capsule", {}).get("governing_decisions", ())
                    )
                    == expected_decisions
                    and Path(existing["workspace_path"]).resolve()
                    == workspace.resolve()
                    and existing["branch_name"] == expected_branch
                )
            except (TypeError, ValueError, OSError):
                same_manifest = False
            if not same_manifest:
                raise PilotSafetyError(
                    "pilot permit or manifest drift for an existing leaf"
                )
            task_ids[leaf.logical_key] = str(existing["id"])
            continue
        capsule = materialize_context_capsule(
            plan.source_path,
            spec=spec,
            relevant_files=leaf.relevant_files,
            symbols=leaf.symbols,
            governing_decisions=expected_decisions,
            base_assumptions=(f"Frozen source is {plan.pin_sha}.",),
        )
        try:
            task_id = register_execution_leaf(
                conn,
                spec=spec,
                capsule=capsule,
                assignee=assignee,
                workspace_path=workspace,
                parents=parents,
                board=plan.board,
                branch_name=leaf.branch,
            )
        except ValueError as exc:
            if "collision with different execution identity" in str(exc):
                raise PilotSafetyError(
                    "pilot permit or manifest drift for an existing leaf"
                ) from exc
            raise
        task_ids[leaf.logical_key] = task_id
    return PreparedPilot(plan.pin_sha, task_ids, manifest_digest)
