"""Scoped GitHub and Slack MCP adapters for the Kanban review runner.

Read adapters are enabled independently from the restricted delivery transports.
MCP discovery is scoped to operator-selected server names, repository/channel/user
allowlists are mandatory, and every tool call is bounded by an outer daemon-thread
timeout.  Delivery remains off unless a separate provider delivery gate is enabled;
the write surface contains no merge, approval, branch, file, or channel-management
operation.
"""

from __future__ import annotations

import hashlib
import json
import queue
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Mapping, Optional, Protocol, Sequence, cast

from hermes_constants import get_hermes_home
from hermes_cli import kanban_coderabbit as coderabbit
from hermes_cli import kanban_github as github
from hermes_cli import kanban_slack as slack
from utils import fast_safe_load


MCPFailureKind = Literal[
    "auth",
    "permission",
    "not_found",
    "rate_limited",
    "timeout",
    "unavailable",
    "validation",
]

_GITHUB_READ_TOOLS = frozenset({
    "get_pull_request",
    "get_pull_request_status",
    "get_pull_request_reviews",
    "get_pull_request_comments",
})
_GITHUB_DELIVERY_TOOLS = frozenset({"create_pull_request_review"})
_SLACK_READ_TOOLS = frozenset({"slack_get_thread_replies"})
_SLACK_DELIVERY_TOOLS = frozenset({
    "slack_get_channel_history",
    "slack_get_thread_replies",
    "slack_post_message",
    "slack_reply_to_thread",
})
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_RE = re.compile(r"^[^/\s]+/[^/\s]+$")
_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_ENV_REFERENCE_RE = re.compile(r"^\$\{(?:env:)?[A-Za-z_][A-Za-z0-9_]*\}$")
_ACTIONABLE_COUNT_RE = re.compile(r"\b(\d+)\s+actionable\s+comments?\b", re.IGNORECASE)
_RATE_LIMIT_RE = re.compile(
    r"rate[- ]?limit|quota\s+(?:reached|exceeded)", re.IGNORECASE
)
_SKIPPED_RE = re.compile(
    r"\b(?:review\s+)?skipped\b|skipping\s+(?:this\s+)?review", re.IGNORECASE
)
_PAUSED_RE = re.compile(r"\b(?:review\s+)?paused\b", re.IGNORECASE)
_PENDING_RE = re.compile(
    r"review\s+(?:is\s+)?(?:in\s+progress|pending)|processing\s+review", re.IGNORECASE
)
_NO_ACTIONABLE_RE = re.compile(r"\b(?:no|0)\s+actionable\s+comments?\b", re.IGNORECASE)
_CLEAN_RE = re.compile(
    r"\b(?:review\s+)?clean\b|\bno\s+(?:issues|problems)\s+found\b|\blooks\s+good\b",
    re.IGNORECASE,
)


class MCPAdapterError(RuntimeError):
    """A scoped MCP call failed before producing trusted normalized evidence."""

    def __init__(self, message: str, *, kind: MCPFailureKind) -> None:
        super().__init__(message)
        self.kind: MCPFailureKind = kind


class MCPToolCaller(Protocol):
    """Minimal typed MCP boundary used by provider-specific normalizers."""

    def call(self, tool_name: str, arguments: Mapping[str, Any]) -> Any: ...


def _load_raw_mcp_servers() -> Mapping[str, Any]:
    """Read unexpanded MCP config so credential-source policy can be verified.

    ``load_config()`` expands ``${env:...}`` references.  Runtime diagnostics
    need the unexpanded form to distinguish an approved credential reference
    from plaintext embedded in ``config.yaml``.  Values are never returned in
    diagnostics or exception messages.
    """

    path = get_hermes_home() / "config.yaml"
    try:
        if not path.exists():
            return {}
        parsed = fast_safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise MCPAdapterError(
            "active config cannot be inspected for MCP credential policy",
            kind="validation",
        ) from exc
    if not isinstance(parsed, Mapping):
        raise MCPAdapterError(
            "active config must be an object for MCP credential policy",
            kind="validation",
        )
    servers = parsed.get("mcp_servers")
    return servers if isinstance(servers, Mapping) else {}


def _credential_preflight(
    *,
    provider: Literal["github", "slack"],
    server_name: str,
    raw_server: Mapping[str, Any],
    expanded_server: Mapping[str, Any],
) -> dict[str, Any]:
    """Return redacted credential readiness and fail closed on plaintext secrets."""

    auth_mode = str(raw_server.get("auth") or "").strip().casefold()
    if auth_mode == "oauth":
        return {
            "provider": provider,
            "server_name": server_name,
            "ready": True,
            "auth_mode": "oauth",
            "credential_storage": "oauth_token_store",
            "checked_keys": [],
        }

    raw_env = raw_server.get("env")
    expanded_env = expanded_server.get("env")
    raw_env = raw_env if isinstance(raw_env, Mapping) else {}
    expanded_env = expanded_env if isinstance(expanded_env, Mapping) else {}
    required_keys = (
        ("GITHUB_PERSONAL_ACCESS_TOKEN",)
        if provider == "github"
        else ("SLACK_BOT_TOKEN", "SLACK_TEAM_ID")
    )
    secret_keys = frozenset({"GITHUB_PERSONAL_ACCESS_TOKEN", "SLACK_BOT_TOKEN"})
    blockers: list[str] = []
    for key in required_keys:
        raw_value = raw_env.get(key)
        expanded_value = expanded_env.get(key)
        if key in secret_keys and not (
            isinstance(raw_value, str)
            and _ENV_REFERENCE_RE.fullmatch(raw_value.strip())
        ):
            blockers.append(f"{key}:approved_credential_reference_required")
            continue
        if not isinstance(expanded_value, str) or not expanded_value.strip():
            blockers.append(f"{key}:value_unavailable")
        elif _ENV_REFERENCE_RE.fullmatch(expanded_value.strip()):
            blockers.append(f"{key}:environment_reference_unresolved")
    return {
        "provider": provider,
        "server_name": server_name,
        "ready": not blockers,
        "auth_mode": "environment",
        "credential_storage": "environment_reference",
        "checked_keys": list(required_keys),
        "blockers": blockers,
    }


def _canonical_repository(value: str) -> str:
    repository = str(value or "").strip().casefold()
    if not _REPOSITORY_RE.fullmatch(repository):
        raise MCPAdapterError(
            "repository must use owner/name form",
            kind="validation",
        )
    return repository


