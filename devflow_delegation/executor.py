"""Synthetic-only Stage-2 executor for the DevFlow Delegation Plane.

This module has deliberately narrow authority. A tick considers only an
operator-approved ``PLANNED`` request from the explicit source kind and only a
target that is both an enabled synthetic fixture and configured for PR creation.
It creates an isolated worktree, invokes an allowlist-owned argv command with
request metadata at ``DDP_REQUEST_PATH``, validates a scoped diff, commits and
pushes the branch, then requires a real injected PR result before it can reach
``MERGE_PENDING``.

There is no merge, deploy, cron, gateway, or live-checkout authority here.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Protocol, Sequence

from devflow_delegation.allowlist import Allowlist, TargetConfig, path_allowed, resolve_target
from devflow_delegation.contract import SEVERITY_RANK
from devflow_delegation.ledger import DelegationLedger
from devflow_delegation.lifecycle import IllegalTransitionError, transition
from devflow_delegation.validator import MAX_OUTPUT_CHARS, validate_worktree
from hermes_cli._subprocess_compat import run_text_capture

logger = logging.getLogger(__name__)

EXECUTOR_ALLOWED_SOURCES = frozenset({"explicit"})
_RISK_CEILING_RANK = {"low": 1, "medium": 2, "high": 3}
_METADATA_RELATIVE_PATH = ".ddp_request.json"


class ExecutorError(RuntimeError):
    """An executor safety gate or subprocess check failed."""


class PrClient(Protocol):
    """PR creation is injected so tests never need a network client."""

    def create_pr(
        self,
        *,
        worktree_path: Path,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
        repo: str,
        label: str = "",
    ) -> Dict[str, Any]:
        ...


class GhPrClient:
    """Explicit real PR client; callers must inject it deliberately."""

    def create_pr(
        self,
        *,
        worktree_path: Path,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
        repo: str,
        label: str = "",
    ) -> Dict[str, Any]:
        argv = [
            "gh", "pr", "create", "--repo", repo, "--base", base_branch,
            "--head", branch, "--title", title, "--body", body,
        ]
        if label:
            argv += ["--label", label]
        created = _run_checked(
            argv,
            cwd=worktree_path,
            timeout_seconds=60,
            label="gh pr create",
        )
        del created
        viewed = _run_checked(
            ["gh", "pr", "view", branch, "--repo", repo, "--json", "number,url,title,state"],
            cwd=worktree_path,
            timeout_seconds=30,
            label="gh pr view",
        )
        try:
            result = json.loads(viewed.stdout)
        except ValueError as exc:
            raise ExecutorError("gh pr view returned invalid JSON") from exc
        if not isinstance(result, dict) or not str(result.get("url") or "").strip() or not result.get("number"):
            raise ExecutorError("gh did not return a PR number and URL")
        return result


def _bounded(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value or "")
    return text if len(text) <= MAX_OUTPUT_CHARS else text[:MAX_OUTPUT_CHARS] + "\n… [output truncated]"


def _run_checked(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    label: str,
    env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    """Run an allowlist-owned argv vector and reject every nonzero outcome."""
    try:
        # run_text_capture, not capture_output=True: these argv vectors spawn
        # grandchildren as a matter of course — `git push` forks a credential
        # helper or ssh, `gh` forks git — and on Windows a grandchild inherits
        # the capture pipes and holds their write end open, so `timeout` never
        # fires: subprocess.run kills only the direct child, then blocks
        # re-draining a pipe that can no longer reach EOF. Capturing into temp
        # files removes the pipes, and with them the hang.
        result = run_text_capture(
            list(argv), cwd=str(cwd), env=env, timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExecutorError(f"{label} timed out: {_bounded(exc.stderr)}") from exc
    except OSError as exc:
        raise ExecutorError(f"{label} could not start: {exc}") from exc
    if result.returncode != 0:
        detail = _bounded(result.stderr) or _bounded(result.stdout)
        raise ExecutorError(f"{label} failed ({result.returncode}): {detail}")
    return result


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _worktree_name(request_id: str, attempt: int) -> str:
    # Request IDs are generated by the contract. Retain only safe branch chars
    # so corrupted imported rows cannot change the worktree boundary.
    safe = "".join(char for char in request_id if char.isascii() and (char.isalnum() or char in "-_"))
    return f"ddp-{safe.removeprefix('dwr_')[:20] or 'request'}-a{attempt}"


def _validate_target_boundary(target: TargetConfig) -> tuple[Path, Path]:
    """Return safe checkout/worktree paths or raise before any mutation."""
    if not target.executor_enabled:
        raise ExecutorError("executor is disabled for target")
    if target.live_gateway_imports:
        raise ExecutorError("executor refuses live-gateway targets")
    if not (target.synthetic_fixture or target.canary_real):
        raise ExecutorError("target is neither a synthetic fixture nor an allowlisted real canary")
    if target.max_autonomous_action != "create_pr":
        raise ExecutorError("target does not permit PR creation")
    if not target.implementation_command:
        raise ExecutorError("target has no implementation command")
    if not target.github_repo:
        raise ExecutorError("target has no GitHub repository identifier")
    if not target.worktree_base:
        raise ExecutorError("target has no explicit worktree base")
    if not target.allowed_globs:
        raise ExecutorError("target has no allowed globs")
    if target.canary_real and target.pr_budget < 1:
        raise ExecutorError("canary target has no PR budget")

    checkout_path = Path(target.checkout_path).expanduser().resolve()
    worktree_base = Path(target.worktree_base).expanduser().resolve()
    if not checkout_path.is_dir():
        raise ExecutorError("synthetic checkout directory is missing")
    # A fixture must be neither the live Hermes root nor nested inside it. This
    # is a defense-in-depth gate in addition to synthetic_fixture=true.
    from events import paths

    live_root = Path(paths.get_default_hermes_root()).expanduser().resolve()
    if checkout_path == live_root or _path_is_within(checkout_path, live_root):
        raise ExecutorError("executor refuses the live Hermes checkout")
    if checkout_path == worktree_base or _path_is_within(worktree_base, checkout_path) or _path_is_within(checkout_path, worktree_base):
        raise ExecutorError("worktree base must be distinct from the checkout")
    _run_checked(
        ["git", "rev-parse", "--is-inside-work-tree"], cwd=checkout_path,
        timeout_seconds=15, label="verify synthetic checkout",
    )
    return checkout_path, worktree_base


def _create_worktree(checkout_path: Path, worktree_base: Path, branch: str, base_branch: str) -> Path:
    worktree_base.mkdir(parents=True, exist_ok=True)
    worktree_path = worktree_base / branch
    if worktree_path.exists():
        raise ExecutorError(f"worktree path already exists: {worktree_path}")
    # 300s, not 45s: `git worktree add` populates the tree with a child
    # `git checkout`/`reset`, and a full checkout of this repo was MEASURED at
    # 84.8s (7329 files) — so 45s failed every invocation on a repo this size.
    # The capture side is already safe (_run_checked uses run_text_capture, so
    # the checkout grandchild cannot defeat the budget); only the budget itself
    # was wrong, which made this a clean-but-certain failure rather than a hang.
    _run_checked(
        ["git", "worktree", "add", str(worktree_path), "-b", branch, base_branch],
        cwd=checkout_path, timeout_seconds=300, label="git worktree add",
    )
    try:
        _run_checked(
            ["git", "worktree", "lock", "--reason", f"ddp executor {branch}", str(worktree_path)],
            cwd=checkout_path, timeout_seconds=15, label="git worktree lock",
        )
    except Exception:
        _remove_worktree(checkout_path, worktree_path, branch)
        raise
    return worktree_path


def _remove_worktree(checkout_path: Path, worktree_path: Path, branch: str) -> None:
    """Best-effort cleanup of a synthetic worktree. Never touches live paths."""
    if not worktree_path.exists():
        return
    try:
        _run_checked(
            ["git", "worktree", "unlock", str(worktree_path)], cwd=checkout_path,
            timeout_seconds=15, label="git worktree unlock",
        )
        _run_checked(
            ["git", "worktree", "remove", "--force", str(worktree_path)], cwd=checkout_path,
            timeout_seconds=30, label="git worktree remove",
        )
        _run_checked(
            ["git", "branch", "--delete", "--force", branch], cwd=checkout_path,
            timeout_seconds=15, label="git branch delete",
        )
    except Exception:
        logger.exception("failed to clean synthetic worktree %s", worktree_path)


def _write_metadata(worktree_path: Path, row: Dict[str, Any]) -> Path:
    try:
        envelope = json.loads(row["envelope_json"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ExecutorError("planned request has invalid envelope metadata") from exc
    path = worktree_path / _METADATA_RELATIVE_PATH
    path.write_text(
        json.dumps({"request_id": row["request_id"], "request": envelope}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _run_implementation(worktree_path: Path, target: TargetConfig, metadata_path: Path) -> None:
    command = target.implementation_command
    if not command:
        raise ExecutorError("target has no implementation command")
    env = os.environ.copy()
    env.update({"GIT_TERMINAL_PROMPT": "0", "DDP_REQUEST_PATH": str(metadata_path)})
    result = _run_checked(
        command, cwd=worktree_path, timeout_seconds=target.command_timeout_seconds,
        label="implementation command", env=env,
    )
    # Keep no unbounded build log in the control plane. An observable command
    # result is mandatory, just as it is for validation.
    if not (result.stdout or result.stderr).strip():
        raise ExecutorError("implementation command produced no observable output")


def _nul_paths(output: str) -> set[str]:
    return {path.replace("\\", "/").strip("/") for path in output.split("\0") if path}


def _changed_paths(worktree_path: Path) -> set[str]:
    tracked = _run_checked(
        ["git", "diff", "--name-only", "-z", "HEAD"], cwd=worktree_path,
        timeout_seconds=20, label="inspect tracked diff",
    )
    untracked = _run_checked(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=worktree_path,
        timeout_seconds=20, label="inspect untracked diff",
    )
    paths = _nul_paths(tracked.stdout) | _nul_paths(untracked.stdout)
    paths.discard(_METADATA_RELATIVE_PATH)
    return paths


def _validate_changed_paths(target: TargetConfig, paths: Iterable[str]) -> list[str]:
    unique = sorted(set(paths))
    if not unique:
        raise ExecutorError("implementation command produced no meaningful diff")
    rejected = [path for path in unique if not path_allowed(target, path)]
    if rejected:
        raise ExecutorError(f"implementation changed denied or out-of-scope paths: {', '.join(rejected)}")
    return unique


def _stage_commit_push(
    worktree_path: Path,
    target: TargetConfig,
    *,
    paths: Sequence[str],
    title: str,
    request_id: str,
) -> None:
    _run_checked(
        ["git", "add", "--", *paths], cwd=worktree_path, timeout_seconds=30, label="git add scoped paths",
    )
    staged = run_text_capture(
        ["git", "diff", "--cached", "--quiet"], cwd=str(worktree_path), timeout=20,
    )
    if staged.returncode == 0:
        raise ExecutorError("scoped changes disappeared before commit")
    if staged.returncode != 1:
        raise ExecutorError(f"git diff --cached failed ({staged.returncode}): {_bounded(staged.stderr)}")
    _run_checked(
        ["git", "commit", "-m", f"[ddp] {_safe_title(title, 72)}", "-m", f"request-id: {request_id}"],
        cwd=worktree_path, timeout_seconds=45, label="git commit",
    )
    branch = _run_checked(
        ["git", "branch", "--show-current"], cwd=worktree_path, timeout_seconds=15, label="read worktree branch",
    ).stdout.strip()
    if not branch:
        raise ExecutorError("worktree has no current branch")
    _run_checked(
        ["git", "push", "--set-upstream", target.remote, branch], cwd=worktree_path,
        timeout_seconds=90, label="git push",
    )


def _pr_budget_exhausted(ledger: DelegationLedger, target: TargetConfig, target_repo: str) -> bool:
    """True when the durable per-window PR budget for a target is spent.

    Fail-closed and independent of the gate.py merge/deploy window budget.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=target.pr_budget_window_hours)).isoformat()
    return ledger.count_prs_for_target_since(target_repo, since) >= target.pr_budget


