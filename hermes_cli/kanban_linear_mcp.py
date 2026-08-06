"""Fail-closed read-only Linear OAuth MCP adapter for Kanban coordination.

This module bridges Linear's structured MCP reads into ``LinearIssueSnapshot``.
It never dispatches Linear writes, never infers PR state/head from prose, never
registers a webhook, and never treats OAuth connectivity as event delivery.
GitHub remains the only authority for exact PR heads and repository state.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Mapping, Optional, Sequence
from urllib.parse import unquote, urlparse

from hermes_cli import kanban_linear as linear
from hermes_cli.kanban_mcp_adapters import (
    MCPAdapterError,
    MCPToolCaller,
    RegistryMCPToolCaller,
)


LinearMCPFailureKind = Literal[
    "auth",
    "permission",
    "not_found",
    "rate_limited",
    "timeout",
    "unavailable",
    "validation",
    "ambiguous",
]
LinearMCPFailureStage = Literal[
    "configuration",
    "connection",
    "discovery",
    "resource",
]

LINEAR_MCP_READ_TOOLS = frozenset({
    "get_attachment",
    "list_comments",
    "get_issue",
    "list_issues",
    "list_issue_statuses",
    "get_issue_status",
    "list_issue_labels",
    "list_projects",
    "get_project",
    "get_diff",
    "list_diffs",
    "get_diff_threads",
    "list_teams",
    "get_team",
})
_TRANSIENT_FAILURES = frozenset({"rate_limited", "timeout", "unavailable"})
_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_ISSUE_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")
_FULL_DIFF_IDENTIFIER_RE = re.compile(r"^([^/\s]+)/([^#\s]+)#([1-9]\d*)$")
_LINEAR_ENDPOINT_HOST = "mcp.linear.app"
_LINEAR_ENDPOINT_PATH = "/mcp"
_MAX_TIMEOUT_SECONDS = 120
_MAX_RETRY_ATTEMPTS = 5
_MAX_PAGES = 100
_MAX_PAGE_SIZE = 250


class LinearMCPReadError(RuntimeError):
    """A Linear MCP read failed before producing trusted normalized evidence."""

    def __init__(
        self,
        message: str,
        *,
        kind: LinearMCPFailureKind,
        attempts: int = 1,
        stage: LinearMCPFailureStage = "resource",
    ) -> None:
        super().__init__(message)
        self.kind: LinearMCPFailureKind = kind
        self.attempts: int = attempts
        self.stage: LinearMCPFailureStage = stage
        self.retryable = kind in _TRANSIENT_FAILURES


_RegisterServers = Callable[[dict[str, dict[str, Any]]], Sequence[str]]
_CallerFactory = Callable[[str, frozenset[str], int], MCPToolCaller]


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LinearMCPReadError(
            f"{field} must be a non-empty string",
            kind="validation",
        )
    return value.strip()


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _bounded_int(value: Any, field: str, *, maximum: int) -> int:
    if isinstance(value, bool):
        raise LinearMCPReadError(
            f"{field} must be a positive integer",
            kind="validation",
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise LinearMCPReadError(
            f"{field} must be a positive integer",
            kind="validation",
        ) from exc
    if parsed < 1 or parsed > maximum:
        raise LinearMCPReadError(
            f"{field} must be between 1 and {maximum}",
            kind="validation",
        )
    return parsed


def _boolish(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on", "enabled"}


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise LinearMCPReadError(
            "Linear MCP result is not JSON serializable",
            kind="validation",
        ) from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _tool_name(server_name: str, provider_tool: str) -> str:
    if not _SERVER_NAME_RE.fullmatch(server_name):
        raise LinearMCPReadError("MCP server name is invalid", kind="validation")
    safe_tool = re.sub(r"[^A-Za-z0-9_]", "_", provider_tool)
    return f"mcp__{server_name}__{safe_tool}"


def _failure_kind(value: Any) -> LinearMCPFailureKind:
    code = str(value or "").strip().casefold().replace("-", "_")
    if code == "auth" or any(
        token in code for token in ("invalid_auth", "not_authed", "unauthorized", "401")
    ):
        return "auth"
    if any(token in code for token in ("missing_scope", "forbidden", "permission", "403")):
        return "permission"
    if any(token in code for token in ("not_found", "missing", "404")):
        return "not_found"
    if any(token in code for token in ("rate_limit", "ratelimit", "429", "quota")):
        return "rate_limited"
    if any(token in code for token in ("timeout", "timed_out", "deadline")):
        return "timeout"
    if any(token in code for token in ("unavailable", "transport", "network", "connect")):
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


def _unwrap_result(raw: Any, *, tool_name: str) -> Any:
    value = _decode_json_string(raw) if isinstance(raw, str) else raw
    if isinstance(value, Mapping) and value.get("error"):
        code = value.get("error")
        raise LinearMCPReadError(
            f"Linear MCP tool {tool_name} failed ({code})",
            kind=_failure_kind(code),
        )
    if isinstance(value, Mapping) and set(value).issubset(
        {"result", "structuredContent", "content"}
    ):
        value = value.get("result", value.get("structuredContent", value.get("content")))
        value = _decode_json_string(value) if isinstance(value, str) else value
    if isinstance(value, Mapping) and (value.get("ok") is False or value.get("error")):
        code = value.get("error") or "provider_error"
        raise LinearMCPReadError(
            f"Linear MCP tool {tool_name} failed ({code})",
            kind=_failure_kind(code),
        )
    return value


def _as_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LinearMCPReadError(f"{field} must be an object", kind="validation")
    return value


def _as_sequence(value: Any, field: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise LinearMCPReadError(f"{field} must be an array", kind="validation")
    return value


def _source_revision(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        raise LinearMCPReadError(
            "Linear issue updatedAt/sourceRevision is required",
            kind="validation",
        )
    if isinstance(value, (int, float)):
        revision = int(value)
        if revision < 0:
            raise LinearMCPReadError(
                "Linear source revision must be non-negative",
                kind="validation",
            )
        return revision
    text = str(value).strip()
    try:
        revision = int(text)
    except ValueError:
        pass
    else:
        if revision < 0:
            raise LinearMCPReadError(
                "Linear source revision must be non-negative",
                kind="validation",
            )
        return revision
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LinearMCPReadError(
            "Linear source revision timestamp is invalid",
            kind="validation",
        ) from exc
    if parsed.tzinfo is None:
        raise LinearMCPReadError(
            "Linear source revision timestamp must include a timezone",
            kind="validation",
        )
    delta = parsed.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )


def _issue_identifier(raw: Mapping[str, Any], issue_url: str, issue_id: str) -> str:
    explicit = _optional_text(raw.get("identifier"))
    if explicit:
        return explicit
    if _ISSUE_IDENTIFIER_RE.fullmatch(issue_id):
        return issue_id
    parts = [unquote(part) for part in urlparse(issue_url).path.split("/") if part]
    if "issue" in parts:
        index = parts.index("issue")
        if index + 1 < len(parts) and _ISSUE_IDENTIFIER_RE.fullmatch(parts[index + 1]):
            return parts[index + 1]
    raise LinearMCPReadError(
        "Linear issue identifier is unavailable",
        kind="validation",
    )


def _linear_issue_url(value: Any) -> str:
    url = _required_text(value, "issue.url")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "linear.app":
        raise LinearMCPReadError(
            "Linear issue URL must use https://linear.app",
            kind="validation",
        )
    return url


def _normalize_labels(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        value = value.get("nodes", value.get("labels", ()))
    labels: set[str] = set()
    for item in _as_sequence(value, "issue.labels"):
        if isinstance(item, Mapping):
            name = _optional_text(item.get("name"))
        else:
            name = _optional_text(item)
        if not name:
            raise LinearMCPReadError(
                "Linear issue label is missing a name",
                kind="validation",
            )
        labels.add(name)
    return tuple(sorted(labels))


def _name_and_id(
    value: Any,
    explicit_id: Any,
) -> tuple[Optional[str], Optional[str]]:
    if isinstance(value, Mapping):
        name = _optional_text(value.get("name"))
        identity = _optional_text(value.get("id")) or _optional_text(explicit_id)
        return name, identity
    return _optional_text(value), _optional_text(explicit_id)


def _parse_github_pr_url(value: str) -> Optional[linear.PullRequestRef]:
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname not in {"github.com", "www.github.com"}:
        return None
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) == 4 and parts[2] == "pull" and parts[3].isdigit():
        return linear.PullRequestRef(f"{parts[0]}/{parts[1]}", int(parts[3]))
    if "pull" in parts:
        raise LinearMCPReadError(
            "Linear attachment contains an ambiguous GitHub pull-request link",
            kind="ambiguous",
        )
    return None


def _diff_pr_ref(raw: Any) -> linear.PullRequestRef:
    value = _as_mapping(raw, "Linear diff")
    if isinstance(value.get("diff"), Mapping):
        value = _as_mapping(value["diff"], "Linear diff")
    refs: set[linear.PullRequestRef] = set()
    url = _optional_text(value.get("url"))
    if url:
        ref = _parse_github_pr_url(url)
        if ref is not None:
            refs.add(ref)
    full_identifier = _optional_text(value.get("fullIdentifier"))
    if full_identifier:
        match = _FULL_DIFF_IDENTIFIER_RE.fullmatch(full_identifier)
        if not match:
            raise LinearMCPReadError(
                "Linear diff fullIdentifier is ambiguous",
                kind="ambiguous",
            )
        refs.add(linear.PullRequestRef(f"{match.group(1)}/{match.group(2)}", int(match.group(3))))
    if len(refs) != 1:
        raise LinearMCPReadError(
            "Linear diff did not resolve to exactly one GitHub pull request",
            kind="ambiguous",
        )
    return next(iter(refs))


@dataclass(frozen=True)
class LinearMCPConfig:
    server_name: str = "linear"
    provider_timeout_seconds: int = 20
    retry_attempts: int = 3
    page_size: int = 50
    max_pages: int = 10

    def __post_init__(self) -> None:
        if not _SERVER_NAME_RE.fullmatch(self.server_name):
            raise LinearMCPReadError("MCP server name is invalid", kind="validation")
        object.__setattr__(
            self,
            "provider_timeout_seconds",
            _bounded_int(
                self.provider_timeout_seconds,
                "provider_timeout_seconds",
                maximum=_MAX_TIMEOUT_SECONDS,
            ),
        )
        object.__setattr__(
            self,
            "retry_attempts",
            _bounded_int(
                self.retry_attempts,
                "retry_attempts",
                maximum=_MAX_RETRY_ATTEMPTS,
            ),
        )
        object.__setattr__(
            self,
            "page_size",
            _bounded_int(self.page_size, "page_size", maximum=_MAX_PAGE_SIZE),
        )
        object.__setattr__(
            self,
            "max_pages",
            _bounded_int(self.max_pages, "max_pages", maximum=_MAX_PAGES),
        )

    @classmethod
    def from_mapping(cls, value: Optional[Mapping[str, Any]]) -> "LinearMCPConfig":
        raw = value if isinstance(value, Mapping) else {}
        try:
            return cls(
                server_name=str(raw.get("mcp_server") or "linear").strip(),
                provider_timeout_seconds=raw.get("provider_timeout_seconds", 20),
                retry_attempts=raw.get("retry_attempts", 3),
                page_size=raw.get("page_size", 50),
                max_pages=raw.get("max_pages", 10),
            )
        except LinearMCPReadError as exc:
            raise LinearMCPReadError(
                str(exc),
                kind=exc.kind,
                stage="configuration",
            ) from exc


class LinearMCPReadAdapter:
    """Normalize only explicitly allowlisted Linear MCP reads."""

    def __init__(
        self,
        caller: MCPToolCaller,
        *,
        config: LinearMCPConfig,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._caller = caller
        self.config = config
        self._sleeper = sleeper
        self._observations: dict[tuple[str, int], linear.LinearIssueSnapshot] = {}

    def _call(self, provider_tool: str, arguments: Mapping[str, Any]) -> Any:
        if provider_tool not in LINEAR_MCP_READ_TOOLS:
            raise LinearMCPReadError(
                f"Linear MCP tool {provider_tool} is outside the read-only allowlist",
                kind="permission",
            )
        tool_name = _tool_name(self.config.server_name, provider_tool)
        for attempt in range(1, self.config.retry_attempts + 1):
            try:
                raw = self._caller.call(tool_name, dict(arguments))
                return _unwrap_result(raw, tool_name=tool_name)
            except LinearMCPReadError as exc:
                error = LinearMCPReadError(str(exc), kind=exc.kind, attempts=attempt)
            except MCPAdapterError as exc:
                error = LinearMCPReadError(
                    str(exc),
                    kind=_failure_kind(exc.kind),
                    attempts=attempt,
                )
            except Exception as exc:
                error = LinearMCPReadError(
                    f"Linear MCP tool {provider_tool} failed",
                    kind="unavailable",
                    attempts=attempt,
                )
                error.__cause__ = exc
            if not error.retryable or attempt >= self.config.retry_attempts:
                raise error
            self._sleeper(min(0.25 * (2 ** (attempt - 1)), 2.0))
        raise AssertionError("unreachable Linear MCP retry state")

    def _paginate(
        self,
        provider_tool: str,
        *,
        item_key: str,
        arguments: Optional[Mapping[str, Any]] = None,
        limit: Optional[int] = None,
    ) -> tuple[Mapping[str, Any], ...]:
        maximum = self.config.page_size * self.config.max_pages
        total_limit = maximum if limit is None else _bounded_int(limit, "limit", maximum=maximum)
        base_arguments = dict(arguments or {})
        cursor: Optional[str] = None
        seen_cursors: set[str] = set()
        by_id: dict[str, tuple[str, Mapping[str, Any]]] = {}
        anonymous: list[Mapping[str, Any]] = []

        for page_index in range(self.config.max_pages):
            remaining = total_limit - len(by_id) - len(anonymous)
            if remaining <= 0:
                break
            page_arguments = dict(base_arguments)
            page_arguments["limit"] = min(self.config.page_size, remaining)
            if cursor is not None:
                page_arguments["cursor"] = cursor
            raw = _as_mapping(
                self._call(provider_tool, page_arguments),
                f"Linear {provider_tool} page",
            )
            page_items = _as_sequence(raw.get(item_key), f"Linear {item_key}")
            for item in page_items:
                mapping = _as_mapping(item, f"Linear {item_key} item")
                normalized = dict(mapping)
                identity = _optional_text(mapping.get("id"))
                if identity is None:
                    anonymous.append(normalized)
                    continue
                digest = _digest(normalized)
                existing = by_id.get(identity)
                if existing is not None and existing[0] != digest:
                    raise LinearMCPReadError(
                        f"Linear pagination returned conflicting data for {identity}",
                        kind="ambiguous",
                    )
                by_id.setdefault(identity, (digest, normalized))

            page_info = raw.get("pageInfo")
            page_info = page_info if isinstance(page_info, Mapping) else {}
            has_next_raw = raw.get("hasNextPage", page_info.get("hasNextPage"))
            candidates = (
                raw.get("nextCursor"),
                raw.get("endCursor"),
                raw.get("cursor"),
                page_info.get("nextCursor"),
                page_info.get("endCursor"),
            )
            next_cursor = next(
                (
                    text
                    for text in (_optional_text(candidate) for candidate in candidates)
                    if text is not None and text != cursor
                ),
                None,
            )
            has_next = _boolish(has_next_raw, default=next_cursor is not None)
            if not has_next:
                cursor = None
                break
            if next_cursor is None or next_cursor in seen_cursors:
                raise LinearMCPReadError(
                    "Linear pagination cursor is missing or repeated",
                    kind="ambiguous",
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            if cursor is not None:
                raise LinearMCPReadError(
                    "Linear pagination exceeded the configured page bound",
                    kind="unavailable",
                )

        return tuple(item[1] for item in by_id.values()) + tuple(anonymous)

    def list_teams(
        self,
        *,
        query: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> tuple[Mapping[str, Any], ...]:
        arguments = {"query": query} if query else {}
        return self._paginate("list_teams", item_key="teams", arguments=arguments, limit=limit)

    def get_team(self, query: str) -> Mapping[str, Any]:
        """Resolve one team through Linear's structured name/key lookup."""
        expected = _required_text(query, "team_query")
        value = self._call("get_team", {"query": expected})
        if value is None:
            raise LinearMCPReadError(
                f"Linear team {expected!r} was not found",
                kind="not_found",
            )
        team = dict(_as_mapping(value, "Linear team"))
        team["id"] = _required_text(team.get("id"), "team.id")
        team["name"] = _required_text(team.get("name"), "team.name")
        return team

    def list_issues(
        self,
        *,
        team: Optional[str] = None,
        query: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> tuple[Mapping[str, Any], ...]:
        arguments: dict[str, Any] = {
            "fields": [
                "id",
                "title",
                "url",
                "updatedAt",
                "status",
                "statusType",
                "labels",
                "team",
                "teamId",
                "project",
                "projectId",
            ]
        }
        if team:
            arguments["team"] = team
        if query:
            arguments["query"] = query
        return self._paginate("list_issues", item_key="issues", arguments=arguments, limit=limit)

    def list_comments(
        self,
        issue_id: str,
        *,
        limit: Optional[int] = None,
    ) -> tuple[Mapping[str, Any], ...]:
        return self._paginate(
            "list_comments",
            item_key="comments",
            arguments={"issueId": _required_text(issue_id, "issue_id")},
            limit=limit,
        )

    def _attachment_refs(self, raw: Mapping[str, Any]) -> Optional[tuple[linear.PullRequestRef, ...]]:
        if "attachments" not in raw or raw.get("attachments") is None:
            return None
        refs: set[linear.PullRequestRef] = set()
        complete = True
        for item in _as_sequence(raw.get("attachments"), "issue.attachments"):
            if not isinstance(item, Mapping):
                complete = False
                continue
            url = _optional_text(item.get("url"))
            if not url:
                complete = False
                continue
            github_ref = _parse_github_pr_url(url)
            if github_ref is not None:
                refs.add(github_ref)
                continue
            parsed = urlparse(url)
            path_parts = [part for part in parsed.path.split("/") if part]
            if parsed.scheme == "https" and parsed.hostname == "linear.app" and "review" in path_parts:
                refs.add(_diff_pr_ref(self._call("get_diff", {"urlOrId": url})))
        return tuple(sorted(refs)) if complete else None

    def read_issue(self, issue_id: str) -> linear.LinearIssueSnapshot:
        requested = _required_text(issue_id, "issue_id")
        value = self._call("get_issue", {"id": requested})
        if value is None:
            raise LinearMCPReadError(
                f"Linear issue {requested!r} was not found",
                kind="not_found",
            )
        raw = _as_mapping(value, "Linear issue")
        if isinstance(raw.get("issue"), Mapping):
            raw = _as_mapping(raw["issue"], "Linear issue")
        stable_id = _required_text(raw.get("id"), "issue.id")
        issue_url = _linear_issue_url(raw.get("url"))
        identifier = _issue_identifier(raw, issue_url, stable_id)
        if requested.casefold() not in {
            stable_id.casefold(),
            identifier.casefold(),
        }:
            raise LinearMCPReadError(
                "Linear MCP returned a different issue than requested",
                kind="ambiguous",
            )

        state_name, state_id = _name_and_id(raw.get("status"), raw.get("statusId"))
        del state_id
        team_name, team_id = _name_and_id(raw.get("team"), raw.get("teamId"))
        project_name, project_id = _name_and_id(raw.get("project"), raw.get("projectId"))
        state_name = _required_text(state_name, "issue.status")
        team_name = _required_text(team_name, "issue.team")
        team_id = _required_text(team_id, "issue.teamId")
        if "labels" not in raw:
            raise LinearMCPReadError(
                "issue.labels must be present even when empty",
                kind="validation",
            )
        source_revision = _source_revision(
            raw.get("sourceRevision", raw.get("updatedAt"))
        )
        attachments = self._attachment_refs(raw)
        semantic = {
            "issue_id": stable_id,
            "identifier": identifier,
            "title": _required_text(raw.get("title"), "issue.title"),
            "issue_url": issue_url,
            "source_revision": source_revision,
            "state": state_name,
            "state_type": _optional_text(raw.get("statusType")),
            "labels": list(_normalize_labels(raw.get("labels"))),
            "team_id": team_id,
            "team_name": team_name,
            "project_id": project_id,
            "project_name": project_name,
            "attachments_complete": attachments is not None,
            "attachments": [
                {"repository": ref.repository, "number": ref.number}
                for ref in (attachments or ())
            ],
        }
        observation_id = (
            f"linear-mcp:{stable_id}:{source_revision}:{_digest(semantic)[:20]}"
        )
        snapshot = linear.LinearIssueSnapshot(
            issue_id=stable_id,
            identifier=identifier,
            title=semantic["title"],
            issue_url=issue_url,
            source_revision=source_revision,
            attachments=attachments,
            state=state_name,
            state_type=semantic["state_type"],
            labels=tuple(semantic["labels"]),
            team_id=team_id,
            team_name=team_name,
            project_id=project_id,
            project_name=project_name,
            observation_id=observation_id,
        )
        key = (stable_id, source_revision)
        existing = self._observations.get(key)
        if existing is not None:
            if existing.digest() != snapshot.digest():
                raise LinearMCPReadError(
                    "Linear MCP reused a source revision for different issue state",
                    kind="validation",
                )
            return existing
        self._observations[key] = snapshot
        return snapshot


@dataclass(frozen=True)
class LinearMCPAdapterBundle:
    adapter: LinearMCPReadAdapter
    config: LinearMCPConfig
    oauth_configured: bool
    registered_read_tools: tuple[str, ...]
    write_enabled: bool = False


def _default_caller_factory(
    server_name: str,
    allowed_tools: frozenset[str],
    timeout_seconds: int,
) -> MCPToolCaller:
    return RegistryMCPToolCaller(server_name, allowed_tools, timeout_seconds)


def _validate_server_config(
    server_name: str,
    raw: Any,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise LinearMCPReadError(
            f"Linear MCP server {server_name!r} is not configured",
            kind="unavailable",
        )
    server = dict(raw)
    if not _boolish(server.get("enabled"), default=True):
        raise LinearMCPReadError(
            f"Linear MCP server {server_name!r} is disabled",
            kind="unavailable",
        )
    if str(server.get("auth") or "").strip().casefold() != "oauth":
        raise LinearMCPReadError(
            "Linear MCP server must use OAuth",
            kind="auth",
        )
    parsed = urlparse(str(server.get("url") or ""))
    if (
        parsed.scheme != "https"
        or parsed.hostname != _LINEAR_ENDPOINT_HOST
        or parsed.path.rstrip("/") != _LINEAR_ENDPOINT_PATH
        or parsed.netloc != _LINEAR_ENDPOINT_HOST
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise LinearMCPReadError(
            "Linear MCP server endpoint is not the official HTTPS endpoint",
            kind="validation",
        )
    return server


def build_linear_mcp_adapter(
    *,
    config: LinearMCPConfig,
    mcp_servers: Optional[Mapping[str, Any]] = None,
    register_servers: Optional[_RegisterServers] = None,
    caller_factory: _CallerFactory = _default_caller_factory,
    sleeper: Callable[[float], None] = time.sleep,
) -> LinearMCPAdapterBundle:
    """Register only Linear read tools and construct the typed adapter."""

    if mcp_servers is None:
        from hermes_cli.config import load_config

        runtime = load_config() or {}
        configured = runtime.get("mcp_servers")
        mcp_servers = configured if isinstance(configured, Mapping) else {}
    server = _validate_server_config(
        config.server_name,
        mcp_servers.get(config.server_name) if isinstance(mcp_servers, Mapping) else None,
    )
    selected = dict(server)
    selected["tools"] = {
        "include": sorted(LINEAR_MCP_READ_TOOLS),
        "prompts": False,
        "resources": False,
    }
    selected["timeout"] = config.provider_timeout_seconds
    selected["connect_timeout"] = min(
        config.provider_timeout_seconds,
        _bounded_int(
            selected.get("connect_timeout", config.provider_timeout_seconds),
            f"mcp_servers.{config.server_name}.connect_timeout",
            maximum=_MAX_TIMEOUT_SECONDS,
        ),
    )
    registrar = register_servers
    if registrar is None:
        from tools.mcp_tool import register_mcp_servers

        registrar = register_mcp_servers
    try:
        registered = tuple(registrar({config.server_name: selected}))
    except LinearMCPReadError:
        raise
    except Exception as exc:
        raise LinearMCPReadError(
            "Linear MCP connection failed before read-tool discovery",
            kind="unavailable",
            stage="connection",
        ) from exc
    required = frozenset(
        _tool_name(config.server_name, tool) for tool in LINEAR_MCP_READ_TOOLS
    )
    selected_prefix = f"mcp__{config.server_name}__"
    registered_for_selected_server = frozenset(
        name for name in registered if name.startswith(selected_prefix)
    )
    unexpected = sorted(registered_for_selected_server.difference(required))
    if unexpected:
        raise LinearMCPReadError(
            "Linear MCP discovery exposed non-allowlisted tools for the selected server",
            kind="permission",
            stage="discovery",
        )
    discovered = tuple(sorted(required.intersection(registered_for_selected_server)))
    missing = sorted(required.difference(discovered))
    if missing:
        raise LinearMCPReadError(
            f"Linear MCP discovery is missing {len(missing)} required read tool(s)",
            kind="unavailable",
            stage="discovery",
        )
    caller = caller_factory(
        config.server_name,
        required,
        config.provider_timeout_seconds,
    )
    return LinearMCPAdapterBundle(
        adapter=LinearMCPReadAdapter(caller, config=config, sleeper=sleeper),
        config=config,
        oauth_configured=True,
        registered_read_tools=discovered,
        write_enabled=False,
    )


def _configured_stage(
    config: LinearMCPConfig,
    mcp_servers: Mapping[str, Any],
) -> tuple[bool, bool]:
    raw = mcp_servers.get(config.server_name)
    if not isinstance(raw, Mapping) or not _boolish(raw.get("enabled"), default=True):
        return False, False
    return True, str(raw.get("auth") or "").strip().casefold() == "oauth"


def _select_team(
    teams: Sequence[Mapping[str, Any]],
    query: Optional[str],
) -> Mapping[str, Any]:
    if query:
        expected = query.strip().casefold()
        matches = [
            team
            for team in teams
            if expected
            in {
                str(team.get("id") or "").strip().casefold(),
                str(team.get("name") or "").strip().casefold(),
                str(team.get("key") or "").strip().casefold(),
            }
        ]
    else:
        matches = list(teams)
    if not matches:
        raise LinearMCPReadError(
            "Linear resource authorization probe found no matching team",
            kind="not_found",
        )
    if len(matches) != 1:
        raise LinearMCPReadError(
            "Linear resource authorization probe is ambiguous across teams",
            kind="ambiguous",
        )
    return matches[0]


def diagnose_linear_mcp(
    *,
    config: LinearMCPConfig,
    mcp_servers: Mapping[str, Any],
    team_query: Optional[str] = None,
    issue_id: Optional[str] = None,
    bundle_builder: Callable[..., LinearMCPAdapterBundle] = build_linear_mcp_adapter,
) -> dict[str, Any]:
    """Probe each read-only readiness gate without creating events or writes."""

    configured, oauth_configured = _configured_stage(config, mcp_servers)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "status": "blocked",
        "checked_at": int(time.time()),
        "server_name": config.server_name,
        "transport": "http_oauth_2_1_pkce" if oauth_configured else "unverified",
        "read_only": True,
        "allowed_read_tools": sorted(LINEAR_MCP_READ_TOOLS),
        "registered_read_tool_count": 0,
        "stages": {
            "configured": configured,
            "connected": False,
            "discovered": False,
            "resource_authorized": False,
            "write_enabled": False,
        },
        "oauth_configured": oauth_configured,
        "webhooks_implemented": False,
        "oauth_event_delivery": False,
        "polling_runner": {
            "mode": "operator_scheduled_read_probe",
            "command": (
                "hermes kanban linear-mcp health --team <team> "
                "--issue-id <issue-id> --json"
            ),
            "recommended_schedule": "every 5m",
            "kanban_mutation": False,
            "snapshot_ingestion_implemented": False,
            "production_activation": "blocked_until_snapshot_ingestion_is_reviewed",
        },
        "external_side_effects": "none",
        "requires_gateway_restart": False,
        "resource": None,
        "failure": None,
    }
    if not configured:
        payload["failure"] = {
            "kind": "unavailable",
            "message": f"Linear MCP server {config.server_name!r} is not configured",
            "retryable": True,
            "attempts": 1,
            "stage": "configuration",
        }
        return payload

    try:
        bundle = bundle_builder(config=config, mcp_servers=mcp_servers)
        payload["stages"]["connected"] = True
        payload["registered_read_tool_count"] = len(bundle.registered_read_tools)
        payload["stages"]["discovered"] = (
            len(bundle.registered_read_tools) == len(LINEAR_MCP_READ_TOOLS)
        )
        # Linear documents team lookup by stable name/key. Team UUIDs are
        # connection-scoped in the live OAuth MCP response, so copied UUIDs
        # from prior connections are not accepted as operator selectors.
        if team_query:
            selected_team = bundle.adapter.get_team(team_query)
        else:
            selected_team = _select_team(bundle.adapter.list_teams(), None)
        selected_team_id = _required_text(selected_team.get("id"), "team.id")
        selected_team_name = _required_text(selected_team.get("name"), "team.name")
        resource: dict[str, Any] = {
            "team_query": team_query,
            "team_id": selected_team_id,
            "team_name": selected_team_name,
            "issue_id": None,
            "issue_identifier": None,
            "source_revision": None,
            "linked_pr_count": None,
        }
        if issue_id:
            snapshot = bundle.adapter.read_issue(issue_id)
            if (
                snapshot.team_id != selected_team_id
                or snapshot.team_name is None
                or snapshot.team_name.casefold() != selected_team_name.casefold()
            ):
                raise LinearMCPReadError(
                    "Linear issue belongs to a different team than the probe",
                    kind="ambiguous",
                )
            resource.update({
                "issue_id": snapshot.issue_id,
                "issue_identifier": snapshot.identifier,
                "source_revision": snapshot.source_revision,
                "linked_pr_count": (
                    len(snapshot.attachments)
                    if snapshot.attachments is not None
                    else None
                ),
                "attachments_complete": snapshot.attachments_complete,
                "state": snapshot.state,
                "label_count": len(snapshot.labels),
            })
        payload["resource"] = resource
        payload["stages"]["resource_authorized"] = True
        payload["status"] = "ready"
    except LinearMCPReadError as exc:
        if exc.stage == "discovery":
            payload["stages"]["connected"] = True
        payload["failure"] = {
            "kind": exc.kind,
            "message": str(exc),
            "retryable": exc.retryable,
            "attempts": exc.attempts,
            "stage": exc.stage,
        }
    return payload