def _full_sha(value: Any) -> Optional[str]:
    sha = str(value or "").strip().casefold()
    return sha if _FULL_SHA_RE.fullmatch(sha) else None


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise MCPAdapterError(f"{field} is required", kind="validation")
    return text


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise MCPAdapterError(f"{field} must be a positive integer", kind="validation")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise MCPAdapterError(
            f"{field} must be a positive integer",
            kind="validation",
        ) from exc
    if parsed < 1:
        raise MCPAdapterError(f"{field} must be a positive integer", kind="validation")
    return parsed


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise MCPAdapterError(
            "MCP result is not JSON serializable", kind="validation"
        ) from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _receipt_marker(provider: Literal["github", "slack"], idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    if provider == "github":
        return f"<!-- hermes-review-receipt:{digest} -->"
    return f"[hermes-review-receipt:{digest}]"


_GITHUB_IDEMPOTENCY_RE = re.compile(
    r"^github-human-review:v1:(?P<repository>[^:]+/[^:]+):pr:(?P<pr_number>[1-9][0-9]*):"
    r"head:(?P<head_sha>[0-9a-f]{40}):surface:(?P<surface>[^:]+):"
    r"operation:(?P<operation>[^:]+)$"
)
_SLACK_IDEMPOTENCY_RE = re.compile(
    r"^slack-human-review:v1:channel:(?P<channel_id>[^:]+):"
    r"thread:(?P<thread_ts>[^:]+):(?P<repository>[^:]+/[^:]+):"
    r"pr:(?P<pr_number>[1-9][0-9]*):head:(?P<head_sha>[0-9a-f]{40}):"
    r"surface:(?P<surface>[^:]+):operation:(?P<operation>[^:]+)$"
)


def _idempotency_identity(
    value: str,
    *,
    provider: Literal["github", "slack"],
) -> Mapping[str, str]:
    pattern = _GITHUB_IDEMPOTENCY_RE if provider == "github" else _SLACK_IDEMPOTENCY_RE
    matched = pattern.fullmatch(str(value or "").strip())
    if matched is None:
        raise MCPAdapterError(
            f"{provider} delivery idempotency key is malformed",
            kind="validation",
        )
    return matched.groupdict()


def _parse_timestamp(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value).strip()
    if not text:
        return None
    try:
        return max(0, int(float(text)))
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, int(parsed.timestamp()))


def _classify_error_code(value: Any) -> MCPFailureKind:
    code = str(value or "").strip().casefold().replace("-", "_")
    if any(
        token in code
        for token in (
            "invalid_auth",
            "not_authed",
            "bad_credentials",
            "unauthorized",
            "401",
        )
    ):
        return "auth"
    if any(
        token in code
        for token in (
            "missing_scope",
            "forbidden",
            "permission",
            "not_in_channel",
            "403",
        )
    ):
        return "permission"
    if any(
        token in code
        for token in ("not_found", "channel_not_found", "thread_not_found", "404")
    ):
        return "not_found"
    if any(token in code for token in ("rate_limit", "ratelimit", "429")):
        return "rate_limited"
    if any(token in code for token in ("timeout", "timed_out", "deadline")):
        return "timeout"
    if any(
        token in code
        for token in ("unavailable", "not connected", "transport", "network", "server")
    ):
        return "unavailable"
    return "validation"


def _decode_json_string(value: str) -> Any:
    current: Any = value
    for _ in range(2):
        if not isinstance(current, str):
            break
        text = current.strip()
        if not text or text[0] not in '[{"':
            break
        try:
            current = json.loads(text)
        except json.JSONDecodeError:
            break
    return current


def _unwrap_tool_result(raw: Any, *, tool_name: str) -> Any:
    parsed = _decode_json_string(raw) if isinstance(raw, str) else raw
    if not isinstance(parsed, Mapping):
        raise MCPAdapterError(
            f"MCP tool {tool_name} returned an invalid envelope",
            kind="validation",
        )
    if parsed.get("error"):
        code = parsed.get("error")
        raise MCPAdapterError(
            f"MCP tool {tool_name} failed ({code})",
            kind=_classify_error_code(code),
        )
    value = parsed.get("result", parsed.get("structuredContent"))
    value = _decode_json_string(value) if isinstance(value, str) else value
    if isinstance(value, Mapping) and (value.get("ok") is False or value.get("error")):
        code = value.get("error") or "provider_error"
        raise MCPAdapterError(
            f"MCP tool {tool_name} failed ({code})",
            kind=_classify_error_code(code),
        )
    return value


@dataclass(frozen=True)
class RegistryMCPToolCaller:
    """Dispatch only an explicit read-tool allowlist through Hermes' MCP registry."""

    server_name: str
    allowed_tools: frozenset[str]
    timeout_seconds: int

    def __post_init__(self) -> None:
        if not _SERVER_NAME_RE.fullmatch(self.server_name):
            raise MCPAdapterError("MCP server name is invalid", kind="validation")
        object.__setattr__(
            self,
            "timeout_seconds",
            _positive_int(self.timeout_seconds, "provider_timeout_seconds"),
        )
        if not self.allowed_tools:
            raise MCPAdapterError("MCP read-tool allowlist is empty", kind="validation")

    def call(self, tool_name: str, arguments: Mapping[str, Any]) -> Any:
        if tool_name not in self.allowed_tools:
            raise MCPAdapterError(
                f"MCP tool {tool_name} is outside the read-only allowlist",
                kind="permission",
            )
        from tools.registry import registry

        entry = registry.get_entry(tool_name)
        if entry is None or entry.toolset != f"mcp-{self.server_name}":
            raise MCPAdapterError(
                f"required MCP tool {tool_name} is unavailable",
                kind="unavailable",
            )

        result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        def dispatch() -> None:
            try:
                result_queue.put((True, registry.dispatch(tool_name, dict(arguments))))
            except BaseException as exc:  # defensive around the registry boundary
                result_queue.put((False, exc))

        worker = threading.Thread(
            target=dispatch,
            name=f"review-runner-{self.server_name}-mcp-read",
            daemon=True,
        )
        worker.start()
        try:
            succeeded, value = result_queue.get(timeout=self.timeout_seconds)
        except queue.Empty as exc:
            raise MCPAdapterError(
                f"MCP tool {tool_name} timed out",
                kind="timeout",
            ) from exc
        if not succeeded:
            raise MCPAdapterError(
                f"MCP tool {tool_name} failed",
                kind="unavailable",
            ) from cast(BaseException, value)
        return _unwrap_tool_result(value, tool_name=tool_name)


def _tool_name(server_name: str, provider_tool_name: str) -> str:
    normalized_server = re.sub(r"[^A-Za-z0-9_]", "_", server_name)
    return f"mcp__{normalized_server}__{provider_tool_name}"


def _as_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MCPAdapterError(f"{field} must be an object", kind="validation")
    return value


