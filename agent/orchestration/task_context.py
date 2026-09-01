from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


_SENSITIVE_MARKERS = (
    "api_key",
    "apikey",
    "secret",
    "token",
    "password",
    "passwd",
    "credential",
    "authorization:",
    "bearer ",
)


def sanitize_text(value: Any, *, max_chars: int = 12000) -> str:
    """Return a bounded string with obvious secret-bearing lines redacted.

    Hermes already has global secret redaction, but orchestration logs and
    handoffs should be conservative at the source as well.  This line-based
    sanitizer avoids copying .env contents, Authorization headers, or token-like
    config into TaskContext markdown.
    """
    if value is None:
        return ""
    text = str(value)
    lines: list[str] = []
    for line in text.splitlines():
        lowered = line.lower()
        if any(marker in lowered for marker in _SENSITIVE_MARKERS):
            lines.append("[REDACTED sensitive line]")
        else:
            lines.append(line)
    cleaned = "\n".join(lines).strip()
    if len(cleaned) > max_chars:
        return cleaned[:max_chars].rstrip() + "\n...[truncated]"
    return cleaned


def sanitize_value(value: Any, *, max_chars: int = 12000) -> Any:
    """Recursively sanitize strings inside JSON-like values."""
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            if len(key_text) > 200:
                key_text = key_text[:200]
            lowered = key_text.lower()
            if any(marker.strip().rstrip(":") in lowered for marker in _SENSITIVE_MARKERS):
                cleaned[key_text] = "[REDACTED]"
            else:
                cleaned[key_text] = sanitize_value(child, max_chars=max_chars)
        return cleaned
    if isinstance(value, list):
        return [sanitize_value(item, max_chars=max_chars) for item in value]
    if isinstance(value, tuple):
        return [sanitize_value(item, max_chars=max_chars) for item in value]
    if isinstance(value, str):
        return sanitize_text(value, max_chars=max_chars)
    return value


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [sanitize_text(v, max_chars=2000) for v in value if sanitize_text(v)]
    text = sanitize_text(value, max_chars=6000)
    return [text] if text else []


@dataclass
class TaskContext:
    """Shared context passed between specialized subagents.

    The object is intentionally serializable and text-renderable so it works for
    both synchronous ``delegate_task`` handoffs and durable Kanban tasks.
    """

    issue_id: str = ""
    issue_title: str = ""
    issue_description: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    repository: str = ""
    branch: str = ""
    current_status: str = ""
    relevant_files: list[str] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    developer_findings: list[str] = field(default_factory=list)
    test_results: list[str] = field(default_factory=list)
    reviewer_findings: list[str] = field(default_factory=list)
    open_problems: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> "TaskContext":
        data = data or {}
        return cls(
            issue_id=sanitize_text(data.get("issue_id") or data.get("IssueId"), max_chars=200),
            issue_title=sanitize_text(data.get("issue_title") or data.get("IssueTitle"), max_chars=500),
            issue_description=sanitize_text(data.get("issue_description") or data.get("IssueDescription"), max_chars=12000),
            acceptance_criteria=_as_list(data.get("acceptance_criteria") or data.get("AcceptanceCriteria")),
            repository=sanitize_text(data.get("repository") or data.get("Repository"), max_chars=1000),
            branch=sanitize_text(data.get("branch") or data.get("Branch"), max_chars=500),
            current_status=sanitize_text(data.get("current_status") or data.get("CurrentStatus"), max_chars=2000),
            relevant_files=_as_list(data.get("relevant_files") or data.get("RelevantFiles")),
            logs=_as_list(data.get("logs") or data.get("Logs")),
            screenshots=_as_list(data.get("screenshots") or data.get("Screenshots")),
            developer_findings=_as_list(data.get("developer_findings") or data.get("DeveloperFindings")),
            test_results=_as_list(data.get("test_results") or data.get("TestResults")),
            reviewer_findings=_as_list(data.get("reviewer_findings") or data.get("ReviewerFindings")),
            open_problems=_as_list(data.get("open_problems") or data.get("OpenProblems")),
            decisions=_as_list(data.get("decisions") or data.get("Decisions")),
            metadata=sanitize_value(data.get("metadata") or data.get("Metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "IssueId": self.issue_id,
            "IssueTitle": self.issue_title,
            "IssueDescription": self.issue_description,
            "AcceptanceCriteria": list(self.acceptance_criteria),
            "Repository": self.repository,
            "Branch": self.branch,
            "CurrentStatus": self.current_status,
            "RelevantFiles": list(self.relevant_files),
            "Logs": list(self.logs),
            "Screenshots": list(self.screenshots),
            "DeveloperFindings": list(self.developer_findings),
            "TestResults": list(self.test_results),
            "ReviewerFindings": list(self.reviewer_findings),
            "OpenProblems": list(self.open_problems),
            "Decisions": list(self.decisions),
            "Metadata": dict(self.metadata),
        }

    def render_markdown(self) -> str:
        def block(title: str, value: str) -> str:
            return f"## {title}\n{value.strip() or '–'}\n"

        def bullets(title: str, values: list[str]) -> str:
            if not values:
                return f"## {title}\n–\n"
            return f"## {title}\n" + "\n".join(f"- {v}" for v in values) + "\n"

        return "\n".join(
            [
                block("IssueId", self.issue_id),
                block("IssueTitle", self.issue_title),
                block("IssueDescription", self.issue_description),
                bullets("AcceptanceCriteria", self.acceptance_criteria),
                block("Repository", self.repository),
                block("Branch", self.branch),
                block("CurrentStatus", self.current_status),
                bullets("RelevantFiles", self.relevant_files),
                bullets("Logs", self.logs),
                bullets("Screenshots", self.screenshots),
                bullets("DeveloperFindings", self.developer_findings),
                bullets("TestResults", self.test_results),
                bullets("ReviewerFindings", self.reviewer_findings),
                bullets("OpenProblems", self.open_problems),
                bullets("Decisions", self.decisions),
            ]
        ).strip()

    def append_handoff(self, role: str, summary: str) -> None:
        role_key = role.lower().strip()
        sanitized = sanitize_text(summary, max_chars=12000)
        if role_key == "developer":
            self.developer_findings.append(sanitized)
        elif role_key in {"tester", "qa", "test"}:
            self.test_results.append(sanitized)
        elif role_key == "reviewer":
            self.reviewer_findings.append(sanitized)
        elif role_key == "debugger":
            self.open_problems.append(f"Debug: {sanitized}")
        else:
            self.decisions.append(f"{role}: {sanitized}")
