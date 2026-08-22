from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol


class Verdict(str, Enum):
    CLEAR = "clear"
    DUPLICATE = "duplicate"
    ASSIGNED = "assigned"
    CLA_REQUIRED = "cla_required"
    NOT_FOUND = "not_found"


class GitHubReader(Protocol):
    def get_issue(self, owner: str, repo: str, number: int) -> dict | None: ...
    def get_issue_timeline(self, owner: str, repo: str, number: int) -> list[dict]: ...
    def get_repo_file_text(self, owner: str, repo: str, path: str) -> str | None: ...


CONTRIBUTING_PATHS = ["CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md"]
CLA_MARKERS = ["contributor license agreement", "cla-assistant", "sign the cla"]
DCO_MARKERS = ["developer certificate of origin", "signed-off-by"]


@dataclass
class ScreenResult:
    owner: str
    repo: str
    issue: int
    verdict: Verdict
    reasons: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    checked_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["verdict"] = self.verdict.value
        return d


def _find_duplicate_prs(timeline: list[dict]) -> list[str]:
    urls = []
    for event in timeline:
        if event.get("event") != "cross-referenced":
            continue
        pr_issue = event.get("source", {}).get("issue", {})
        if "pull_request" in pr_issue and pr_issue.get("html_url"):
            urls.append(pr_issue["html_url"])
    return urls


def _detect_contribution_norm(text: str) -> str | None:
    lowered = text.lower()
    if any(marker in lowered for marker in CLA_MARKERS):
        return "cla"
    if any(marker in lowered for marker in DCO_MARKERS):
        return "dco"
    return None


def screen_issue(
    owner: str,
    repo: str,
    number: int,
    client: GitHubReader,
    known_signed_orgs: frozenset[str] = frozenset(),
) -> ScreenResult:
    issue = client.get_issue(owner, repo, number)
    if issue is None:
        return ScreenResult(owner, repo, number, Verdict.NOT_FOUND, ["issue not found"])

    reasons: list[str] = []
    evidence: dict = {"title": issue.get("title"), "state": issue.get("state")}

    assignees = [a["login"] for a in (issue.get("assignees") or [])]
    if assignees:
        evidence["assignees"] = assignees
        reasons.append(f"assigned to {', '.join(assignees)}")

    duplicate_prs = _find_duplicate_prs(client.get_issue_timeline(owner, repo, number))
    if duplicate_prs:
        evidence["duplicate_prs"] = duplicate_prs
        reasons.append(f"{len(duplicate_prs)} PR(s) already reference this issue")

    contribution_norm = None
    for path in CONTRIBUTING_PATHS:
        text = client.get_repo_file_text(owner, repo, path)
        if text:
            evidence["contributing_path"] = path
            contribution_norm = _detect_contribution_norm(text)
            break
    if contribution_norm:
        evidence["contribution_norm"] = contribution_norm

    if duplicate_prs:
        verdict = Verdict.DUPLICATE
    elif assignees:
        verdict = Verdict.ASSIGNED
    elif contribution_norm == "cla" and owner.lower() not in known_signed_orgs:
        verdict = Verdict.CLA_REQUIRED
        reasons.append("CONTRIBUTING.md requires a CLA and this org isn't in known_signed_orgs")
    else:
        verdict = Verdict.CLEAR

    return ScreenResult(owner, repo, number, verdict, reasons, evidence)