def _planned_rows(ledger: DelegationLedger) -> list[Dict[str, Any]]:
    # Scan the entire bounded window so an ineligible row cannot starve an
    # eligible explicit synthetic request. Stable oldest-first order aids audit.
    return sorted(ledger.list_requests(state="PLANNED", limit=200), key=lambda row: (row["created_at"], row["request_id"]))


def _canary_resumable_rows(ledger: DelegationLedger, request_id: str) -> list[Dict[str, Any]]:
    """The designated request's row, if it is a bounded canary resume candidate.

    A request in VALIDATED is resumable ONLY when it carries a ``shadow``
    artifact -- durable proof that THIS executor already shadow-verified it
    in an earlier tick. VALIDATED alone is not that proof (a request can
    reach VALIDATED only via this same pipeline, but callers must not rely on
    that as a substitute for checking the artifact -- e.g. test/ops tooling
    that force-sets state for setup). Fail-closed: no shadow artifact, no
    resume. Returns a single-element list (or empty) so callers can treat it
    uniformly alongside ``_planned_rows``.
    """
    row = ledger.get_request(request_id)
    if row is None or row["state"] != "VALIDATED":
        return []
    if not any(artifact["kind"] == "shadow" for artifact in ledger.artifacts_for(request_id)):
        return []
    return [row]


