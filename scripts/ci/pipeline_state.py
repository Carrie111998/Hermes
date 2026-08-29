#!/usr/bin/env python3
"""Fail-closed provider adapter for the ``ci-reviewed`` label.

The fixture and diagnosis result mirror the reviewed P1 schema.  This module
has no runtime dependency on the P1 repository.  The live path reads the
current PR head, exact ``ci.yaml`` run and attempt, jobs, the exact required
check, its annotations, and compare/merge-base evidence through ``gh`` before
it can issue one failed-jobs rerun.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

DIAGNOSIS_CLASSES = (
    "BLOCKED_PROVIDER_BILLING",
    "BLOCKED_PROVIDER_POLICY",
    "BLOCKED_LINEAGE",
    "TRANSIENT_PROVIDER_FAILURE",
    "WORKFLOW_OR_HARNESS_FAILURE",
    "CODE_OR_TEST_FAILURE",
    "LOCAL_GREEN_AWAITING_HOSTED",
    "HOSTED_GREEN",
    "EVIDENCE_INCOMPLETE",
)

GITHUB_ACTIONS_APP_ID = "15368"
GITHUB_ACTIONS_APP_SLUG = "github-actions"
TRUSTED_GITHUB_ACTIONS_APP = {"id": GITHUB_ACTIONS_APP_ID, "slug": GITHUB_ACTIONS_APP_SLUG}

_REASONS = {
    "INVALID_INPUT_SCHEMA", "REPOSITORY_MISMATCH", "HEAD_MISMATCH", "CONTEXT_MISMATCH",
    "PROVIDER_APP_MISMATCH", "FOREIGN_PROVIDER", "LATEST_ATTEMPT_MISMATCH", "STALE_HEAD",
    "OLD_ATTEMPT", "MISSING_REQUIRED_CONTEXT", "DUPLICATE_REQUIRED_CONTEXT", "DUPLICATE_CONTEXT",
    "DUPLICATE_JOB", "PAGINATION_INCOMPLETE", "JOBS_INCOMPLETE", "ANNOTATIONS_INCOMPLETE",
    "MISSING_JOBS", "NO_MERGE_BASE", "POST_WRITE_HEAD_CHANGED", "POST_WRITE_READBACK_INCOMPLETE",
    "BILLING_ANNOTATION", "POLICY_ANNOTATION", "TRANSIENT_ANNOTATION", "WORKFLOW_ANNOTATION",
    "CODE_TEST_ANNOTATION", "ACTION_REQUIRED", "PYTEST_EXIT_5", "EMPTY_SELECTOR",
    "PROVIDER_FAILURE_AFTER_STARTED", "LOCAL_FAILURE", "PROVIDER_NOT_GREEN",
}
_EVIDENCE_REASONS = {
    "INVALID_INPUT_SCHEMA", "REPOSITORY_MISMATCH", "HEAD_MISMATCH", "CONTEXT_MISMATCH",
    "PROVIDER_APP_MISMATCH", "FOREIGN_PROVIDER", "LATEST_ATTEMPT_MISMATCH", "STALE_HEAD",
    "OLD_ATTEMPT", "MISSING_REQUIRED_CONTEXT", "DUPLICATE_REQUIRED_CONTEXT", "DUPLICATE_CONTEXT",
    "DUPLICATE_JOB", "PAGINATION_INCOMPLETE", "JOBS_INCOMPLETE", "ANNOTATIONS_INCOMPLETE",
    "MISSING_JOBS", "POST_WRITE_HEAD_CHANGED", "POST_WRITE_READBACK_INCOMPLETE",
}
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_ZERO_SHA = "0" * 40
_STATUSES = {"queued", "in_progress", "completed"}
_CONCLUSIONS = {"success", "failure", "cancelled", "skipped", "neutral", "timed_out", "action_required", None}
_ANNOTATION_CATEGORIES = {"billing", "policy", "transient", "workflow", "code_test"}
_WRAPPER_KEYS = {"jobs", "check_runs", "annotations", "workflow_runs"}
_WORKFLOW_RUN_KEYS = {"run_id", "head_sha", "attempt", "status", "conclusion"}
_ROUTING_FALSE = {
    "retry_allowed": False,
    "code_fix_allowed": False,
    "local_repro_required": False,
    "source_branch_loop_allowed": False,
}


class RerunCommandError(RuntimeError):
    """The provider rejected the one allowed rerun request or head fence."""


class EvidenceError(RuntimeError):
    """Provider data cannot be converted to bounded P1 evidence."""


def _sha(value: Any, *, allow_zero: bool = False) -> bool:
    return isinstance(value, str) and _SHA_RE.fullmatch(value) is not None and (allow_zero or value != _ZERO_SHA)


def _text(value: Any, limit: int = 1024, *, allow_empty: bool = False) -> bool:
    return isinstance(value, str) and len(value) <= limit and (allow_empty or bool(value.strip())) and not any(ord(char) < 32 for char in value)


def _app(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {"id", "slug"} and all(_text(value[key], 128) for key in ("id", "slug"))


def _official_app(value: Any) -> bool:
    return _app(value) and value == TRUSTED_GITHUB_ACTIONS_APP


def _repository(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {"owner", "repo"} and all(_text(value[key], 100) for key in ("owner", "repo"))


def _result(classification: str, reasons: list[str], evidence: dict[str, Any]) -> dict[str, Any]:
    """Build the exact five-key P1 result shape."""
    return {
        "schema": 1,
        "classification": classification,
        "reason_codes": list(dict.fromkeys(reason for reason in reasons if reason in _REASONS)),
        "routing": dict(_ROUTING_FALSE),
        "evidence": evidence,
    }


def _empty_evidence() -> dict[str, Any]:
    return {
        "repository": None,
        "head_sha": None,
        "context": None,
        "provider_app": None,
        "latest_attempt": None,
        "merge_base": None,
        "real_started_step": False,
        "post_write_valid": False,
    }


def _evidence(data: dict[str, Any]) -> dict[str, Any]:
    target = data["target"]
    post = data["post_write"]
    post_valid = post["readback_complete"] and (post["head_sha"] is None or post["head_sha"] == target["head_sha"])
    return {
        "repository": dict(target["repository"]),
        "head_sha": target["head_sha"],
        "context": target["context"],
        "provider_app": dict(target["provider_app"]),
        "latest_attempt": target["latest_attempt"],
        "merge_base": data["lineage"]["merge_base"],
        "real_started_step": False,
        "post_write_valid": post_valid,
    }


def _valid_step(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {"name", "status", "conclusion"} and _text(value["name"], 256) and value["status"] in _STATUSES and value["conclusion"] in _CONCLUSIONS


def _valid_annotation(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {"category", "message"} and value["category"] in _ANNOTATION_CATEGORIES and _text(value["message"])


def _valid_job(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"job_id", "head_sha", "attempt", "status", "conclusion", "runner_id", "steps", "annotations"}
        and _text(value["job_id"], 256)
        and _sha(value["head_sha"])
        and isinstance(value["attempt"], int) and not isinstance(value["attempt"], bool) and value["attempt"] >= 1
        and value["status"] in _STATUSES and value["conclusion"] in _CONCLUSIONS
        and isinstance(value["runner_id"], int) and not isinstance(value["runner_id"], bool) and value["runner_id"] >= 0
        and isinstance(value["steps"], list) and len(value["steps"]) <= 256 and all(_valid_step(step) for step in value["steps"])
        and isinstance(value["annotations"], list) and len(value["annotations"]) <= 128 and all(_valid_annotation(annotation) for annotation in value["annotations"])
    )


def _valid_context(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"context", "head_sha", "app", "attempt", "status", "conclusion", "jobs"}
        and _text(value["context"], 256) and _sha(value["head_sha"])
        and _official_app(value["app"])
        and isinstance(value["attempt"], int) and not isinstance(value["attempt"], bool) and value["attempt"] >= 1
        and value["status"] in _STATUSES and value["conclusion"] in _CONCLUSIONS
        and isinstance(value["jobs"], list) and len(value["jobs"]) <= 128 and all(_valid_job(job) for job in value["jobs"])
    )


def _valid_fixture_shape(data: Any) -> bool:
    if not isinstance(data, dict) or set(data) != {"schema", "target", "provider_snapshot", "lineage", "local", "post_write"} or data["schema"] != 1:
        return False
    target, snapshot, lineage, local, post = (data[key] for key in ("target", "provider_snapshot", "lineage", "local", "post_write"))
    if not all(isinstance(value, dict) for value in (target, snapshot, lineage, local, post)):
        return False
    if set(target) != {"repository", "head_sha", "context", "provider_app", "latest_attempt"}:
        return False
    if set(snapshot) != {"repository", "head_sha", "required_context", "provider_app", "latest_attempt", "workflow_run", "pagination", "contexts"}:
        return False
    if set(lineage) != {"merge_base", "source_branch", "target_branch"} or set(local) != {"status", "exit_code", "selector"} or set(post) != {"head_sha", "readback_complete"}:
        return False
    if not _repository(target["repository"]) or not _repository(snapshot["repository"]):
        return False
    if not _official_app(target["provider_app"]) or not _official_app(snapshot["provider_app"]):
        return False
    if not _sha(target["head_sha"]) or not _sha(snapshot["head_sha"]):
        return False
    if not _text(target["context"], 256) or not _text(snapshot["required_context"], 256):
        return False
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in (target["latest_attempt"], snapshot["latest_attempt"])):
        return False
    pagination = snapshot["pagination"]
    if not isinstance(pagination, dict) or set(pagination) != {"contexts_complete", "jobs_complete", "annotations_complete"} or not all(isinstance(value, bool) for value in pagination.values()):
        return False
    if not isinstance(snapshot["contexts"], list) or len(snapshot["contexts"]) > 128 or not all(_valid_context(context) for context in snapshot["contexts"]):
        return False
    workflow_run = snapshot["workflow_run"]
    if not isinstance(workflow_run, dict) or set(workflow_run) != _WORKFLOW_RUN_KEYS:
        return False
    if not _text(workflow_run["run_id"], 128) or not _sha(workflow_run["head_sha"]):
        return False
    if isinstance(workflow_run["attempt"], bool) or not isinstance(workflow_run["attempt"], int) or workflow_run["attempt"] < 1:
        return False
    if not _text(workflow_run["status"], 64) or workflow_run["conclusion"] is not None and not _text(workflow_run["conclusion"], 64):
        return False
    if lineage["merge_base"] is not None and not _sha(lineage["merge_base"]):
        return False
    if not _text(lineage["source_branch"], 255) or not _text(lineage["target_branch"], 255):
        return False
    if local["status"] not in {"NOT_RUN", "PASS", "FAIL"}:
        return False
    if local["exit_code"] is not None and (isinstance(local["exit_code"], bool) or not isinstance(local["exit_code"], int) or local["exit_code"] < 0):
        return False
    if local["selector"] is not None and not _text(local["selector"], 256, allow_empty=True):
        return False
    if local["status"] == "NOT_RUN" and (local["exit_code"] is not None or local["selector"] is not None):
        return False
    if local["status"] == "PASS" and (local["exit_code"] != 0 or not _text(local["selector"], 256)):
        return False
    if local["status"] == "FAIL" and (not isinstance(local["exit_code"], int) or not _text(local["selector"], 256, allow_empty=True)):
        return False
    return type(post["readback_complete"]) is bool and (post["head_sha"] is None or _sha(post["head_sha"]))


def _real_started_step(step: dict[str, Any]) -> bool:
    return step["status"] == "in_progress" or (step["status"] == "completed" and step["conclusion"] not in {None, "skipped"})


def _job_executed_failure(job: dict[str, Any]) -> bool:
    return job["status"] == "completed" and job["conclusion"] == "failure" and job["runner_id"] > 0 and any(_real_started_step(step) for step in job["steps"])


def _category(annotation: dict[str, Any]) -> str | None:
    category = annotation.get("category")
    message = annotation.get("message", "").lower()
    if category == "billing" or "billing" in message or "spending" in message:
        return "billing"
    return category if category in {"policy", "transient", "workflow", "code_test"} else None


def _diagnose_valid(data: dict[str, Any], *, require_post_write: bool) -> dict[str, Any]:
    target = data["target"]
    snapshot = data["provider_snapshot"]
    evidence = _evidence(data)
    reasons: list[str] = []
    if snapshot["repository"] != target["repository"]:
        reasons.append("REPOSITORY_MISMATCH")
    if snapshot["head_sha"] != target["head_sha"]:
        reasons.append("HEAD_MISMATCH")
    if snapshot["required_context"] != target["context"]:
        reasons.append("CONTEXT_MISMATCH")
    if snapshot["provider_app"] != target["provider_app"]:
        reasons.append("PROVIDER_APP_MISMATCH")
    if snapshot["latest_attempt"] != target["latest_attempt"]:
        reasons.append("LATEST_ATTEMPT_MISMATCH")
    pagination = snapshot["pagination"]
    if not pagination["contexts_complete"]:
        reasons.append("PAGINATION_INCOMPLETE")
    if not pagination["jobs_complete"]:
        reasons.append("JOBS_INCOMPLETE")
    if not pagination["annotations_complete"]:
        reasons.append("ANNOTATIONS_INCOMPLETE")
    contexts = snapshot["contexts"]
    counts: dict[str, int] = {}
    for context in contexts:
        counts[context["context"]] = counts.get(context["context"], 0) + 1
        if context["head_sha"] != target["head_sha"]:
            reasons.append("STALE_HEAD")
        if context["app"] != target["provider_app"]:
            reasons.append("FOREIGN_PROVIDER")
        if context["attempt"] != target["latest_attempt"]:
            reasons.append("OLD_ATTEMPT")
        for job in context["jobs"]:
            if job["head_sha"] != target["head_sha"]:
                reasons.append("STALE_HEAD")
            if job["attempt"] != target["latest_attempt"]:
                reasons.append("OLD_ATTEMPT")
    required_count = counts.get(target["context"], 0)
    if required_count == 0:
        reasons.append("MISSING_REQUIRED_CONTEXT")
    elif required_count > 1:
        reasons.append("DUPLICATE_REQUIRED_CONTEXT")
    for name, count in counts.items():
        if name != target["context"] and count > 1:
            reasons.append("DUPLICATE_CONTEXT")
    required = next((context for context in contexts if context["context"] == target["context"]), None)
    jobs = required["jobs"] if required else []
    if required and not jobs and required["conclusion"] != "action_required":
        reasons.append("MISSING_JOBS")
    job_ids = [job["job_id"] for job in jobs]
    if len(set(job_ids)) != len(job_ids):
        reasons.append("DUPLICATE_JOB")
    if data["lineage"]["merge_base"] is None:
        reasons.append("NO_MERGE_BASE")
    workflow_run = snapshot["workflow_run"]
    if workflow_run["head_sha"] != target["head_sha"]:
        reasons.append("HEAD_MISMATCH")
    if workflow_run["attempt"] != target["latest_attempt"]:
        reasons.append("OLD_ATTEMPT")
    post = data["post_write"]
    if require_post_write:
        if not post["readback_complete"]:
            reasons.append("POST_WRITE_READBACK_INCOMPLETE")
            evidence["post_write_valid"] = False
        if post["head_sha"] is not None and post["head_sha"] != target["head_sha"]:
            reasons.append("POST_WRITE_HEAD_CHANGED")
            evidence["post_write_valid"] = False
    else:
        evidence["post_write_valid"] = False
        if post["head_sha"] is not None and post["head_sha"] != target["head_sha"]:
            reasons.append("POST_WRITE_HEAD_CHANGED")
    action_required = bool(required and (required["conclusion"] == "action_required" or any(job["conclusion"] == "action_required" for job in jobs)))
    if action_required and not jobs and not reasons:
        return _result("BLOCKED_PROVIDER_POLICY", ["ACTION_REQUIRED"], evidence)
    if workflow_run["status"] != "completed" or workflow_run["conclusion"] != "failure":
        return _result("EVIDENCE_INCOMPLETE", [*reasons, "PROVIDER_NOT_GREEN"], evidence)
    if required and (required["status"] != "completed" or required["conclusion"] != "failure") and not reasons:
        if required["status"] in {"queued", "in_progress"}:
            result = _result("TRANSIENT_PROVIDER_FAILURE", ["PROVIDER_NOT_GREEN"], evidence)
            result["routing"]["retry_allowed"] = True
            return result
        return _result("EVIDENCE_INCOMPLETE", ["PROVIDER_NOT_GREEN"], evidence)
    non_lineage_evidence = [reason for reason in reasons if reason != "NO_MERGE_BASE"]
    if any(reason in _EVIDENCE_REASONS for reason in non_lineage_evidence):
        return _result("EVIDENCE_INCOMPLETE", reasons, evidence)
    if data["lineage"]["merge_base"] is None:
        return _result("BLOCKED_LINEAGE", reasons, evidence)
    annotations = [annotation for job in jobs for annotation in job["annotations"]]
    categories = [_category(annotation) for annotation in annotations]
    # Billing/spending is a hard provider block even when the job did execute.
    if "billing" in categories:
        return _result("BLOCKED_PROVIDER_BILLING", [*reasons, "BILLING_ANNOTATION"], evidence)
    if action_required or "policy" in categories:
        return _result("BLOCKED_PROVIDER_POLICY", [*reasons, "ACTION_REQUIRED" if action_required else "POLICY_ANNOTATION"], evidence)
    failed_jobs = [job for job in jobs if job["conclusion"] == "failure"]
    failed_executed = [job for job in failed_jobs if _job_executed_failure(job)]
    evidence["real_started_step"] = bool(failed_executed)
    local = data["local"]
    if local["status"] == "FAIL" and local["exit_code"] == 5:
        reasons.append("PYTEST_EXIT_5")
    if local["status"] == "FAIL" and local["selector"] == "":
        reasons.append("EMPTY_SELECTOR")
    if local["status"] == "FAIL" and (local["exit_code"] == 5 or local["selector"] == ""):
        return _result("WORKFLOW_OR_HARNESS_FAILURE", reasons, evidence)
    for category, classification, reason in (
        ("transient", "TRANSIENT_PROVIDER_FAILURE", "TRANSIENT_ANNOTATION"),
        ("workflow", "WORKFLOW_OR_HARNESS_FAILURE", "WORKFLOW_ANNOTATION"),
        ("code_test", "CODE_OR_TEST_FAILURE", "CODE_TEST_ANNOTATION"),
    ):
        if category in categories:
            result = _result(classification, [*reasons, reason], evidence)
            if classification == "TRANSIENT_PROVIDER_FAILURE":
                result["routing"]["retry_allowed"] = True
            elif classification == "WORKFLOW_OR_HARNESS_FAILURE":
                result["routing"]["local_repro_required"] = bool(failed_executed and local["status"] != "FAIL")
            else:
                result["routing"].update(code_fix_allowed=True, local_repro_required=bool(failed_executed and local["status"] != "FAIL"), source_branch_loop_allowed=True)
            return result
    if required and required["status"] == "completed" and required["conclusion"] == "failure":
        if failed_jobs and len(failed_executed) == len(failed_jobs):
            result = _result("CODE_OR_TEST_FAILURE", [*reasons, "PROVIDER_FAILURE_AFTER_STARTED"], evidence)
            result["routing"].update(code_fix_allowed=True, local_repro_required=local["status"] != "FAIL", source_branch_loop_allowed=True)
            return result
        return _result("WORKFLOW_OR_HARNESS_FAILURE", reasons, evidence)
    if local["status"] == "FAIL":
        result = _result("CODE_OR_TEST_FAILURE", [*reasons, "LOCAL_FAILURE"], evidence)
        result["routing"].update(code_fix_allowed=True, source_branch_loop_allowed=True)
        return result
    if local["status"] == "PASS":
        return _result("LOCAL_GREEN_AWAITING_HOSTED", reasons, evidence)
    if required and required["status"] in {"queued", "in_progress"}:
        result = _result("TRANSIENT_PROVIDER_FAILURE", [*reasons, "PROVIDER_NOT_GREEN"], evidence)
        result["routing"]["retry_allowed"] = True
        return result
    if require_post_write and required and required["status"] == "completed" and required["conclusion"] == "success" and jobs and all(job["status"] == "completed" and job["conclusion"] == "success" for job in jobs):
        return _result("HOSTED_GREEN", reasons, evidence)
    return _result("EVIDENCE_INCOMPLETE", [*reasons, "PROVIDER_NOT_GREEN"], evidence)


def diagnose_pipeline(data: Any, *, require_post_write: bool = True) -> dict[str, Any]:
    """Return exact P1 diagnosis; malformed evidence never escapes an exception."""
    try:
        if not _valid_fixture_shape(data):
            return _result("EVIDENCE_INCOMPLETE", ["INVALID_INPUT_SCHEMA"], _empty_evidence())
        return _diagnose_valid(data, require_post_write=require_post_write)
    except Exception:
        return _result("EVIDENCE_INCOMPLETE", ["INVALID_INPUT_SCHEMA"], _empty_evidence())


def can_request_rerun(data: Any) -> bool:
    """Allow one rerun only when every failed job is a completed executed failure."""
    try:
        decision = diagnose_pipeline(data, require_post_write=False)
        if decision["classification"] != "CODE_OR_TEST_FAILURE" or not _valid_fixture_shape(data):
            return False
        workflow_run = data["provider_snapshot"]["workflow_run"]
        if workflow_run["status"] != "completed" or workflow_run["conclusion"] != "failure":
            return False
        context = next(context for context in data["provider_snapshot"]["contexts"] if context["context"] == data["target"]["context"])
        failed = [job for job in context["jobs"] if job["conclusion"] == "failure"]
        return bool(failed) and all(_job_executed_failure(job) for job in failed) and decision["evidence"]["real_started_step"]
    except Exception:
        return False


def _workflow_matches(run_workflow: Any, requested: str) -> bool:
    if run_workflow == requested:
        return True
    return isinstance(run_workflow, str) and requested.lower().endswith((".yml", ".yaml")) and run_workflow.lower() == Path(requested).stem.lower()


def select_exact_workflow_run(runs: list[dict[str, Any]], *, workflow: str, head_sha: str) -> dict[str, Any] | None:
    """Select the newest run whose workflow and head both match."""
    if not _sha(head_sha):
        return None
    candidates = [run for run in runs if isinstance(run, dict) and run.get("headSha", run.get("head_sha")) == head_sha and _workflow_matches(run.get("workflowName", run.get("workflow_name")), workflow)]
    return max(candidates, key=lambda run: (str(run.get("createdAt", run.get("created_at", ""))), int(run.get("databaseId", run.get("id", 0)) or 0)), default=None)


def parse_paginated_response(value: Any, key: str) -> list[dict[str, Any]] | None:
    """Unwrap REST objects/pages and reject every non-object item."""
    if isinstance(value, dict):
        if any(wrapper_key in value for wrapper_key in _WRAPPER_KEYS if wrapper_key != key) or not isinstance(value.get(key), list):
            return None
        items = value[key]
    elif isinstance(value, list):
        if all(isinstance(page, list) for page in value):
            items = [item for page in value for item in page]
        elif any(isinstance(page, dict) and key in page for page in value):
            if not all(
                isinstance(page, dict)
                and key in page
                and isinstance(page[key], list)
                and not any(wrapper_key in page for wrapper_key in _WRAPPER_KEYS if wrapper_key != key)
                for page in value
            ):
                return None
            items = [item for page in value for item in page[key]]
        elif all(isinstance(page, dict) for page in value):
            if any(wrapper_key in page for page in value for wrapper_key in _WRAPPER_KEYS):
                return None
            items = value
        else:
            return None
    else:
        return None
    return items if all(isinstance(item, dict) for item in items) else None


def request_exactly_one_rerun(
    decision: dict[str, Any], *, repo: str, run_id: str, runner: Callable[[list[str]], Any] | None = None,
    eligible: bool | None = None, expected_run_id: str | None = None, expected_attempt: int | None = None,
    expected_head_sha: str | None = None, head_reader: Callable[[], str] | None = None,
    workflow_run_reader: Callable[[], dict[str, Any]] | None = None,
    required_check_reader: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Fence the head, then issue exactly one rerun request."""
    allowed = (decision.get("classification") == "CODE_OR_TEST_FAILURE" and decision.get("evidence", {}).get("real_started_step") is True) if eligible is None else eligible
    if not allowed:
        return {"status": decision.get("classification", "EVIDENCE_INCOMPLETE")}
    if expected_run_id is None or expected_attempt is None or expected_head_sha is None or head_reader is None or workflow_run_reader is None or required_check_reader is None:
        raise RerunCommandError("missing mandatory pre-rerun evidence fences")
    if str(run_id) != str(expected_run_id):
        raise RerunCommandError("rerun target does not match selected run")
    bound_run_id = str(expected_run_id)
    try:
        workflow_run = workflow_run_reader()
    except Exception as exc:
        raise RerunCommandError(str(exc)) from exc
    if not isinstance(workflow_run, dict) or str(workflow_run.get("run_id")) != bound_run_id or workflow_run.get("head_sha") != expected_head_sha or workflow_run.get("attempt") != expected_attempt or workflow_run.get("status") != "completed" or workflow_run.get("conclusion") != "failure":
        raise RerunCommandError("workflow run is not the exact completed failure")
    try:
        check = required_check_reader()
    except Exception as exc:
        raise RerunCommandError(str(exc)) from exc
    if not isinstance(check, dict) or check.get("head_sha") != expected_head_sha or check.get("status") != "completed" or check.get("conclusion") != "failure":
        raise RerunCommandError("required check is not an exact completed failure")
    try:
        current = head_reader()
    except Exception as exc:
        raise RerunCommandError(str(exc)) from exc
    if current != expected_head_sha:
        raise RerunCommandError("PR head changed before rerun")
    args = ["run", "rerun", bound_run_id, "--repo", repo, "--failed"]
    try:
        outcome = subprocess.run(["gh", *args], check=True, capture_output=True, text=True) if runner is None else runner(args)
    except Exception as exc:
        if isinstance(exc, RerunCommandError):
            raise
        raise RerunCommandError(str(exc)) from exc
    if getattr(outcome, "returncode", 0) not in (0, None):
        raise RerunCommandError("gh run rerun returned non-zero")
    return {"status": "RERUN_REQUESTED"}


