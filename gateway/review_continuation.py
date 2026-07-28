"""Bounded continuations for successful review tasks with actionable findings."""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("gateway.run")

DEFAULT_REVIEW_CONTINUATION_ENABLED = True
DEFAULT_REVIEW_CONTINUATION_MAX_CHAIN = 2
DEFAULT_REQUIRED_SEVERITY = "high"
_MAX_ARTIFACT_BYTES = 1_000_000

_SEVERITY_RANK = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}
_REVIEW_ROLE_TOKENS = {"audit", "auditor", "review", "reviewer"}
_FINDINGS_MARKER_RE = re.compile(
    r"(?im)^\s*(?:#{1,6}\s*)?(?:primary\s+)?findings?\s*:"
    r"|^\s*#{1,6}\s+(?:primary\s+)?findings?\s*$"
)
_REAL_CLASSIFICATION_RE = re.compile(
    r"(?i)\b(?:classification|class|disposition)\s*[:=-]\s*"
    r"(?:real[_ -]?now|real[_ -]?later)\b"
)
_SEVERITY_RE = re.compile(
    r"(?i)(?:"
    r"\bseverity\s*[:=-]\s*(critical|high|medium|low)\b"
    r"|\[\s*(critical|high|medium|low)\s*\]"
    r"|\b(critical|high|medium|low)\s+severity\b"
    r")"
)


def resolve_review_continuation_settings(cfg: Any) -> dict:
    """Extract ``worker_bridge.review_continuation`` with safe defaults."""
    wb_cfg = cfg.get("worker_bridge", {}) if isinstance(cfg, dict) else {}
    raw = wb_cfg.get("review_continuation", {}) if isinstance(wb_cfg, dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    try:
        max_chain = max(
            0,
            int(raw.get("max_chain", DEFAULT_REVIEW_CONTINUATION_MAX_CHAIN)),
        )
    except (TypeError, ValueError):
        max_chain = DEFAULT_REVIEW_CONTINUATION_MAX_CHAIN
    require_severity = str(
        raw.get("require_severity", DEFAULT_REQUIRED_SEVERITY)
    ).strip().lower()
    if require_severity not in _SEVERITY_RANK:
        require_severity = DEFAULT_REQUIRED_SEVERITY
    return {
        "enabled": raw.get("enabled", DEFAULT_REVIEW_CONTINUATION_ENABLED) is not False,
        "max_chain": max_chain,
        "require_severity": require_severity,
    }


def _task_summary(task: dict) -> str:
    result = task.get("result") or {}
    if not isinstance(result, dict):
        return ""
    return str(result.get("summary") or "").strip()


def _artifact_values(result: dict) -> Iterable[Any]:
    for key in ("artifacts", "artifact_paths", "artifact", "artifact_path"):
        value = result.get(key)
        if isinstance(value, (list, tuple)):
            yield from value
        elif value:
            yield value


def _artifact_path(value: Any) -> Path | None:
    if isinstance(value, dict):
        value = value.get("path") or value.get("file_path")
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value.strip())
    return path if path.suffix.lower() == ".md" else None


def _read_markdown_artifacts(task: dict) -> list[str]:
    result = task.get("result") or {}
    if not isinstance(result, dict):
        return []
    contents: list[str] = []
    seen: set[str] = set()
    for value in _artifact_values(result):
        path = _artifact_path(value)
        if path is None:
            continue
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            if path.stat().st_size > _MAX_ARTIFACT_BYTES:
                logger.warning("review continuation skipped oversized artifact %s", path)
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            logger.debug(
                "review continuation could not read artifact %s",
                path,
                exc_info=True,
            )
            continue
        if text.strip():
            contents.append(text.strip())
    return contents


def _is_review_task(spec: dict, metadata: dict, summary: str) -> bool:
    if str(metadata.get("continuation_kind") or "").strip().lower() == "fix":
        return False
    kind = str(metadata.get("continuation_kind") or "").strip().lower()
    if kind == "review":
        return True
    for key in ("role", "type"):
        role = re.sub(r"[^a-z0-9]+", "_", str(metadata.get(key) or "").lower()).strip(
            "_"
        )
        if set(role.split("_")) & _REVIEW_ROLE_TOKENS:
            return True
    objective = str(spec.get("objective") or "")
    return bool(_FINDINGS_MARKER_RE.search(objective) or _FINDINGS_MARKER_RE.search(summary))


