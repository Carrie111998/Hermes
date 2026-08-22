#!/usr/bin/env python3
"""Deterministic CI gate and selection parser for Hermes upstream PRs."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_STATE_DIR = Path("/home/jiddy/.hermes/state/upstream-pr-review")
PASS_CONCLUSIONS = {"SUCCESS", "SKIPPED", "NEUTRAL"}
FAIL_CONCLUSIONS = {
    "FAILURE",
    "CANCELLED",
    "TIMED_OUT",
    "ACTION_REQUIRED",
    "STALE",
    "STARTUP_FAILURE",
}
AGGREGATE_CHECK = "All required checks pass"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_gh(repo: str, pr: int) -> dict[str, Any]:
    command = [
        "gh",
        "pr",
        "view",
        str(pr),
        "--repo",
        repo,
        "--json",
        "state,isDraft,headRefOid,labels,statusCheckRollup,url",
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=45,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"gh pr view failed ({completed.returncode}): {detail}")
    return json.loads(completed.stdout)


def _check_name(item: dict[str, Any]) -> str:
    workflow = str(item.get("workflowName") or "").strip()
    name = str(item.get("name") or "unnamed check").strip()
    return f"{workflow} / {name}" if workflow and workflow not in name else name


def _classify(
    payload: dict[str, Any], expected_head: str, authority: str
) -> dict[str, Any]:
    actual_head = str(payload.get("headRefOid") or "")
    state = str(payload.get("state") or "UNKNOWN").upper()
    result: dict[str, Any] = {
        "schema": 1,
        "classification": "pending",
        "state": state,
        "draft": bool(payload.get("isDraft")),
        "head_sha": actual_head,
        "authority": authority,
        "technical_failures": [],
        "human_gates": [],
        "pending_checks": [],
        "reviewer_required": False,
        "auto_merge_eligible": False,
        "url": payload.get("url"),
    }

    if state != "OPEN":
        result["classification"] = "stale"
        result["reason"] = f"PR state is {state}, not OPEN"
        return result
    if actual_head != expected_head:
        result["classification"] = "stale"
        result["reason"] = "PR head SHA changed"
        return result
    if result["draft"]:
        result["classification"] = "issues"
        result["human_gates"].append(
            {"name": "PR is draft", "conclusion": "ACTION_REQUIRED"}
        )
        result["reviewer_required"] = True
        return result

    checks = payload.get("statusCheckRollup") or []
    aggregate_success = False
    aggregate_failure: dict[str, Any] | None = None
    for item in checks:
        name = _check_name(item)
        raw_name = str(item.get("name") or "")
        status = str(item.get("status") or "").upper()
        conclusion = str(item.get("conclusion") or "").upper()
        record = {
            "name": name,
            "conclusion": conclusion or None,
            "url": item.get("detailsUrl"),
        }
        if raw_name == AGGREGATE_CHECK:
            aggregate_success = status == "COMPLETED" and conclusion == "SUCCESS"
            if status == "COMPLETED" and conclusion in FAIL_CONCLUSIONS:
                aggregate_failure = record
            elif status != "COMPLETED":
                result["pending_checks"].append(record)
            continue
        if status != "COMPLETED" or not conclusion:
            result["pending_checks"].append(record)
            continue
        if conclusion in PASS_CONCLUSIONS:
            continue
        if "review label gate" in name.lower() or conclusion == "ACTION_REQUIRED":
            result["human_gates"].append(record)
        elif conclusion in FAIL_CONCLUSIONS or conclusion not in PASS_CONCLUSIONS:
            result["technical_failures"].append(record)

    if (
        aggregate_failure
        and not result["technical_failures"]
        and not result["human_gates"]
    ):
        result["technical_failures"].append(aggregate_failure)

    if result["technical_failures"] or result["human_gates"]:
        result["classification"] = "issues"
        result["reviewer_required"] = True
    elif result["pending_checks"] or not checks or not aggregate_success:
        result["classification"] = "pending"
        if checks and not aggregate_success:
            result["pending_checks"].append(
                {"name": AGGREGATE_CHECK, "conclusion": None, "url": None}
            )
    else:
        result["classification"] = "clean"
        result["auto_merge_eligible"] = authority == "auto_clean"
    return result


def _fingerprint(result: dict[str, Any], repo: str, pr: int, head: str) -> str:
    material = {
        "repo": repo,
        "pr": pr,
        "head": head,
        "classification": result["classification"],
        "technical_failures": result["technical_failures"],
        "human_gates": result["human_gates"],
        "pending_checks": result["pending_checks"],
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                events.append(json.loads(line))
            except (json.JSONDecodeError, TypeError):
                continue
    return events


def _append_event(state_dir: Path, event: dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_dir, 0o700)
    path = state_dir / "events.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    os.chmod(path, 0o600)


def _check(args: argparse.Namespace) -> int:
    deadline = time.monotonic() + max(args.wait_seconds, 0)
    last_result: dict[str, Any] | None = None
    while True:
        try:
            payload = _run_gh(args.repo, args.pr)
            last_result = _classify(payload, args.head, args.authority)
        except Exception as exc:
            last_result = {
                "schema": 1,
                "classification": "error",
                "state": "UNKNOWN",
                "head_sha": None,
                "authority": args.authority,
                "technical_failures": [],
                "human_gates": [],
                "pending_checks": [],
                "reviewer_required": False,
                "auto_merge_eligible": False,
                "reason": str(exc),
            }
        if last_result["classification"] != "pending":
            break
        if time.monotonic() >= deadline:
            last_result["classification"] = "timeout"
            last_result["reason"] = (
                "required checks did not reach a terminal state before timeout"
            )
            break
        time.sleep(max(args.poll_seconds, 1))

    fingerprint = _fingerprint(last_result, args.repo, args.pr, args.head)
    last_result.update(
        {
            "repo": args.repo,
            "pr": args.pr,
            "expected_head": args.head,
            "fingerprint": fingerprint,
        }
    )
    events_path = Path(args.state_dir) / "events.jsonl"
    last_result["duplicate_reviewer"] = any(
        event.get("event") == "reviewer_staged"
        and event.get("fingerprint") == fingerprint
        for event in _read_events(events_path)
    )
    if not args.dry_run:
        _append_event(
            Path(args.state_dir),
            {
                "ts": _now(),
                "event": "gate_check",
                "repo": args.repo,
                "pr": args.pr,
                "head": args.head,
                "authority": args.authority,
                "classification": last_result["classification"],
                "fingerprint": fingerprint,
            },
        )
    print(json.dumps(last_result, sort_keys=True, separators=(",", ":")))
    return {"clean": 0, "issues": 10, "timeout": 20, "stale": 30, "error": 31}.get(
        last_result["classification"], 31
    )


def _mark_reviewer(args: argparse.Namespace) -> int:
    _append_event(
        Path(args.state_dir),
        {
            "ts": _now(),
            "event": "reviewer_staged",
            "repo": args.repo,
            "pr": args.pr,
            "head": args.head,
            "authority": args.authority,
            "fingerprint": args.fingerprint,
            "job_id": args.job_id,
        },
    )
    print(
        json.dumps({"recorded": True, "fingerprint": args.fingerprint}, sort_keys=True)
    )
    return 0


def _parse_selection(args: argparse.Namespace) -> int:
    text = " ".join(args.text.strip().lower().split())
    available = [
        item.strip().upper() for item in args.available.split(",") if item.strip()
    ]
    recommended = [
        item.strip().upper() for item in args.recommended.split(",") if item.strip()
    ]
    result: dict[str, Any] = {"action": "clarify", "selected": [], "unknown": []}
    if text in {"1", "all", "approve all", "apply all", "all recommended"}:
        result = {"action": "apply", "selected": recommended, "unknown": []}
    elif text in {"2", "leave", "leave it", "leave unchanged", "skip", "no changes"}:
        result = {"action": "leave", "selected": [], "unknown": []}
    elif text.startswith("explain"):
        selected = re.findall(r"\b([a-z])\b", text, flags=re.IGNORECASE)
        selected = [item.upper() for item in selected]
        unknown = [item for item in selected if item not in available]
        result = {
            "action": "explain" if selected and not unknown else "clarify",
            "selected": selected,
            "unknown": unknown,
        }
    else:
        selected = [
            item.upper()
            for item in re.findall(r"\b([a-z])\b", text, flags=re.IGNORECASE)
        ]
        selected = list(dict.fromkeys(selected))
        unknown = [item for item in selected if item not in available]
        if selected and not unknown:
            result = {"action": "apply", "selected": selected, "unknown": []}
        else:
            result = {"action": "clarify", "selected": selected, "unknown": unknown}
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["action"] != "clarify" else 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="wait for and classify one exact PR head")
    check.add_argument("--repo", required=True)
    check.add_argument("--pr", required=True, type=int)
    check.add_argument("--head", required=True)
    check.add_argument("--authority", required=True, choices=["auto_clean", "jid_only"])
    check.add_argument("--wait-seconds", type=int, default=1800)
    check.add_argument("--poll-seconds", type=int, default=30)
    check.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    check.add_argument("--dry-run", action="store_true")
    check.set_defaults(func=_check)

    mark = sub.add_parser("mark-reviewer", help="record that a reviewer job was staged")
    mark.add_argument("--repo", required=True)
    mark.add_argument("--pr", required=True, type=int)
    mark.add_argument("--head", required=True)
    mark.add_argument("--authority", required=True, choices=["auto_clean", "jid_only"])
    mark.add_argument("--fingerprint", required=True)
    mark.add_argument("--job-id", required=True)
    mark.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    mark.set_defaults(func=_mark_reviewer)

    selection = sub.add_parser(
        "parse-selection", help="parse a tagged quote-reply selection"
    )
    selection.add_argument("--text", required=True)
    selection.add_argument("--available", required=True)
    selection.add_argument("--recommended", required=True)
    selection.set_defaults(func=_parse_selection)
    return parser


def main() -> int:
    args = _parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