def build_fixture_from_api(
    *, repo: str, head_sha: str, run: dict[str, Any], jobs: list[dict[str, Any]], check_runs: list[dict[str, Any]], annotations_by_check: dict[Any, list[dict[str, Any]]], base_sha: str, source_branch: str, target_branch: str, merge_base: str | None, required_context: str = "All required checks pass"
) -> dict[str, Any] | None:
    """Bind raw provider data to one trusted check and its aggregate job."""
    try:
        if not _sha(head_sha) or not _sha(base_sha) or not isinstance(run, dict) or "headSha" not in run or run["headSha"] != head_sha:
            return None
        if run.get("databaseId") is None or not _text(str(run["databaseId"]), 128):
            return None
        attempt = run["attempt"]
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            return None
        if run.get("status") != "completed" or run.get("conclusion") != "failure":
            return None
        selected = [check for check in check_runs if isinstance(check, dict) and check.get("name") == required_context and "head_sha" in check and check["head_sha"] == head_sha]
        if len(selected) != 1:
            return None
        check = selected[0]
        check_id = check["id"]
        if check.get("status") not in _STATUSES or check.get("conclusion") not in _CONCLUSIONS or not _official_app({"id": str((check.get("app") or {}).get("id")), "slug": (check.get("app") or {}).get("slug")}):
            return None
        raw_annotations = annotations_by_check[check_id]
        if not isinstance(raw_annotations, list) or not all(isinstance(annotation, dict) for annotation in raw_annotations):
            return None
        normalized_jobs: list[dict[str, Any]] = []
        raw_by_id: dict[str, dict[str, Any]] = {}
        aggregate_raw: list[dict[str, Any]] = []
        for raw in jobs:
            if not isinstance(raw, dict) or raw.get("id") is None or str(raw["id"]) in raw_by_id:
                return None
            if "head_sha" not in raw or raw["head_sha"] != head_sha or "run_attempt" not in raw or raw["run_attempt"] != attempt:
                return None
            if "steps" not in raw or not isinstance(raw["steps"], list):
                return None
            raw_by_id[str(raw["id"])] = raw
            if raw.get("name") == required_context or raw.get("check_run_id") == check_id:
                aggregate_raw.append(raw)
            normalized_jobs.append({
                "job_id": str(raw["id"]),
                "head_sha": raw["head_sha"],
                "attempt": attempt,
                "status": raw.get("status"),
                "conclusion": raw.get("conclusion"),
                "runner_id": raw.get("runner_id", 0) or 0,
                "steps": [{"name": step.get("name"), "status": step.get("status"), "conclusion": step.get("conclusion")} for step in raw["steps"]],
                "annotations": [],
            })
        if len(aggregate_raw) != 1:
            return None
        if aggregate_raw[0].get("status") != check["status"] or aggregate_raw[0].get("conclusion") != check["conclusion"]:
            return None
        by_id = {job["job_id"]: job for job in normalized_jobs}
        aggregate_destination = by_id[str(aggregate_raw[0]["id"])]
        for raw_annotation in raw_annotations:
            if not isinstance(raw_annotation.get("message"), str) or not raw_annotation["message"]:
                return None
            destination = by_id.get(str(raw_annotation["job_id"])) if raw_annotation.get("job_id") is not None else aggregate_destination
            if destination is None:
                return None
            message = raw_annotation["message"]
            raw_category = raw_annotation.get("category")
            if raw_category in _ANNOTATION_CATEGORIES:
                category = raw_category
            elif any(word in message.lower() for word in ("billing", "spending")):
                category = "billing"
            elif "policy" in message.lower() or "action_required" in message.lower():
                category = "policy"
            else:
                category = "code_test"
            destination["annotations"].append({"category": category, "message": message})
        raw_app = check.get("app") or {}
        app = {"id": str(raw_app.get("id")), "slug": raw_app.get("slug")}
        owner, name = repo.split("/", 1)
        if not _official_app(app) or not _repository({"owner": owner, "repo": name}) or not _text(source_branch, 255) or not _text(target_branch, 255):
            return None
        target = {"repository": {"owner": owner, "repo": name}, "head_sha": head_sha, "context": required_context, "provider_app": dict(TRUSTED_GITHUB_ACTIONS_APP), "latest_attempt": attempt}
        context = {"context": required_context, "head_sha": head_sha, "app": dict(TRUSTED_GITHUB_ACTIONS_APP), "attempt": attempt, "status": check["status"], "conclusion": check["conclusion"], "jobs": normalized_jobs}
        return {
            "schema": 1,
            "target": target,
            "provider_snapshot": {"repository": dict(target["repository"]), "head_sha": head_sha, "required_context": required_context, "provider_app": dict(TRUSTED_GITHUB_ACTIONS_APP), "latest_attempt": attempt, "workflow_run": {"run_id": str(run["databaseId"]), "head_sha": head_sha, "attempt": attempt, "status": run["status"], "conclusion": run["conclusion"]}, "pagination": {"contexts_complete": True, "jobs_complete": True, "annotations_complete": True}, "contexts": [context]},
            "lineage": {"merge_base": merge_base, "source_branch": source_branch, "target_branch": target_branch},
            "local": {"status": "NOT_RUN", "exit_code": None, "selector": None},
            "post_write": {"head_sha": None, "readback_complete": False},
        }
    except Exception:
        return None


