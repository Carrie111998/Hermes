"""Deterministic audit of durable context and measured learning state."""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home


def _finding(kind: str, severity: str, subject: str, message: str) -> dict[str, str]:
    return {
        "kind": kind,
        "severity": severity,
        "subject": subject,
        "message": message,
    }


def _memory_findings() -> tuple[list[dict[str, str]], dict[str, int]]:
    findings: list[dict[str, str]] = []
    stats = {"memory_chunks": 0, "memory_chars": 0}
    base = get_hermes_home() / "memories"
    normalized: list[str] = []
    for name in ("MEMORY.md", "USER.md"):
        path = base / name
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        stats["memory_chars"] += len(text)
        for chunk in (part.strip() for part in text.split("\n§\n")):
            if not chunk:
                continue
            stats["memory_chunks"] += 1
            normalized.append(" ".join(chunk.lower().split()))
    for text, count in Counter(normalized).items():
        if count < 2:
            continue
        subject = "memory:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        findings.append(
            _finding(
                "duplicate_memory",
                "medium",
                subject,
                f"The same durable memory appears {count} times.",
            )
        )
    return findings, stats


def _candidate_findings() -> tuple[list[dict[str, str]], dict[str, int]]:
    from agent import learning_ledger

    findings: list[dict[str, str]] = []
    candidates = learning_ledger.list_candidates()
    stats = {
        "candidates": len(candidates),
        "pending_candidates": 0,
        "validated_candidates": 0,
    }
    stale_before = datetime.now(timezone.utc) - timedelta(days=7)
    for candidate in candidates:
        candidate_id = candidate["candidate_id"]
        status = candidate["status"]
        events = learning_ledger.list_events(candidate_id=candidate_id)
        outcomes = [event for event in events if event["event"].startswith("outcome_")]
        if status in {"pending", "applying"}:
            stats["pending_candidates"] += 1
            try:
                created = datetime.fromisoformat(str(candidate["created_at"]).replace("Z", "+00:00"))
            except Exception:
                created = None
            if created is not None and created < stale_before:
                findings.append(
                    _finding("stale_candidate", "medium", candidate_id, "Learning candidate has awaited disposition for more than seven days.")
                )
        if status == "validated":
            stats["validated_candidates"] += 1
        if status == "active" and not outcomes:
            findings.append(
                _finding("unvalidated_learning", "medium", candidate_id, "Applied learning has no recorded outcome evidence.")
            )
        failed = [
            event for event in outcomes
            if event["event"] in {"outcome_verification_failed", "outcome_user_corrected", "outcome_retry_failed"}
        ]
        if failed:
            findings.append(
                _finding("failed_learning_outcome", "high", candidate_id, f"Applied learning has {len(failed)} negative outcome receipt(s).")
            )
    return findings, stats


def audit_context() -> dict[str, Any]:
    findings, stats = _memory_findings()
    candidate_findings, candidate_stats = _candidate_findings()
    findings.extend(candidate_findings)
    stats.update(candidate_stats)
    try:
        from agent.trace_compiler import discover_compilation_proposals

        proposals = discover_compilation_proposals()
    except Exception:
        proposals = []
    stats["compilation_candidates"] = len(proposals)
    for proposal in proposals:
        findings.append(
            _finding(
                "compilation_candidate",
                "low",
                proposal["id"],
                "Repeated successful deterministic workflow is ready for human review.",
            )
        )
    weights = {"low": 3, "medium": 10, "high": 20}
    score = max(0, 100 - sum(weights.get(item["severity"], 5) for item in findings))
    findings.sort(key=lambda item: ({"high": 0, "medium": 1, "low": 2}.get(item["severity"], 3), item["kind"], item["subject"]))
    return {"score": score, "findings": findings, "stats": stats}


def format_context_audit() -> str:
    result = audit_context()
    lines = [
        f"Context Health: score={result['score']}/100, findings={len(result['findings'])}",
        (
            f"Memory: {result['stats']['memory_chunks']} chunks / "
            f"{result['stats']['memory_chars']} chars · "
            f"Learning: {result['stats']['candidates']} candidates, "
            f"{result['stats']['validated_candidates']} validated"
        ),
    ]
    if not result["findings"]:
        lines.append("No deterministic context-health issues found.")
    else:
        for item in result["findings"]:
            lines.append(
                f"- [{item['severity']}] {item['kind']} ({item['subject']}): {item['message']}"
            )
    return "\n".join(lines)