def _actionable_findings(texts: Iterable[str], require_severity: str) -> str:
    """Return explicitly classified actionable finding lines/blocks."""
    threshold = _SEVERITY_RANK[require_severity]
    findings: list[str] = []
    for text in texts:
        blocks = re.split(r"\n\s*\n", text)
        for block in blocks:
            normalized = block.strip()
            if not normalized:
                continue
            real = bool(_REAL_CLASSIFICATION_RE.search(normalized))
            severities = []
            for match in _SEVERITY_RE.finditer(normalized):
                label = next(group for group in match.groups() if group)
                severities.append(_SEVERITY_RANK[label.lower()])
            severe = bool(severities and max(severities) >= threshold)
            if real or severe:
                findings.append(normalized)
    return "\n\n".join(dict.fromkeys(findings))


def _render_fix_objective(template: str, findings: str, review_task_id: str) -> str:
    rendered = template.replace("{findings}", findings).replace(
        "{review_task_id}", review_task_id
    )
    if "{findings}" not in template:
        rendered = f"{rendered.rstrip()}\n\nReview findings:\n{findings}"
    return rendered.strip()


def create_review_continuations(bridge: Any, settings: dict) -> int:
    """Create one idempotent fix task for each opted-in actionable review."""
    if not settings.get("enabled"):
        return 0
    max_chain = max(0, int(settings.get("max_chain", 0)))
    require_severity = str(
        settings.get("require_severity") or DEFAULT_REQUIRED_SEVERITY
    ).lower()
    if require_severity not in _SEVERITY_RANK:
        require_severity = DEFAULT_REQUIRED_SEVERITY

    tasks = bridge.list_tasks(limit=10_000)
    continued_reviews = {
        str((task.get("spec") or {}).get("metadata", {}).get("source_review"))
        for task in tasks
        if isinstance(task.get("spec"), dict)
        and isinstance((task.get("spec") or {}).get("metadata"), dict)
        and (task["spec"]["metadata"].get("continuation_kind") == "fix")
        and task["spec"]["metadata"].get("source_review")
    }
    created = 0
    for task in tasks:
        if str(task.get("status") or "").lower() != "succeeded":
            continue
        task_id = str(task.get("task_id") or "").strip()
        spec = task.get("spec") or {}
        if not task_id or not isinstance(spec, dict):
            continue
        metadata = spec.get("metadata") or {}
        if not isinstance(metadata, dict):
            continue
        on_findings = metadata.get("on_findings")
        if not isinstance(on_findings, dict) or on_findings.get("enabled") is not True:
            continue
        template = str(on_findings.get("fix_objective_template") or "").strip()
        if not template or not _is_review_task(spec, metadata, _task_summary(task)):
            continue
        try:
            chain_depth = max(
                0,
                int(
                    metadata.get(
                        "continuation_chain_depth",
                        metadata.get("successor_chain_depth", 0),
                    )
                ),
            )
        except (TypeError, ValueError):
            continue
        if chain_depth >= max_chain or task_id in continued_reviews:
            continue

        texts = _read_markdown_artifacts(task)
        summary = _task_summary(task)
        if summary:
            texts.append(summary)
        findings = _actionable_findings(texts, require_severity)
        if not findings:
            logger.info(
                "worker-bridge review continuation no-op for %s: "
                "no actionable findings at severity %s",
                task_id,
                require_severity,
            )
            continue

        finding_hash = hashlib.sha256(findings.encode("utf-8")).hexdigest()
        successor_metadata = dict(metadata)
        successor_metadata.pop("on_findings", None)
        successor_metadata.update(
            {
                "continuation_kind": "fix",
                "source_review": task_id,
                "finding_hash": finding_hash,
                "continuation_chain_depth": chain_depth + 1,
                "successor_chain_depth": chain_depth + 1,
            }
        )
        successor_spec = dict(spec)
        successor_spec.update(
            {
                "task_id": None,
                "idempotency_key": (
                    f"review-continuation:{task_id}:{finding_hash}"
                ),
                "parent_task_id": task_id,
                "objective": _render_fix_objective(template, findings, task_id),
                "metadata": successor_metadata,
            }
        )
        bridge.create_task(successor_spec)
        continued_reviews.add(task_id)
        created += 1
        logger.info(
            "worker-bridge review continuation created for %s at chain depth %d",
            task_id,
            chain_depth + 1,
        )
    return created