def _gh_json(args: list[str]) -> Any:
    try:
        result = subprocess.run(["gh", *args], check=True, capture_output=True, text=True)
        return json.loads(result.stdout)
    except Exception as exc:
        raise EvidenceError(str(exc)) from exc


def read_current_pr_head(repo: str, pr_number: str) -> str:
    value = _gh_json(["pr", "view", pr_number, "--repo", repo, "--json", "headRefOid"])
    head = value.get("headRefOid") if isinstance(value, dict) else None
    if not _sha(head):
        raise EvidenceError("invalid current PR head")
    return head


def read_exact_workflow_run(repo: str, run_id: str) -> dict[str, Any]:
    """Read the exact run identity and state at the mutation boundary."""
    value = _gh_json(["run", "view", run_id, "--repo", repo, "--json", "databaseId,headSha,attempt,status,conclusion"])
    if not isinstance(value, dict) or value.get("databaseId") is None:
        raise EvidenceError("workflow run readback is not an object")
    return {
        "run_id": str(value["databaseId"]),
        "head_sha": value.get("headSha"),
        "attempt": value.get("attempt"),
        "status": value.get("status"),
        "conclusion": value.get("conclusion"),
    }


def read_exact_required_check(repo: str, head_sha: str, required_context: str) -> dict[str, Any]:
    """Read and validate the required check again at the mutation boundary."""
    checks = parse_paginated_response(
        _gh_json(["api", "--paginate", "--slurp", f"repos/{repo}/commits/{head_sha}/check-runs?per_page=100"]),
        "check_runs",
    )
    if checks is None:
        raise EvidenceError("required-check readback is malformed")
    selected = [check for check in checks if check.get("name") == required_context and check.get("head_sha") == head_sha]
    if len(selected) != 1:
        raise EvidenceError("required-check readback is missing or duplicated")
    check = selected[0]
    app = check.get("app") or {}
    if not _official_app({"id": str(app.get("id")), "slug": app.get("slug")}):
        raise EvidenceError("required-check provider identity changed")
    return {"status": check.get("status"), "conclusion": check.get("conclusion"), "head_sha": head_sha}