def _as_sequence(value: Any, field: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise MCPAdapterError(f"{field} must be an array", kind="validation")
    return value


def _github_failure(error: MCPAdapterError) -> github.GitHubTransportFailure:
    kind_map: dict[MCPFailureKind, github.FailureKind] = {
        "auth": "auth",
        "permission": "permission",
        "not_found": "not_found",
        "rate_limited": "rate_limited",
        "timeout": "timeout",
        "unavailable": "unavailable",
        "validation": "validation",
    }
    return github.GitHubTransportFailure(str(error), kind=kind_map[error.kind])


def _slack_failure(error: MCPAdapterError) -> slack.SlackTransportFailure:
    kind_map: dict[MCPFailureKind, slack.FailureKind] = {
        "auth": "auth",
        "permission": "permission",
        "not_found": "thread_not_found",
        "rate_limited": "rate_limited",
        "timeout": "timeout",
        "unavailable": "unavailable",
        "validation": "validation",
    }
    return slack.SlackTransportFailure(str(error), kind=kind_map[error.kind])


@dataclass(frozen=True)
class _GitHubCollection:
    snapshot: github.GitHubPullRequestSnapshot
    status: Mapping[str, Any]
    reviews: tuple[Mapping[str, Any], ...]
    comments: tuple[Mapping[str, Any], ...]


class GitHubMCPReadAdapter:
    """Normalize exact-head GitHub and CodeRabbit evidence from read-only MCP calls."""

    def __init__(
        self,
        caller: MCPToolCaller,
        *,
        server_name: str,
        repositories: Sequence[str],
        coderabbit_logins: Sequence[str] = ("coderabbitai[bot]", "coderabbitai"),
        clock: Callable[[], float] = time.time,
    ) -> None:
        allowed = frozenset(_canonical_repository(item) for item in repositories)
        if not allowed:
            raise MCPAdapterError(
                "GitHub repository allowlist is empty", kind="permission"
            )
        logins = frozenset(
            str(item or "").strip().casefold() for item in coderabbit_logins
        )
        if not logins or "" in logins:
            raise MCPAdapterError(
                "CodeRabbit login allowlist is invalid", kind="validation"
            )
        self._caller = caller
        self._server_name = server_name
        self._repositories = allowed
        self._coderabbit_logins = logins
        self._clock = clock
        self._pending: dict[tuple[str, int, str], _GitHubCollection] = {}

    def _call(self, tool: str, arguments: Mapping[str, Any]) -> Any:
        return self._caller.call(_tool_name(self._server_name, tool), arguments)

    def _collect(self, repository: str, pr_number: int) -> _GitHubCollection:
        canonical = _canonical_repository(repository)
        if canonical not in self._repositories:
            raise github.GitHubTransportFailure(
                f"repository {canonical} is outside the configured allowlist",
                kind="permission",
            )
        number = _positive_int(pr_number, "pr_number")
        owner, repo = canonical.split("/", 1)
        arguments = {"owner": owner, "repo": repo, "pull_number": number}
        try:
            raw_pr = _as_mapping(
                self._call("get_pull_request", arguments), "pull request"
            )
            raw_status = _as_mapping(
                self._call("get_pull_request_status", arguments),
                "pull request status",
            )
            raw_reviews = tuple(
                _as_mapping(item, "review")
                for item in _as_sequence(
                    self._call("get_pull_request_reviews", arguments),
                    "pull request reviews",
                )
            )
            raw_comments = tuple(
                _as_mapping(item, "review comment")
                for item in _as_sequence(
                    self._call("get_pull_request_comments", arguments),
                    "pull request comments",
                )
            )
            observed_at = int(self._clock())
            snapshot = self._normalize_snapshot(
                canonical,
                number,
                raw_pr,
                raw_status,
                raw_reviews,
                raw_comments,
                observed_at=observed_at,
            )
        except MCPAdapterError as exc:
            raise _github_failure(exc) from exc
        return _GitHubCollection(snapshot, raw_status, raw_reviews, raw_comments)

    def read_snapshot(
        self,
        *,
        repository: str,
        pr_number: int,
    ) -> github.GitHubPullRequestSnapshot:
        collection = self._collect(repository, pr_number)
        key = (
            collection.snapshot.repository,
            collection.snapshot.pr_number,
            collection.snapshot.head_sha,
        )
        self._pending[key] = collection
        return collection.snapshot

    def read_review(
        self,
        *,
        repository: str,
        pr_number: int,
        expected_head_sha: str,
    ) -> coderabbit.CodeRabbitSnapshot:
        canonical = _canonical_repository(repository)
        number = _positive_int(pr_number, "pr_number")
        expected = _full_sha(expected_head_sha)
        if expected is None:
            raise coderabbit.CodeRabbitBoundaryError(
                "expected_head_sha must be a full 40-character lowercase SHA"
            )
        collection = self._pending.pop((canonical, number, expected), None)
        if collection is None:
            collection = self._collect(canonical, number)
        if collection.snapshot.head_sha != expected:
            raise coderabbit.CodeRabbitBoundaryError(
                "GitHub MCP readback does not match the expected exact head SHA"
            )
        return self._normalize_coderabbit(collection, expected_head_sha=expected)

    def _normalize_snapshot(
        self,
        repository: str,
        pr_number: int,
        raw_pr: Mapping[str, Any],
        raw_status: Mapping[str, Any],
        raw_reviews: Sequence[Mapping[str, Any]],
        raw_comments: Sequence[Mapping[str, Any]],
        *,
        observed_at: int,
    ) -> github.GitHubPullRequestSnapshot:
        if int(raw_pr.get("number") or 0) != pr_number:
            raise MCPAdapterError(
                "GitHub returned a different PR number", kind="validation"
            )
        head = _as_mapping(raw_pr.get("head"), "pull request head")
        base = _as_mapping(raw_pr.get("base"), "pull request base")
        head_sha = _full_sha(head.get("sha"))
        if head_sha is None:
            raise MCPAdapterError("GitHub PR head SHA is malformed", kind="validation")
        status_sha = _full_sha(raw_status.get("sha"))
        if status_sha is not None and status_sha != head_sha:
            raise MCPAdapterError(
                "GitHub status readback is for a different head SHA",
                kind="validation",
            )
        state_text = str(raw_pr.get("state") or "").strip().casefold()
        state: github.PullRequestState
        if raw_pr.get("merged_at") is not None or raw_pr.get("merged") is True:
            state = "merged"
        elif state_text in {"open", "closed"}:
            state = cast(github.PullRequestState, state_text)
        else:
            raise MCPAdapterError("GitHub PR state is unsupported", kind="validation")

        checks = self._normalize_checks(raw_status, head_sha=head_sha)
        reviews = self._normalize_reviews(raw_reviews)
        threads = self._normalize_review_threads(
            raw_comments, current_head_sha=head_sha
        )
        requested: list[github.GitHubRequestedReviewer] = []
        for item in raw_pr.get("requested_reviewers") or ():
            if isinstance(item, Mapping) and item.get("login"):
                requested.append(
                    github.GitHubRequestedReviewer(
                        principal=str(item["login"]),
                        kind="user",
                    )
                )
        for item in raw_pr.get("requested_teams") or ():
            if isinstance(item, Mapping) and (item.get("slug") or item.get("name")):
                requested.append(
                    github.GitHubRequestedReviewer(
                        principal=str(item.get("slug") or item.get("name")),
                        kind="team",
                    )
                )
        identity = {
            "repository": repository,
            "pr_number": pr_number,
            "head_sha": head_sha,
            "state": state,
            "checks": [item.normalized_dict() for item in checks],
            "reviews": [item.normalized_dict() for item in reviews],
            "threads": [item.normalized_dict() for item in threads],
            "requested_reviewers": [item.normalized_dict() for item in requested],
        }
        return github.GitHubPullRequestSnapshot(
            provider="GitHub-MCP",
            observation_id=f"github-mcp:{repository}:{pr_number}:{_digest(identity)[:20]}",
            repository=repository,
            pr_number=pr_number,
            pr_url=_required_text(raw_pr.get("html_url"), "pull request html_url"),
            state=state,
            is_draft=bool(raw_pr.get("draft") or raw_pr.get("is_draft")),
            base_ref=_required_text(base.get("ref"), "pull request base ref"),
            head_ref=_required_text(head.get("ref"), "pull request head ref"),
            head_sha=head_sha,
            observed_at=observed_at,
            checks=checks,
            reviews=reviews,
            review_threads=threads,
            requested_reviewers=tuple(requested),
        )

    @staticmethod
    def _normalize_checks(
        raw_status: Mapping[str, Any],
        *,
        head_sha: str,
    ) -> tuple[github.GitHubCheck, ...]:
        checks: list[github.GitHubCheck] = []
        for index, item in enumerate(raw_status.get("statuses") or ()):
            if not isinstance(item, Mapping):
                continue
            item_sha = _full_sha(item.get("sha")) or head_sha
            state = str(item.get("state") or item.get("status") or "").casefold()
            if state in {"pending", "queued"}:
                status: github.CheckStatus = "queued"
                conclusion = None
            elif state in {"in_progress", "in-progress"}:
                status = "in_progress"
                conclusion = None
            elif state == "success":
                status = "completed"
                conclusion = "success"
            elif state in {"failure", "error"}:
                status = "completed"
                conclusion = "failure"
            else:
                status = "completed"
                conclusion = "neutral"
            checks.append(
                github.GitHubCheck(
                    check_id=str(item.get("id") or f"status-{index}"),
                    name=str(
                        item.get("context") or item.get("name") or f"status-{index}"
                    ),
                    head_sha=item_sha,
                    status=status,
                    conclusion=cast(Optional[github.CheckConclusion], conclusion),
                )
            )
        if not checks:
            combined_state = str(raw_status.get("state") or "pending").casefold()
            if combined_state == "success":
                status = "completed"
                conclusion = "success"
            elif combined_state in {"failure", "error"}:
                status = "completed"
                conclusion = "failure"
            else:
                status = "queued"
                conclusion = None
            checks.append(
                github.GitHubCheck(
                    check_id="combined-status",
                    name="GitHub combined status",
                    head_sha=head_sha,
                    status=cast(github.CheckStatus, status),
                    conclusion=cast(Optional[github.CheckConclusion], conclusion),
                )
            )
        return tuple(checks)

    @staticmethod
    def _normalize_reviews(
        raw_reviews: Sequence[Mapping[str, Any]],
    ) -> tuple[github.GitHubReview, ...]:
        normalized: list[github.GitHubReview] = []
        for item in raw_reviews:
            user = item.get("user")
            author = user.get("login") if isinstance(user, Mapping) else None
            head_sha = _full_sha(item.get("commit_id"))
            submitted_at = _parse_timestamp(item.get("submitted_at"))
            state = str(item.get("state") or "").strip().casefold()
            if author and head_sha is None:
                raise MCPAdapterError(
                    "GitHub review commit_id is unavailable; exact-head review evidence is blocked",
                    kind="validation",
                )
            if (
                not author
                or head_sha is None
                or submitted_at is None
                or state not in github.REVIEW_STATES
            ):
                continue
            normalized.append(
                github.GitHubReview(
                    review_id=str(item.get("id") or item.get("node_id") or "review"),
                    author_login=str(author),
                    head_sha=head_sha,
                    state=cast(github.ReviewState, state),
                    submitted_at=submitted_at,
                )
            )
        return tuple(normalized)

    @staticmethod
    def _normalize_review_threads(
        raw_comments: Sequence[Mapping[str, Any]],
        *,
        current_head_sha: str,
    ) -> tuple[github.GitHubReviewThread, ...]:
        grouped: dict[
            str, list[tuple[Mapping[str, Any], github.GitHubReviewComment]]
        ] = {}
        for item in raw_comments:
            user = item.get("user")
            author = user.get("login") if isinstance(user, Mapping) else None
            head_sha = _full_sha(item.get("commit_id"))
            created_at = _parse_timestamp(item.get("created_at"))
            if author and head_sha is None:
                raise MCPAdapterError(
                    "GitHub review comment commit_id is unavailable; exact-head thread evidence is blocked",
                    kind="validation",
                )
            if not author or head_sha is None or created_at is None:
                continue
            outdated = head_sha != current_head_sha or (
                item.get("position") is None and item.get("line") is None
            )
            comment = github.GitHubReviewComment(
                comment_id=str(item.get("id") or item.get("node_id") or "comment"),
                author_login=str(author),
                head_sha=head_sha,
                created_at=created_at,
                actionable=not outdated and item.get("in_reply_to_id") is None,
            )
            root = str(
                item.get("in_reply_to_id") or item.get("id") or comment.comment_id
            )
            grouped.setdefault(root, []).append((item, comment))
        threads: list[github.GitHubReviewThread] = []
        for root, values in grouped.items():
            comments = tuple(item[1] for item in values)
            outdated = all(
                comment.head_sha != current_head_sha
                or (raw.get("position") is None and raw.get("line") is None)
                for raw, comment in values
            )
            threads.append(
                github.GitHubReviewThread(
                    thread_id=f"review-comment:{root}",
                    head_sha=comments[0].head_sha,
                    resolved=False,
                    outdated=outdated,
                    actionable=any(comment.actionable for comment in comments),
                    comments=comments,
                )
            )
        return tuple(threads)

    def _normalize_coderabbit(
        self,
        collection: _GitHubCollection,
        *,
        expected_head_sha: str,
    ) -> coderabbit.CodeRabbitSnapshot:
        exact_reviews = [
            item
            for item in collection.reviews
            if self._actor_login(item) in self._coderabbit_logins
            and _full_sha(item.get("commit_id")) == expected_head_sha
        ]
        all_comments = [
            item
            for item in collection.comments
            if self._actor_login(item) in self._coderabbit_logins
            and _full_sha(item.get("commit_id")) is not None
        ]
        comments: list[coderabbit.CodeRabbitComment] = []
        for item in all_comments:
            comment_sha = cast(str, _full_sha(item.get("commit_id")))
            outdated = comment_sha != expected_head_sha or (
                item.get("position") is None and item.get("line") is None
            )
            root = str(item.get("in_reply_to_id") or item.get("id") or "comment")
            comments.append(
                coderabbit.CodeRabbitComment(
                    comment_id=str(item.get("id") or item.get("node_id") or root),
                    thread_id=f"review-comment:{root}",
                    head_sha=comment_sha,
                    state="outdated" if outdated else "open",
                    actionable=not outdated and item.get("in_reply_to_id") is None,
                )
            )

        status, status_text = self._coderabbit_check_state(
            collection.status,
            expected_head_sha=expected_head_sha,
        )
        review_texts = [str(item.get("body") or "") for item in exact_reviews]
        combined_text = "\n".join([status_text, *review_texts]).strip()
        summary = self._summary_from_text(combined_text)
        actionable_count = sum(
            item.state == "open"
            and item.actionable
            and item.head_sha == expected_head_sha
            for item in comments
        )
        if actionable_count:
            status = "success"
            summary = coderabbit.CodeRabbitReviewSummary(
                "actionable",
                actionable_count=actionable_count,
            )
        elif summary is not None:
            status = cast(coderabbit.CheckStatus, summary.state)
            if summary.state in {"clean", "no_actionable_comments", "actionable"}:
                status = "success"
        elif exact_reviews or any(
            item.head_sha == expected_head_sha for item in comments
        ):
            status = "success"

        review_ids = [
            int(item["id"])
            for item in exact_reviews
            if str(item.get("id") or "").isdigit()
        ]
        review_ids.extend(
            int(item["pull_request_review_id"])
            for item in all_comments
            if _full_sha(item.get("commit_id")) == expected_head_sha
            and str(item.get("pull_request_review_id") or "").isdigit()
        )
        generation = max(review_ids, default=0)
        semantic = {
            "repository": collection.snapshot.repository,
            "pr_number": collection.snapshot.pr_number,
            "head_sha": expected_head_sha,
            "generation": generation,
            "status": status,
            "summary": summary.normalized_dict() if summary else None,
            "comments": [item.normalized_dict() for item in comments],
        }
        observation_identity = {
            **semantic,
            "observed_at": collection.snapshot.observed_at,
        }
        return coderabbit.CodeRabbitSnapshot(
            provider="CodeRabbit-via-GitHub-MCP",
            observation_id=(
                f"coderabbit-mcp:{collection.snapshot.repository}:"
                f"{collection.snapshot.pr_number}:{_digest(observation_identity)[:20]}"
            ),
            repository=collection.snapshot.repository,
            pr_number=collection.snapshot.pr_number,
            head_sha=expected_head_sha,
            review_generation=generation,
            observed_at=collection.snapshot.observed_at,
            check_status=status,
            summary=summary,
            comments=tuple(comments),
        )

    @staticmethod
    def _actor_login(item: Mapping[str, Any]) -> str:
        user = item.get("user")
        return (
            str(user.get("login") or "").casefold() if isinstance(user, Mapping) else ""
        )

    @staticmethod
    def _coderabbit_check_state(
        raw_status: Mapping[str, Any],
        *,
        expected_head_sha: str,
    ) -> tuple[coderabbit.CheckStatus, str]:
        matches: list[Mapping[str, Any]] = []
        for item in raw_status.get("statuses") or ():
            if not isinstance(item, Mapping):
                continue
            context = str(item.get("context") or item.get("name") or "").casefold()
            status_sha = _full_sha(item.get("sha")) or _full_sha(raw_status.get("sha"))
            if "coderabbit" in context and status_sha == expected_head_sha:
                matches.append(item)
        if not matches:
            return "pending", ""
        item = matches[-1]
        text = " ".join(
            str(item.get(key) or "")
            for key in ("context", "description", "state", "status")
        )
        if _RATE_LIMIT_RE.search(text):
            return "rate_limited", text
        if _SKIPPED_RE.search(text):
            return "skipped", text
        if _PAUSED_RE.search(text):
            return "paused", text
        state = str(item.get("state") or item.get("status") or "").casefold()
        if state == "success":
            return "success", text
        if state in {"pending", "queued", "in_progress"}:
            return "pending", text
        return "unavailable", text

    @staticmethod
    def _summary_from_text(text: str) -> Optional[coderabbit.CodeRabbitReviewSummary]:
        if not text:
            return None
        if _RATE_LIMIT_RE.search(text):
            return coderabbit.CodeRabbitReviewSummary("rate_limited")
        if _SKIPPED_RE.search(text):
            return coderabbit.CodeRabbitReviewSummary("skipped")
        if _PAUSED_RE.search(text):
            return coderabbit.CodeRabbitReviewSummary("paused")
        actionable = _ACTIONABLE_COUNT_RE.search(text)
        if actionable:
            count = int(actionable.group(1))
            return coderabbit.CodeRabbitReviewSummary(
                "actionable" if count else "no_actionable_comments",
                actionable_count=count,
            )
        if _NO_ACTIONABLE_RE.search(text):
            return coderabbit.CodeRabbitReviewSummary("no_actionable_comments")
        if _CLEAN_RE.search(text):
            return coderabbit.CodeRabbitReviewSummary("clean")
        if _PENDING_RE.search(text):
            return coderabbit.CodeRabbitReviewSummary("pending")
        return None


class GitHubMCPDeliveryTransport:
    """Restricted exact-head review-comment transport with receipt readback."""

    def __init__(
        self,
        caller: MCPToolCaller,
        *,
        server_name: str,
        repositories: Sequence[str],
    ) -> None:
        self._caller = caller
        self._server_name = server_name
        self._repositories = frozenset(
            _canonical_repository(item) for item in repositories
        )
        if not self._repositories:
            raise MCPAdapterError(
                "GitHub delivery repository allowlist is empty",
                kind="permission",
            )

    def _call(self, tool: str, arguments: Mapping[str, Any]) -> Any:
        return self._caller.call(_tool_name(self._server_name, tool), arguments)

    def _identity(self, idempotency_key: str) -> Mapping[str, str]:
        try:
            identity = _idempotency_identity(idempotency_key, provider="github")
            repository = _canonical_repository(identity["repository"])
        except MCPAdapterError as exc:
            raise _github_failure(exc) from exc
        if repository not in self._repositories:
            raise github.GitHubTransportFailure(
                f"repository {repository} is outside the configured delivery allowlist",
                kind="permission",
            )
        return identity

    @staticmethod
    def _receipt(
        review: Mapping[str, Any],
        *,
        idempotency_key: str,
        expected_head_sha: str,
    ) -> github.GitHubDeliveryReceipt:
        commit_id = _full_sha(review.get("commit_id"))
        if commit_id != expected_head_sha:
            raise github.GitHubTransportFailure(
                "GitHub delivery receipt is not bound to the exact head SHA",
                kind="conflict",
            )
        external_id = (
            review.get("id") or review.get("node_id") or review.get("html_url")
        )
        if not external_id:
            raise github.GitHubTransportFailure(
                "GitHub delivery response omitted a provider receipt ID",
                kind="unavailable",
            )
        return github.GitHubDeliveryReceipt(
            external_id=f"github-review:{external_id}",
            idempotency_key=idempotency_key,
        )

    def find_delivery(
        self,
        *,
        idempotency_key: str,
    ) -> Optional[github.GitHubDeliveryReceipt]:
        identity = self._identity(idempotency_key)
        owner, repo = identity["repository"].split("/", 1)
        arguments = {
            "owner": owner,
            "repo": repo,
            "pull_number": int(identity["pr_number"]),
        }
        marker = _receipt_marker("github", idempotency_key)
        try:
            reviews = _as_sequence(
                self._call("get_pull_request_reviews", arguments),
                "pull request reviews",
            )
            for item in reviews:
                review = _as_mapping(item, "pull request review")
                if marker not in str(review.get("body") or ""):
                    continue
                return self._receipt(
                    review,
                    idempotency_key=idempotency_key,
                    expected_head_sha=identity["head_sha"],
                )
        except MCPAdapterError as exc:
            raise _github_failure(exc) from exc
        return None

    def send_intent(
        self,
        intent: github.GitHubOutboxIntent,
    ) -> github.GitHubDeliveryReceipt:
        identity = self._identity(intent.idempotency_key)
        if (
            identity["repository"] != intent.repository
            or int(identity["pr_number"]) != intent.pr_number
            or identity["head_sha"] != intent.head_sha
            or identity["surface"] != intent.surface
            or identity["operation"] != intent.operation
        ):
            raise github.GitHubTransportFailure(
                "GitHub intent fields do not match its idempotency identity",
                kind="conflict",
            )
        if intent.operation == "request_reviewer":
            raise github.GitHubTransportFailure(
                "the configured GitHub MCP has no reviewer-request write tool",
                kind="validation",
            )
        try:
            body = _required_text(intent.payload.get("body"), "GitHub intent body")
        except MCPAdapterError as exc:
            raise _github_failure(exc) from exc
        marker = _receipt_marker("github", intent.idempotency_key)
        owner, repo = intent.repository.split("/", 1)
        try:
            review = _as_mapping(
                self._call(
                    "create_pull_request_review",
                    {
                        "owner": owner,
                        "repo": repo,
                        "pull_number": intent.pr_number,
                        "body": f"{body.rstrip()}\n\n{marker}",
                        "event": "COMMENT",
                        "commit_id": intent.head_sha,
                    },
                ),
                "pull request review delivery",
            )
            return self._receipt(
                review,
                idempotency_key=intent.idempotency_key,
                expected_head_sha=intent.head_sha,
            )
        except MCPAdapterError as exc:
            if exc.kind == "validation":
                exc = MCPAdapterError(
                    "GitHub delivery response is ambiguous; retry through receipt readback",
                    kind="unavailable",
                )
            raise _github_failure(exc) from exc


class SlackMCPDeliveryTransport:
    """Restricted exact-route Slack sender with deterministic receipt readback."""

    def __init__(
        self,
        caller: MCPToolCaller,
        *,
        server_name: str,
        channel_ids: Sequence[str],
    ) -> None:
        self._caller = caller
        self._server_name = server_name
        self._channels = frozenset(str(item or "").strip() for item in channel_ids)
        if not self._channels or "" in self._channels:
            raise MCPAdapterError(
                "Slack delivery channel allowlist is empty or invalid",
                kind="permission",
            )

    def _call(self, tool: str, arguments: Mapping[str, Any]) -> Any:
        return self._caller.call(_tool_name(self._server_name, tool), arguments)

    def _identity(self, idempotency_key: str) -> Mapping[str, str]:
        try:
            identity = _idempotency_identity(idempotency_key, provider="slack")
        except MCPAdapterError as exc:
            raise _slack_failure(exc) from exc
        if identity["channel_id"] not in self._channels:
            raise slack.SlackTransportFailure(
                f"Slack channel {identity['channel_id']} is outside the delivery allowlist",
                kind="permission",
            )
        return identity

    @staticmethod
    def _receipt(
        response: Mapping[str, Any],
        *,
        idempotency_key: str,
        channel_id: str,
        thread_ts: str,
    ) -> slack.SlackDeliveryReceipt:
        message = response.get("message")
        nested = message if isinstance(message, Mapping) else {}
        response_channel = str(response.get("channel") or nested.get("channel") or "")
        if response_channel and response_channel != channel_id:
            raise slack.SlackTransportFailure(
                "Slack delivery response changed the stored channel route",
                kind="conflict",
            )
        message_ts = str(
            response.get("ts") or nested.get("ts") or response.get("message_ts") or ""
        ).strip()
        if not message_ts:
            raise slack.SlackTransportFailure(
                "Slack delivery response omitted message_ts",
                kind="unavailable",
            )
        delivered_thread = thread_ts or message_ts
        response_thread = str(
            response.get("thread_ts") or nested.get("thread_ts") or delivered_thread
        ).strip()
        if response_thread != delivered_thread:
            raise slack.SlackTransportFailure(
                "Slack delivery response changed the stored thread route",
                kind="conflict",
            )
        return slack.SlackDeliveryReceipt(
            external_id=f"slack:{channel_id}:{message_ts}",
            message_ts=message_ts,
            thread_ts=delivered_thread,
            idempotency_key=idempotency_key,
        )

    def find_delivery(
        self,
        *,
        idempotency_key: str,
    ) -> Optional[slack.SlackDeliveryReceipt]:
        identity = self._identity(idempotency_key)
        channel = identity["channel_id"]
        thread = identity["thread_ts"]
        marker = _receipt_marker("slack", idempotency_key)
        try:
            if thread == "root":
                raw = self._call(
                    "slack_get_channel_history",
                    {"channel_id": channel, "limit": 100},
                )
                delivered_thread = ""
            else:
                raw = self._call(
                    "slack_get_thread_replies",
                    {"channel_id": channel, "thread_ts": thread},
                )
                delivered_thread = thread
            messages = SlackMCPAcknowledgementProvider._extract_messages(raw)
            for message in messages:
                if marker not in str(message.get("text") or ""):
                    continue
                return self._receipt(
                    message,
                    idempotency_key=idempotency_key,
                    channel_id=channel,
                    thread_ts=delivered_thread,
                )
        except MCPAdapterError as exc:
            raise _slack_failure(exc) from exc
        return None

    def send_intent(
        self,
        intent: slack.SlackOutboxIntent,
    ) -> slack.SlackDeliveryReceipt:
        identity = self._identity(intent.idempotency_key)
        expected_thread = intent.thread_ts or "root"
        if (
            identity["channel_id"] != intent.channel_id
            or identity["thread_ts"] != expected_thread
            or identity["repository"] != intent.repository
            or int(identity["pr_number"]) != intent.pr_number
            or identity["head_sha"] != intent.head_sha
            or identity["surface"] != intent.surface
            or identity["operation"] != intent.operation
        ):
            raise slack.SlackTransportFailure(
                "Slack intent fields do not match its idempotency identity",
                kind="conflict",
            )
        try:
            body = _required_text(intent.payload.get("body"), "Slack intent body")
        except MCPAdapterError as exc:
            raise _slack_failure(exc) from exc
        text = f"{body.rstrip()}\n\n{_receipt_marker('slack', intent.idempotency_key)}"
        if intent.surface == "channel":
            tool = "slack_post_message"
            arguments = {"channel_id": intent.channel_id, "text": text}
        else:
            tool = "slack_reply_to_thread"
            arguments = {
                "channel_id": intent.channel_id,
                "thread_ts": intent.thread_ts,
                "text": text,
            }
        try:
            response = _as_mapping(self._call(tool, arguments), "Slack delivery")
            return self._receipt(
                response,
                idempotency_key=intent.idempotency_key,
                channel_id=intent.channel_id,
                thread_ts=intent.thread_ts,
            )
        except MCPAdapterError as exc:
            if exc.kind == "validation":
                exc = MCPAdapterError(
                    "Slack delivery response is ambiguous; retry through receipt readback",
                    kind="unavailable",
                )
            raise _slack_failure(exc) from exc


class SlackMCPAcknowledgementProvider:
    """Read normalized acknowledgements from exact allowlisted Slack threads."""

    def __init__(
        self,
        caller: MCPToolCaller,
        *,
        server_name: str,
        channel_ids: Sequence[str],
        user_ids: Sequence[str],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._caller = caller
        self._server_name = server_name
        self._channels = frozenset(str(item or "").strip() for item in channel_ids)
        self._users = frozenset(str(item or "").strip() for item in user_ids)
        if not self._channels or "" in self._channels:
            raise MCPAdapterError(
                "Slack channel allowlist is empty or invalid", kind="permission"
            )
        if not self._users or "" in self._users:
            raise MCPAdapterError(
                "Slack acknowledgement user allowlist is empty or invalid",
                kind="permission",
            )
        self._clock = clock

    def read_acknowledgements(
        self,
        *,
        channel_id: str,
        thread_ts: str,
    ) -> tuple[slack.SlackAcknowledgementEvent, ...]:
        channel = str(channel_id or "").strip()
        thread = str(thread_ts or "").strip()
        if channel not in self._channels:
            raise slack.SlackTransportFailure(
                f"Slack channel {channel} is outside the configured allowlist",
                kind="permission",
            )
        if not thread:
            raise slack.SlackTransportFailure(
                "Slack thread_ts is required", kind="validation"
            )
        try:
            raw = self._caller.call(
                _tool_name(self._server_name, "slack_get_thread_replies"),
                {"channel_id": channel, "thread_ts": thread},
            )
            messages = self._extract_messages(raw)
        except MCPAdapterError as exc:
            raise _slack_failure(exc) from exc

        events: list[slack.SlackAcknowledgementEvent] = []
        for message in messages:
            message_ts = str(
                message.get("ts") or message.get("timestamp") or ""
            ).strip()
            user_id = str(message.get("user") or message.get("user_id") or "").strip()
            if not message_ts:
                continue
            observed_at = _parse_timestamp(message_ts) or int(self._clock())
            text = str(message.get("text") or "").strip()
            if text and message_ts != thread and user_id in self._users:
                events.append(
                    slack.SlackAcknowledgementEvent(
                        provider="Slack-MCP",
                        event_id=f"slack-message:{channel}:{message_ts}",
                        channel_id=channel,
                        thread_ts=thread,
                        message_ts=message_ts,
                        user_id=user_id,
                        source="text",
                        value=text,
                        observed_at=observed_at,
                    )
                )
            for reaction in message.get("reactions") or ():
                if not isinstance(reaction, Mapping):
                    continue
                name = str(reaction.get("name") or "").strip()
                users = reaction.get("users") or ()
                if not name:
                    continue
                for reacting_user in sorted({
                    str(item) for item in users if str(item) in self._users
                }):
                    events.append(
                        slack.SlackAcknowledgementEvent(
                            provider="Slack-MCP",
                            event_id=(
                                f"slack-reaction:{channel}:{message_ts}:"
                                f"{name}:{reacting_user}"
                            ),
                            channel_id=channel,
                            thread_ts=thread,
                            message_ts=message_ts,
                            user_id=reacting_user,
                            source="reaction",
                            value=name,
                            observed_at=observed_at,
                        )
                    )
        return tuple(sorted(events, key=lambda item: (item.observed_at, item.event_id)))

    @staticmethod
    def _extract_messages(raw: Any) -> tuple[Mapping[str, Any], ...]:
        value = _decode_json_string(raw) if isinstance(raw, str) else raw
        if isinstance(value, Mapping):
            if value.get("ok") is False or value.get("error"):
                code = value.get("error") or "provider_error"
                raise MCPAdapterError(
                    f"Slack MCP read failed ({code})",
                    kind=_classify_error_code(code),
                )
            value = value.get("messages", value.get("replies"))
        if not isinstance(value, Sequence) or isinstance(
            value, (str, bytes, bytearray)
        ):
            raise MCPAdapterError(
                "Slack thread replies were not returned as structured messages",
                kind="validation",
            )
        messages = []
        for item in value:
            if not isinstance(item, Mapping):
                raise MCPAdapterError(
                    "Slack thread reply is not an object",
                    kind="validation",
                )
            messages.append(item)
        return tuple(messages)


@dataclass(frozen=True)
class ReviewRunnerMCPBundle:
    provider_timeout_seconds: int
    github_adapter: Optional[GitHubMCPReadAdapter] = None
    github_delivery_transport: Optional[GitHubMCPDeliveryTransport] = None
    slack_delivery_transport: Optional[SlackMCPDeliveryTransport] = None
    slack_acknowledgement_provider: Optional[SlackMCPAcknowledgementProvider] = None
    credential_preflight: Optional[Mapping[str, Mapping[str, Any]]] = None


def build_review_runner_mcp_bundle(
    *,
    provider_timeout_seconds: int,
    github_server_name: Optional[str] = None,
    github_repositories: Sequence[str] = (),
    coderabbit_logins: Sequence[str] = ("coderabbitai[bot]", "coderabbitai"),
    github_delivery_enabled: bool = False,
    slack_server_name: Optional[str] = None,
    slack_channel_ids: Sequence[str] = (),
    slack_user_ids: Sequence[str] = (),
    slack_delivery_enabled: bool = False,
    mcp_servers: Optional[Mapping[str, Any]] = None,
    raw_mcp_servers: Optional[Mapping[str, Any]] = None,
    clock: Callable[[], float] = time.time,
) -> ReviewRunnerMCPBundle:
    """Connect only explicitly selected MCP servers and return read-only adapters."""

    timeout = _positive_int(provider_timeout_seconds, "provider_timeout_seconds")
    required_tools_by_server: dict[str, set[str]] = {}
    if github_server_name:
        required_tools_by_server.setdefault(github_server_name, set()).update(
            _GITHUB_READ_TOOLS
        )
        if github_delivery_enabled:
            required_tools_by_server[github_server_name].update(_GITHUB_DELIVERY_TOOLS)
    if slack_server_name:
        required_tools_by_server.setdefault(slack_server_name, set()).update(
            _SLACK_READ_TOOLS
        )
        if slack_delivery_enabled:
            required_tools_by_server[slack_server_name].update(_SLACK_DELIVERY_TOOLS)
    if github_delivery_enabled and not github_server_name:
        raise MCPAdapterError(
            "GitHub MCP delivery requires the GitHub provider adapter",
            kind="permission",
        )
    if slack_delivery_enabled and not slack_server_name:
        raise MCPAdapterError(
            "Slack MCP delivery requires the Slack provider adapter",
            kind="permission",
        )
    selected_names = tuple(required_tools_by_server)
    if not selected_names:
        return ReviewRunnerMCPBundle(timeout)
    for name in selected_names:
        if not _SERVER_NAME_RE.fullmatch(str(name)):
            raise MCPAdapterError("MCP server name is invalid", kind="validation")
    if github_server_name and not tuple(github_repositories):
        raise MCPAdapterError(
            "GitHub repository allowlist is required before MCP registration",
            kind="permission",
        )
    if slack_server_name and not tuple(slack_channel_ids):
        raise MCPAdapterError(
            "Slack channel allowlist is required before MCP registration",
            kind="permission",
        )
    if slack_server_name and not tuple(slack_user_ids):
        raise MCPAdapterError(
            "Slack acknowledgement user allowlist is required before MCP registration",
            kind="permission",
        )

    if mcp_servers is None:
        from hermes_cli.config import load_config

        loaded = load_config() or {}
        configured = loaded.get("mcp_servers")
        mcp_servers = configured if isinstance(configured, Mapping) else {}
        if raw_mcp_servers is None:
            raw_mcp_servers = _load_raw_mcp_servers()
    elif raw_mcp_servers is None:
        raw_mcp_servers = mcp_servers

    selected: dict[str, dict[str, Any]] = {}
    credential_preflight: dict[str, Mapping[str, Any]] = {}
    for name in selected_names:
        raw = mcp_servers.get(name) if isinstance(mcp_servers, Mapping) else None
        if not isinstance(raw, Mapping):
            raise MCPAdapterError(
                f"MCP server {name} is not configured",
                kind="unavailable",
            )
        server_config = dict(raw)
        raw_unexpanded = (
            raw_mcp_servers.get(name) if isinstance(raw_mcp_servers, Mapping) else None
        )
        if not isinstance(raw_unexpanded, Mapping):
            raise MCPAdapterError(
                f"MCP server {name} credential source cannot be verified",
                kind="auth",
            )
        providers: list[Literal["github", "slack"]] = []
        if name == github_server_name:
            providers.append("github")
        if name == slack_server_name:
            providers.append("slack")
        for provider in providers:
            credential_report = _credential_preflight(
                provider=provider,
                server_name=name,
                raw_server=raw_unexpanded,
                expanded_server=server_config,
            )
            credential_preflight[provider] = credential_report
            if not credential_report["ready"]:
                raise MCPAdapterError(
                    f"MCP server {name} credential readiness is blocked",
                    kind="auth",
                )
        # Explicit registration must not widen the runner process to the MCP
        # server's write tools. Registry dispatch is independently allowlisted,
        # but restricting discovery keeps the global tool registry read-only too.
        server_config["tools"] = {
            "include": sorted(required_tools_by_server[name]),
            "prompts": False,
            "resources": False,
        }
        server_config["timeout"] = timeout
        server_config["connect_timeout"] = min(
            timeout,
            _positive_int(
                server_config.get("connect_timeout", timeout),
                f"mcp_servers.{name}.connect_timeout",
            ),
        )
        selected[name] = server_config

    from tools.mcp_tool import register_mcp_servers

    registered_names = frozenset(register_mcp_servers(selected))
    expected_names = frozenset(
        _tool_name(server_name, tool)
        for server_name, tools in required_tools_by_server.items()
        for tool in tools
    )
    missing_names = sorted(expected_names - registered_names)
    if missing_names:
        raise MCPAdapterError(
            "required MCP read tools were not discovered for the review runner",
            kind="unavailable",
        )
    selected_prefixes = tuple(_tool_name(name, "") for name in selected_names)
    unexpected_names = sorted(
        name
        for name in registered_names - expected_names
        if name.startswith(selected_prefixes)
    )
    if unexpected_names:
        # ``register_mcp_servers`` is idempotent by server name. If another
        # startup path already registered a selected server with a wider tool
        # surface, the narrower config above cannot retroactively remove it.
        # Refuse the adapter rather than coexisting with provider write tools.
        raise MCPAdapterError(
            "selected MCP servers exposed non-allowlisted tools to the review runner",
            kind="permission",
        )

    github_adapter: Optional[GitHubMCPReadAdapter] = None
    github_delivery_transport: Optional[GitHubMCPDeliveryTransport] = None
    if github_server_name:
        allowed = frozenset(
            _tool_name(github_server_name, tool) for tool in _GITHUB_READ_TOOLS
        )
        github_caller = RegistryMCPToolCaller(
            github_server_name,
            allowed,
            timeout,
        )
        github_adapter = GitHubMCPReadAdapter(
            github_caller,
            server_name=github_server_name,
            repositories=github_repositories,
            coderabbit_logins=coderabbit_logins,
            clock=clock,
        )
        if github_delivery_enabled:
            delivery_allowed = frozenset(
                _tool_name(github_server_name, tool)
                for tool in (_GITHUB_DELIVERY_TOOLS | {"get_pull_request_reviews"})
            )
            github_delivery_transport = GitHubMCPDeliveryTransport(
                RegistryMCPToolCaller(
                    github_server_name,
                    delivery_allowed,
                    timeout,
                ),
                server_name=github_server_name,
                repositories=github_repositories,
            )

    slack_provider: Optional[SlackMCPAcknowledgementProvider] = None
    slack_delivery_transport: Optional[SlackMCPDeliveryTransport] = None
    if slack_server_name:
        allowed = frozenset(
            _tool_name(slack_server_name, tool) for tool in _SLACK_READ_TOOLS
        )
        slack_caller = RegistryMCPToolCaller(
            slack_server_name,
            allowed,
            timeout,
        )
        slack_provider = SlackMCPAcknowledgementProvider(
            slack_caller,
            server_name=slack_server_name,
            channel_ids=slack_channel_ids,
            user_ids=slack_user_ids,
            clock=clock,
        )
        if slack_delivery_enabled:
            delivery_allowed = frozenset(
                _tool_name(slack_server_name, tool) for tool in _SLACK_DELIVERY_TOOLS
            )
            slack_delivery_transport = SlackMCPDeliveryTransport(
                RegistryMCPToolCaller(
                    slack_server_name,
                    delivery_allowed,
                    timeout,
                ),
                server_name=slack_server_name,
                channel_ids=slack_channel_ids,
            )

    return ReviewRunnerMCPBundle(
        provider_timeout_seconds=timeout,
        github_adapter=github_adapter,
        github_delivery_transport=github_delivery_transport,
        slack_delivery_transport=slack_delivery_transport,
        slack_acknowledgement_provider=slack_provider,
        credential_preflight=credential_preflight,
    )
