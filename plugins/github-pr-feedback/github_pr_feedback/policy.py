"""Fail-closed admission policy for GitHub feedback receipts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import subprocess
from typing import Mapping, Sequence


_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_FEEDBACK_KINDS = frozenset({"issue_comment", "review_comment", "review"})


def _nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _repository(value: object, field: str) -> str:
    repository = _nonempty_string(value, field)
    if not _REPOSITORY.fullmatch(repository):
        raise ValueError(f"{field} must be an exact owner/repository name")
    return repository


def _string_list(value: object, field: str, *, normalize=str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{field} must be a non-empty list of strings")
    items = tuple(normalize(_nonempty_string(item, field)) for item in value)
    if not items:
        raise ValueError(f"{field} must not be empty")
    return items


def _is_git_worktree(path: Path) -> bool:
    """Accept only a Git worktree root, including a linked-worktree `.git` file."""

    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree", "--show-toplevel"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    lines = result.stdout.splitlines()
    return result.returncode == 0 and lines == ["true", str(path.resolve())]


@dataclass(frozen=True, slots=True)
class FeedbackReceipt:
    """The immutable, head-scoped identity of one item of review feedback."""

    repository: str
    pr_number: int
    feedback_kind: str
    feedback_id: str
    head_sha: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "repository", _repository(self.repository, "repository"))
        if not isinstance(self.pr_number, int) or isinstance(self.pr_number, bool) or self.pr_number < 1:
            raise ValueError("pr_number must be a positive integer")
        if self.feedback_kind not in _FEEDBACK_KINDS:
            raise ValueError("feedback_kind is not supported")
        object.__setattr__(self, "feedback_id", _nonempty_string(self.feedback_id, "feedback_id"))
        object.__setattr__(self, "head_sha", _nonempty_string(self.head_sha, "head_sha"))

    @property
    def key(self) -> tuple[str, int, str, str, str]:
        return (self.repository, self.pr_number, self.feedback_kind, self.feedback_id, self.head_sha)


@dataclass(frozen=True, slots=True)
class PullRequest:
    """Canonical PR fields required to make an admission decision."""

    number: int
    state: str
    base_repository: str
    head_repository: str
    author_login: str
    head_ref_name: str
    head_sha: str

    def __post_init__(self) -> None:
        if not isinstance(self.number, int) or isinstance(self.number, bool) or self.number < 1:
            raise ValueError("number must be a positive integer")
        object.__setattr__(self, "state", _nonempty_string(self.state, "state").upper())
        object.__setattr__(self, "base_repository", _repository(self.base_repository, "base_repository"))
        object.__setattr__(self, "head_repository", _repository(self.head_repository, "head_repository"))
        object.__setattr__(self, "author_login", _nonempty_string(self.author_login, "author_login"))
        object.__setattr__(self, "head_ref_name", _nonempty_string(self.head_ref_name, "head_ref_name"))
        object.__setattr__(self, "head_sha", _nonempty_string(self.head_sha, "head_sha"))


@dataclass(frozen=True, slots=True)
class Reviewer:
    login: str
    association: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "login", _nonempty_string(self.login, "reviewer login"))
        if self.association is not None:
            object.__setattr__(
                self,
                "association",
                _nonempty_string(self.association, "reviewer association").upper(),
            )


@dataclass(frozen=True, slots=True)
class RepositoryTarget:
    base_repository: str
    head_repository: str
    local_path: Path
    owner_login: str
    branch_prefixes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Admission:
    admitted: bool
    reason: str | None = None
    target: RepositoryTarget | None = None


@dataclass(frozen=True, slots=True)
class PluginPolicy:
    enabled: bool
    targets: Mapping[str, RepositoryTarget]
    reviewer_logins: frozenset[str]
    reviewer_associations: frozenset[str]
    include_self_feedback: bool
    include_bot_feedback: bool
    not_before: datetime | None
    assignee: str | None
    board: str | None

    def admit_pull_request(self, pull_request: PullRequest) -> Admission:
        """Admit only an exact configured PR before reading any feedback bodies."""

        if not self.enabled:
            return Admission(False, "disabled")
        target = self.targets.get(pull_request.base_repository)
        if target is None:
            return Admission(False, "base_repository_not_allowed")
        if pull_request.head_repository != target.head_repository:
            return Admission(False, "head_repository_not_allowed")
        if pull_request.state != "OPEN":
            return Admission(False, "pull_request_not_open")
        if pull_request.author_login.casefold() != target.owner_login.casefold():
            return Admission(False, "author_not_allowed")
        if not any(pull_request.head_ref_name.startswith(prefix) for prefix in target.branch_prefixes):
            return Admission(False, "branch_not_allowed")
        return Admission(True, target=target)

    def admit(
        self,
        pull_request: PullRequest,
        reviewer: Reviewer,
        receipt: FeedbackReceipt,
        *,
        is_bot: bool = False,
    ) -> Admission:
        pull_request_admission = self.admit_pull_request(pull_request)
        target = pull_request_admission.target
        if not pull_request_admission.admitted or target is None:
            return pull_request_admission
        if receipt.repository != pull_request.base_repository:
            return Admission(False, "base_repository_not_allowed")
        trusted_login = reviewer.login.casefold() in self.reviewer_logins
        trusted_association = (reviewer.association or "") in self.reviewer_associations
        trusted_self = (
            self.include_self_feedback
            and reviewer.login.casefold() == target.owner_login.casefold()
        )
        trusted_bot = self.include_bot_feedback and is_bot
        if not (trusted_login or trusted_association or trusted_self or trusted_bot):
            return Admission(False, "reviewer_not_allowed")
        if receipt.pr_number != pull_request.number or receipt.head_sha != pull_request.head_sha:
            return Admission(False, "head_changed")
        return Admission(True, target=target)


def _parse_target(raw: object) -> RepositoryTarget:
    if not isinstance(raw, Mapping):
        raise ValueError("repositories entries must be mappings")
    expected = {"base_repository", "head_repository", "local_path", "owner_login", "branch_prefixes"}
    if set(raw) != expected:
        raise ValueError("repository target has missing or unknown fields")
    path = Path(_nonempty_string(raw["local_path"], "local_path"))
    if not path.is_absolute() or not path.is_dir() or not _is_git_worktree(path):
        raise ValueError("local_path must be an existing local Git repository")
    prefixes = _string_list(raw["branch_prefixes"], "branch_prefixes")
    if any(prefix.startswith("refs/") or any(char.isspace() for char in prefix) for prefix in prefixes):
        raise ValueError("branch_prefixes must be literal branch prefixes")
    return RepositoryTarget(
        base_repository=_repository(raw["base_repository"], "base_repository"),
        head_repository=_repository(raw["head_repository"], "head_repository"),
        local_path=path.resolve(),
        owner_login=_nonempty_string(raw["owner_login"], "owner_login"),
        branch_prefixes=prefixes,
    )


def _not_before(value: object) -> datetime:
    text = _nonempty_string(value, "not_before")
    try:
        boundary = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("not_before must be ISO-8601") from error
    if boundary.tzinfo is None:
        raise ValueError("not_before must include a timezone")
    return boundary.astimezone(timezone.utc)


def load_policy(raw: object) -> PluginPolicy:
    """Parse plugin configuration, retaining no enabled behavior on any omission."""

    if not isinstance(raw, Mapping):
        raise ValueError("plugin configuration must be a mapping")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")
    if not enabled:
        return PluginPolicy(False, {}, frozenset(), frozenset(), False, False, None, None, None)
    required = {
        "enabled",
        "repositories",
        "reviewer_logins",
        "reviewer_associations",
        "not_before",
        "assignee",
        "board",
    }
    optional = {"include_self_feedback", "include_bot_feedback"}
    if not required.issubset(raw) or set(raw) - required - optional:
        raise ValueError("enabled configuration has missing or unknown fields")
    include_self_feedback = raw.get("include_self_feedback", False)
    include_bot_feedback = raw.get("include_bot_feedback", False)
    if not isinstance(include_self_feedback, bool) or not isinstance(include_bot_feedback, bool):
        raise ValueError("feedback inclusion settings must be booleans")
    repositories = raw["repositories"]
    if isinstance(repositories, (str, bytes)) or not isinstance(repositories, Sequence):
        raise ValueError("repositories must be a non-empty list")
    parsed_targets = tuple(_parse_target(target) for target in repositories)
    if not parsed_targets:
        raise ValueError("repositories must not be empty")
    targets = {target.base_repository: target for target in parsed_targets}
    if len(targets) != len(parsed_targets):
        raise ValueError("each base_repository may have only one configured head_repository")
    reviewer_logins = (
        frozenset()
        if raw["reviewer_logins"] == []
        else frozenset(_string_list(raw["reviewer_logins"], "reviewer_logins", normalize=str.casefold))
    )
    reviewer_associations = (
        frozenset()
        if raw["reviewer_associations"] == []
        else frozenset(
            _string_list(raw["reviewer_associations"], "reviewer_associations", normalize=str.upper)
        )
    )
    if not reviewer_logins and not reviewer_associations:
        raise ValueError("at least one reviewer login or association is required")
    return PluginPolicy(
        True,
        targets,
        reviewer_logins,
        reviewer_associations,
        include_self_feedback,
        include_bot_feedback,
        _not_before(raw["not_before"]),
        _nonempty_string(raw["assignee"], "assignee"),
        _nonempty_string(raw["board"], "board"),
    )