def verify_exact_head_run_readback(*, expected_head_sha: str, expected_run_id: str, head_reader: Callable[[], str], run_reader: Callable[[], dict[str, Any]]) -> bool:
    """Validate the exact head and run only after the mutation returns."""
    try:
        run = run_reader()
        return head_reader() == expected_head_sha and isinstance(run, dict) and str(run.get("databaseId", run.get("id"))) == str(expected_run_id) and run.get("headSha", run.get("head_sha")) == expected_head_sha
    except Exception:
        return False


def collect_provider_fixture(*, repo: str, pr_number: str, workflow: str, required_context: str = "All required checks pass", event_head_sha: str | None = None) -> tuple[str, str, dict[str, Any]]:
    """Read exact provider evidence; malformed evidence is fatal to the run."""
    pr = _gh_json(["pr", "view", pr_number, "--repo", repo, "--json", "headRefOid,baseRefOid,headRefName,baseRefName"])
    if not isinstance(pr, dict):
        raise EvidenceError("PR view is not an object")
    head, base = pr.get("headRefOid"), pr.get("baseRefOid")
    if not _sha(head) or (event_head_sha is not None and event_head_sha != head):
        raise EvidenceError("current PR head does not match label event")
    runs = parse_paginated_response(_gh_json(["run", "list", "--repo", repo, "--workflow", workflow, "--commit", head, "--limit", "100", "--json", "databaseId,status,conclusion,headSha,workflowName,createdAt"]), "workflow_runs")
    if runs is None:
        raise EvidenceError("workflow run response is malformed")
    run = select_exact_workflow_run(runs, workflow=workflow, head_sha=head)
    if run is None:
        raise EvidenceError("no exact-head workflow run")
    selected_run_id = run.get("databaseId")
    if selected_run_id is None or not _text(str(selected_run_id), 128):
        raise EvidenceError("selected workflow run lacks an ID")
    run_id = str(selected_run_id)
    details = _gh_json(["run", "view", run_id, "--repo", repo, "--json", "databaseId,status,conclusion,headSha,workflowName,attempt"])
    if not isinstance(details, dict) or str(details.get("databaseId")) != run_id or details.get("headSha") != head or not isinstance(details.get("attempt"), int) or isinstance(details.get("attempt"), bool) or details["attempt"] < 1:
        raise EvidenceError("run view lacks exact identity, head, or attempt")
    run = {**run, **details, "databaseId": run_id}
    jobs = parse_paginated_response(_gh_json(["api", "--paginate", "--slurp", f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"]), "jobs")
    checks = parse_paginated_response(_gh_json(["api", "--paginate", "--slurp", f"repos/{repo}/commits/{head}/check-runs?per_page=100"]), "check_runs")
    if jobs is None or checks is None:
        raise EvidenceError("jobs or check-runs response is malformed")
    required = [check for check in checks if check.get("name") == required_context and check.get("head_sha") == head]
    if len(required) != 1 or required[0].get("id") is None:
        raise EvidenceError("required check is missing or duplicated")
    check_id = required[0]["id"]
    annotations = parse_paginated_response(_gh_json(["api", "--paginate", "--slurp", f"repos/{repo}/check-runs/{check_id}/annotations?per_page=100"]), "annotations")
    if annotations is None:
        raise EvidenceError("annotations response is malformed")
    compare = _gh_json(["api", f"repos/{repo}/compare/{base}...{head}"]) if _sha(base) else {}
    merge = compare.get("merge_base_commit", {}).get("sha") if isinstance(compare, dict) else None
    fixture = build_fixture_from_api(repo=repo, head_sha=head, run=run, jobs=jobs, check_runs=checks, annotations_by_check={check_id: annotations}, base_sha=base, source_branch=pr.get("headRefName"), target_branch=pr.get("baseRefName"), merge_base=merge if _sha(merge) else None, required_context=required_context)
    if fixture is None:
        raise EvidenceError("provider evidence failed bounded schema binding")
    return head, run_id, fixture


def _print_json(value: dict[str, Any]) -> None:
    print(json.dumps(value, separators=(",", ":"), sort_keys=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--repo")
    parser.add_argument("--pr")
    parser.add_argument("--workflow", default="ci.yaml")
    parser.add_argument("--required-context", default="All required checks pass")
    parser.add_argument("--event-head-sha")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.fixture:
            decision = diagnose_pipeline(json.loads(args.fixture.read_text(encoding="utf-8")))
            _print_json(decision)
            return 1 if decision["classification"] == "EVIDENCE_INCOMPLETE" else 0
        if not args.repo or not args.pr:
            raise EvidenceError("--repo and --pr are required without --fixture")
        head, selected_run_id, fixture = collect_provider_fixture(repo=args.repo, pr_number=args.pr, workflow=args.workflow, required_context=args.required_context, event_head_sha=args.event_head_sha)
        decision = diagnose_pipeline(fixture, require_post_write=False)
        if not can_request_rerun(fixture):
            _print_json({"status": decision["classification"], "head_sha": head})
            return 1 if decision["classification"] == "EVIDENCE_INCOMPLETE" else 0
        workflow_run = fixture["provider_snapshot"]["workflow_run"]
        bound_run_id = str(selected_run_id)
        if str(workflow_run["run_id"]) != bound_run_id:
            raise EvidenceError("bounded fixture changed selected workflow run ID")
        result = request_exactly_one_rerun(decision, repo=args.repo, run_id=bound_run_id, eligible=True, expected_run_id=bound_run_id, expected_attempt=workflow_run["attempt"], expected_head_sha=head, head_reader=lambda: read_current_pr_head(args.repo, args.pr), workflow_run_reader=lambda: read_exact_workflow_run(args.repo, bound_run_id), required_check_reader=lambda: read_exact_required_check(args.repo, head, args.required_context))
        if not verify_exact_head_run_readback(expected_head_sha=head, expected_run_id=bound_run_id, head_reader=lambda: read_current_pr_head(args.repo, args.pr), run_reader=lambda: _gh_json(["run", "view", bound_run_id, "--repo", args.repo, "--json", "databaseId,headSha"])):
            raise EvidenceError("post-rerun exact-head readback failed")
        _print_json(result)
        return 0
    except RerunCommandError as exc:
        print(f"label-rerun: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        _print_json({"status": "EVIDENCE_INCOMPLETE", "reason_codes": ["EVIDENCE_INCOMPLETE"]})
        print(f"label-rerun: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