def _target_is_eligible(
    row: Dict[str, Any],
    allowlist: Allowlist,
    pr_client: Optional[PrClient],
    mode: str,
    *,
    synthetic_only: bool = False,
) -> bool:
    if row.get("source_kind") not in EXECUTOR_ALLOWED_SOURCES:
        return False
    # Canary opens a real PR and therefore requires an injected client; shadow
    # never pushes and is eligible without one.
    if mode == "canary" and pr_client is None:
        return False
    target = resolve_target(allowlist, str(row.get("target_repo") or ""))
    if target is None:
        return False
    if synthetic_only and not target.synthetic_fixture:
        return False
    if SEVERITY_RANK.get(str(row.get("severity") or ""), 0) > _RISK_CEILING_RANK.get(target.risk_ceiling, 0):
        return False
    return (
        target.executor_enabled
        and (target.synthetic_fixture or target.canary_real)
        and not target.live_gateway_imports
        and target.max_autonomous_action == "create_pr"
        and bool(target.implementation_command)
        and bool(target.github_repo)
        and bool(target.worktree_base)
        and bool(target.allowed_globs)
        and (target.pr_budget >= 1 if target.canary_real else True)
    )


def _mark_failed(
    ledger: DelegationLedger,
    bus,
    request_id: str,
    *,
    actor: str,
    policy_version: str,
    error: Exception,
) -> None:
    current = ledger.get_request(request_id)
    if current is None or current["state"] not in {"BUILDING", "VALIDATED", "PR_OPEN"}:
        return
    try:
        transition(
            ledger, bus, request_id, "FAILED", actor=actor, policy_version=policy_version,
            evidence_ref=str(error)[:255],
        )
    except Exception:
        logger.exception("failed to record executor failure for %s", request_id)


def _diff_line_count(worktree_path: Path, paths: Sequence[str]) -> int:
    """Best-effort added+removed line count for the scoped shadow change.

    ``git add -N`` (intent-to-add) surfaces untracked files in the diff without
    committing; the worktree is disposable and removed in the caller's finally
    block, so mutating its index here has no lasting effect.

    Diffed against ``HEAD`` (not the bare unstaged tree): ``git add -N`` on a
    tracked file that was DELETED stages that deletion into the index, so an
    unstaged-only diff would compare an empty index entry against an already
    identical worktree (nothing) and silently report 0 removed lines. Diffing
    against HEAD captures both staged and unstaged changes uniformly.
    """
    if not paths:
        return 0
    _run_checked(["git", "add", "-N", "--", *paths], cwd=worktree_path, timeout_seconds=20, label="git add intent")
    numstat = _run_checked(["git", "diff", "--numstat", "HEAD", "--", *paths], cwd=worktree_path, timeout_seconds=20, label="git diff numstat")
    total = 0
    for line in numstat.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        added, removed = parts[0].strip(), parts[1].strip()
        total += (int(added) if added.isdigit() else 0) + (int(removed) if removed.isdigit() else 0)
    return total


def _safe_title(title: str, limit: int) -> str:
    """Collapse whitespace, keep only printable characters, and truncate.

    The envelope ``title`` is producer-supplied free text. This is the ONE
    sanitizer shared by every surface it reaches -- the shadow artifact, the
    git commit message, and the PR title -- so a newline or control
    character in a title can never land in any of them.
    """
    safe = re.sub(r"\s+", " ", str(title or "")).strip()[:limit]
    return "".join(ch for ch in safe if ch.isprintable())


def _shadow_ref(paths_count: int, lines: int, branch: str, title: str) -> str:
    """Compact, leak-free shadow evidence: counts + safe branch + sanitized title.

    Deliberately excludes absolute paths, file contents, prompts, and model
    details, matching the projection's sanitization posture.
    """
    safe_title = _safe_title(title, 80)
    return f"paths={paths_count} lines={lines} branch={branch} title={safe_title}"


def _pr_body(request_id: str) -> str:
    """Human-review PR body. Carries only the request id + do-not-merge marker.

    Deliberately excludes secrets, paths, prompts, and provider/model details.
    """
    return (
        "Automated DevFlow canary PR.\n\n"
        f"request-id: {request_id}\n\n"
        "Do not auto-merge — human review required."
    )


def run_executor_tick(
    ledger: DelegationLedger,
    allowlist: Allowlist,
    bus=None,
    *,
    actor: str = "ddp.executor",
    policy_version: str = "policy-v1",
    pr_client: Optional[PrClient] = None,
    mode: str = "shadow",
    request_id: Optional[str] = None,
    synthetic_only: bool = False,
) -> Dict[str, int]:
    """Process one eligible request.

    ``mode="shadow"`` (default) runs the pipeline to ``VALIDATED``, records a
    leak-free ``shadow`` artifact, and STOPS — no push, no PR, no remote side
    effect; eligible without a ``pr_client``. ``mode="canary"`` runs the full
    path to ``MERGE_PENDING`` and requires an injected ``pr_client`` plus an
    available PR budget. An unknown mode is a safe no-op. ``request_id``
    restricts selection to one designated request. ``synthetic_only``, when
    true, further restricts eligibility to ``synthetic_fixture`` targets,
    excluding ``canary_real`` targets even if otherwise eligible.

    Selection normally scans only ``PLANNED`` rows. Bounded canary resume is
    the one exception: when ``mode="canary"`` AND ``request_id`` names a
    request already in ``VALIDATED`` that carries a ``shadow`` artifact (see
    ``_canary_resumable_rows``), that request becomes selectable again so an
    operator can shadow-verify a request and then canary that SAME request.
    The worktree from the shadow run is gone, so a resume still fully
    rebuilds the worktree, reruns the implementation command, and
    re-validates before pushing -- it only skips the two lifecycle
    transitions (``BUILDING``, ``VALIDATED``) the request already passed
    through, since re-entering ``BUILDING`` from ``VALIDATED`` is not a legal
    edge. Resume is unreachable in shadow mode and unreachable without a
    designated ``request_id``.
    """
    if mode not in {"shadow", "canary"}:
        return {"processed": 0, "errors": 0, "skipped": 0}

    # Defense-in-depth: a real PR requires a DESIGNATED request. The CLI
    # already enforces this (executor-canary requires --request-id), but
    # run_executor_tick(mode="canary", request_id=None) would otherwise
    # auto-select the oldest eligible PLANNED row for any direct caller.
    # Refuse before any ledger mutation.
    if mode == "canary" and request_id is None:
        return {"processed": 0, "errors": 0, "skipped": 0}

    skipped = 0
    row: Optional[Dict[str, Any]] = None
    candidates = _planned_rows(ledger)
    if mode == "canary" and request_id is not None:
        # Bounded canary resume candidates: canary-only, designated-only (the
        # request_id is not None check above the mode=="shadow" case is
        # never reached here, and the request_id is None early-return above
        # already ran), and only a VALIDATED row with a shadow artifact.
        candidates = candidates + _canary_resumable_rows(ledger, request_id)
    if request_id is not None:
        candidates = [c for c in candidates if c["request_id"] == request_id]
    for candidate in candidates:
        if _target_is_eligible(candidate, allowlist, pr_client, mode, synthetic_only=synthetic_only):
            row = candidate
            break
        skipped += 1
    if row is None:
        return {"processed": 0, "errors": 0, "skipped": skipped}

    request_id = row["request_id"]
    target = resolve_target(allowlist, row["target_repo"])
    assert target is not None  # established by _target_is_eligible
    resuming = row["state"] == "VALIDATED"

    # Canary-only precondition: a durable per-window PR budget, checked before
    # any mutation so an exhausted budget leaves the request PLANNED (no
    # transition, fail-closed). Shadow never opens a PR, so it never consumes it.
    if mode == "canary" and _pr_budget_exhausted(ledger, target, row["target_repo"]):
        logger.warning("canary refused: PR budget exhausted for %s", row["target_repo"])
        return {"processed": 0, "errors": 0, "skipped": skipped}

    checkout_path: Optional[Path] = None
    worktree_path: Optional[Path] = None
    branch = ""
    lease: Optional[Dict[str, Any]] = None
    try:
        envelope = json.loads(row["envelope_json"])
        title = str(envelope.get("title") or "")

        # Acquire the lease FIRST: it is the mutual-exclusion primitive, not
        # the BUILDING transition. Two overlapping ticks racing the same
        # PLANNED row both attempt this insert; leases.request_id is a
        # PRIMARY KEY, so only one succeeds. The loser hits IntegrityError
        # here -- before touching lifecycle state -- and backs off as a safe
        # no-op instead of stealing/failing the winner's in-flight request.
        validation_count = sum(len(group) for group in (
            target.test_commands, target.lint_commands, target.typecheck_commands, target.build_commands,
        ))
        lease_seconds = max(600, target.command_timeout_seconds * max(2, validation_count + 1))
        try:
            lease = ledger.acquire_lease(request_id, actor, expires_in_seconds=lease_seconds)
        except sqlite3.IntegrityError:
            logger.info("executor tick skipped %s: lease already held by another worker", request_id)
            return {"processed": 0, "errors": 0, "skipped": skipped}

        # Transition to BUILDING only after the lease is ours, so no other
        # tick can reach this line for the same row concurrently. It still
        # happens before the boundary check (not after) so a boundary
        # failure -- e.g. the live-checkout refusal -- lands on a state
        # _mark_failed can advance to FAILED. A failure recorded while still
        # PLANNED would be invisible in the ledger and the request would be
        # reselected and re-fail on every subsequent tick.
        #
        # Skipped on resume: the request is already VALIDATED, and
        # VALIDATED -> BUILDING is not a legal edge (TRANSITIONS["VALIDATED"]
        # == {"PR_OPEN", "FAILED"}) -- re-transitioning would raise
        # IllegalTransitionError. _mark_failed already accepts VALIDATED, so
        # a boundary/rebuild failure below still correctly reaches FAILED.
        if not resuming:
            transition(ledger, bus, request_id, "BUILDING", actor=actor, policy_version=policy_version)
        checkout_path, worktree_base = _validate_target_boundary(target)
        branch = _worktree_name(request_id, int(lease["attempt_count"]))
        worktree_path = worktree_base / branch
        if not ledger.set_lease_worktree(request_id, lease["lease_id"], str(worktree_path), branch):
            raise ExecutorError("lost lease before worktree creation")

        worktree_path = _create_worktree(checkout_path, worktree_base, branch, target.default_branch)
        ledger.add_artifact(request_id, "worktree", str(worktree_path))
        ledger.add_artifact(request_id, "branch", branch)

        metadata_path = _write_metadata(worktree_path, row)
        _run_implementation(worktree_path, target, metadata_path)
        if not ledger.renew_heartbeat(request_id, lease["lease_id"], expires_in_seconds=lease_seconds):
            raise ExecutorError("lost lease after implementation")

        changed_paths = _validate_changed_paths(target, _changed_paths(worktree_path))
        validation = validate_worktree(worktree_path, target)
        if not validation.passed:
            evidence = validation.commands[0].evidence if validation.commands else "validation:failed"
            raise ExecutorError(evidence)
        for ref in validation.evidence_refs:
            ledger.add_artifact(request_id, "validation", ref)
        # Skipped on resume: the request is already VALIDATED (that is what
        # made it a resume candidate), and re-transitioning to the same
        # state is not a legal edge. The rebuild-and-revalidate above still
        # ran unconditionally -- a resume never pushes a change that was not
        # freshly re-validated in this tick.
        if not resuming:
            transition(ledger, bus, request_id, "VALIDATED", actor=actor, policy_version=policy_version)

        if mode == "shadow":
            # Shadow stops here: record the intended outcome and take no remote
            # action. No push, no PR, no lifecycle beyond VALIDATED.
            # The line count is a cosmetic diagnostic, not a safety gate, and
            # this runs AFTER the request is already VALIDATED: any failure
            # here (index lock, timeout, odd path) must never turn an
            # otherwise-successful shadow run into a lost/FAILED one. -1
            # signals "unknown" rather than a real count.
            try:
                lines = _diff_line_count(worktree_path, changed_paths)
            except Exception:
                logger.warning(
                    "diff line count failed for %s; recording lines=-1 (unknown)",
                    request_id, exc_info=True,
                )
                lines = -1
            ledger.add_artifact(request_id, "shadow", _shadow_ref(len(changed_paths), lines, branch, title))
            return {"processed": 1, "errors": 0, "skipped": skipped}

        _stage_commit_push(
            worktree_path, target, paths=changed_paths, title=title, request_id=request_id,
        )
        if not ledger.renew_heartbeat(request_id, lease["lease_id"], expires_in_seconds=lease_seconds):
            raise ExecutorError("lost lease before PR creation")

        assert pr_client is not None  # eligibility checks it in canary mode
        # Record the attempt durably BEFORE invoking the PR client. If
        # ``gh pr create`` succeeds but a later step (e.g. ``gh pr view``)
        # raises, this row still consumes the budget -- see
        # DelegationLedger.count_prs_for_target_since -- so a real,
        # ledger-invisible PR can never let a second canary open another one.
        ledger.add_artifact(request_id, "pr_attempt", branch)
        pr = pr_client.create_pr(
            worktree_path=worktree_path,
            branch=branch,
            base_branch=target.default_branch,
            title=_safe_title(title, 160),
            body=_pr_body(request_id),
            repo=target.github_repo,
            label="devflow-canary",
        )
        if not isinstance(pr, dict) or not str(pr.get("url") or "").strip() or not pr.get("number"):
            raise ExecutorError("PR client did not return a PR number and URL")
        ledger.add_artifact(request_id, "pr", str(pr["url"]))
        ledger.add_artifact(request_id, "pr_number", str(pr["number"]))
        transition(ledger, bus, request_id, "PR_OPEN", actor=actor, policy_version=policy_version)
        transition(ledger, bus, request_id, "MERGE_PENDING", actor=actor, policy_version=policy_version)
    except (ExecutorError, IllegalTransitionError, OSError, ValueError, sqlite3.Error) as exc:
        logger.error("executor tick failed for %s: %s", request_id, exc)
        # Only mark FAILED if this tick actually owns the request -- i.e. it
        # successfully acquired the lease above. A lease-acquisition race
        # (sqlite3.IntegrityError) already returned early as a no-op before
        # reaching here, but this guard also covers any other pre-lease
        # failure (e.g. a malformed envelope) so a tick can never mark a
        # request FAILED that it never held.
        if lease is not None:
            _mark_failed(ledger, bus, request_id, actor=actor, policy_version=policy_version, error=exc)
        return {"processed": 1, "errors": 1, "skipped": skipped}
    finally:
        if worktree_path is not None and checkout_path is not None:
            _remove_worktree(checkout_path, worktree_path, branch)
        if lease is not None:
            try:
                ledger.release_lease(request_id, lease["lease_id"])
            except Exception:
                logger.exception("failed to release executor lease for %s", request_id)

    return {"processed": 1, "errors": 0, "skipped": skipped}
