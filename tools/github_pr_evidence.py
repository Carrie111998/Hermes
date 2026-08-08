"""Tuple-bound, read-only GitHub PR evidence for trusted static webhooks.

The model receives no repository, ref, path, URL, or run-id parameters. A
trusted static webhook route installs an :class:`EvidenceScope`; the tool then
returns opaque, single-use cursors whose authority is derived only from that
immutable tuple and from IDs/paths returned by GitHub itself.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import posixpath
import re
import secrets
import subprocess
import threading
import zipfile
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Callable, Iterator, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from tools.registry import registry

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_CLOSING_ISSUES_QUERY = """
query($owner:String!,$name:String!,$number:Int!,$after:String) {
  repository(owner:$owner,name:$name) {
    pullRequest(number:$number) {
      closingIssuesReferences(first:100,after:$after) {
        nodes { number repository { nameWithOwner } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".strip()
_MAX_JSON_BYTES = 10 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
_MAX_ARCHIVE_ENTRIES = 200
_MAX_TOTAL_UNCOMPRESSED = 100 * 1024 * 1024
_MAX_ENTRY_BYTES = 2 * 1024 * 1024
_ARCHIVE_ENTRY_CHUNK_BYTES = 12 * 1024
_MAX_COMPRESSION_RATIO = 100
_MAX_RESULT_CHARS = 90_000
_MAX_INLINE_STRING_CHARS = 20_000
_MAX_CONCISE_PATCH_CHARS = 4_000
_MAX_PAGE_CHARS = 60_000
_MAX_RECOVERY_CURSOR_INVENTORY = 16
_MAX_EXPOSED_CURSORS = 16
# A pull request can expose up to 3,000 changed files. Exact-head review may
# materialize both base and head blobs plus the bounded 200-entry artifact
# inventory, so the old 200-cursor ceiling rejected legitimate large reviews.
# Keep a hard per-scope ceiling, but size it for the API's bounded inventory.
_MAX_ACTIVE_CURSORS = 10_000
_CANONICAL_REVIEW_PATHS = {
    "AGENTS.md",
    "CLAUDE.md",
    "docs/DEV.md",
    "docs/TESTING.md",
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "playwright.config.ts",
    "playwright.config.js",
    "playwright.config.mjs",
    "playwright.config.cjs",
    "turbo.json",
}
_REFERENCED_PATH_RE = re.compile(
    r"(?:^|[\s'\"`:=,(])((?:\.?\.?/)?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)"
)


@dataclass(frozen=True)
class _Cursor:
    kind: str
    endpoint: str = ""
    data: Any = None
    required: bool = True


@dataclass
class EvidenceScope:
    contract_version: str
    repository: str
    pr_number: int
    base_sha: str
    head_sha: str
    concise_review: bool = False
    cursors: dict[str, _Cursor] = field(default_factory=dict)
    manifest_cursors: dict[str, str] = field(default_factory=dict)
    fatal_error: str = ""
    manifest_created: bool = False
    expected_changed_files: Optional[int] = None
    observed_changed_files: Optional[int] = None
    pull_validated: bool = False
    workflow_runs_observed: int = 0
    tree_diff_reconciled: bool = False
    canonical_files_materialized: bool = False
    required_logs_materialized: bool = False
    required_artifact_inventories_materialized: bool = False
    base_tree_sha: str = ""
    merge_base_sha: str = ""
    merge_base_tree_sha: str = ""
    head_tree_sha: str = ""
    required_execution_gates: tuple[str, ...] = ()
    baseline_execution_gates: tuple[str, ...] = ()
    execution_gate_policy_version: str = ""
    execution_gate_policy_sha256: str = ""
    execution_gate_contracts: dict[str, dict[str, Any]] = field(default_factory=dict)
    gate_resolution_manifest_sha256: str = ""
    gate_resolution_valid: bool = False
    execution_attestation_public_key: bytes = b""
    execution_attestation_loader: Optional[Callable[[], tuple[bytes, str]]] = field(
        default=None, repr=False
    )
    gate_resolution_loader: Optional[Callable[[], tuple[bytes, str]]] = field(
        default=None, repr=False
    )
    execution_attestation_valid: bool = False
    execution_attestation: dict[str, Any] = field(default_factory=dict)
    api_changed_inventory: set[tuple[str, str, str]] = field(default_factory=set)
    tree_raw_inventory: set[tuple[str, str, str]] = field(default_factory=set)
    tree_changed_inventory: set[tuple[str, str, str]] = field(default_factory=set)
    base_tree: dict[str, dict[str, Any]] = field(default_factory=dict)
    merge_base_tree: dict[str, dict[str, Any]] = field(default_factory=dict)
    head_tree: dict[str, dict[str, Any]] = field(default_factory=dict)
    blob_tokens_by_sha: dict[str, str] = field(default_factory=dict)
    canonical_blob_tokens: set[str] = field(default_factory=set)
    required_log_tokens: set[str] = field(default_factory=set)
    required_artifact_tokens: set[str] = field(default_factory=set)
    observed_action_jobs: dict[int, dict[str, Any]] = field(default_factory=dict)
    logs_discovered: bool = False
    artifacts_discovered: bool = False
    exposed_cursors: set[str] = field(default_factory=set)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def __post_init__(self) -> None:
        if self.contract_version != "v2":
            raise ValueError("Unsupported evidence contract version")
        if not _REPO_RE.fullmatch(self.repository):
            raise ValueError("Invalid evidence repository")
        if isinstance(self.pr_number, bool) or self.pr_number <= 0:
            raise ValueError("Invalid evidence PR number")
        if not _SHA_RE.fullmatch(self.base_sha):
            raise ValueError("Invalid evidence base SHA")
        if not _SHA_RE.fullmatch(self.head_sha):
            raise ValueError("Invalid evidence head SHA")

    @property
    def tuple_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "repository": self.repository,
            "pr_number": self.pr_number,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
        }

    @property
    def cache_key(self) -> str:
        return ":".join(
            (
                self.contract_version,
                self.repository,
                str(self.pr_number),
                self.base_sha,
                self.head_sha,
            )
        )

    @property
    def required_cursors(self) -> set[str]:
        with self.lock:
            return {token for token, cursor in self.cursors.items() if cursor.required}


_ACTIVE_SCOPE: ContextVar[Optional[EvidenceScope]] = ContextVar(
    "github_pr_evidence_scope", default=None
)


@contextmanager
def evidence_scope(scope: EvidenceScope) -> Iterator[EvidenceScope]:
    """Install one scope for the current task/thread and clean it up afterward."""
    token = _ACTIVE_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _ACTIVE_SCOPE.reset(token)


def current_evidence_scope() -> Optional[EvidenceScope]:
    return _ACTIVE_SCOPE.get()


def evidence_scope_cache_key() -> Optional[str]:
    scope = current_evidence_scope()
    return scope.cache_key if scope is not None else None


def check_github_pr_evidence_requirements() -> bool:
    return current_evidence_scope() is not None


# Registry/model schema caches must evaluate this check for every ContextVar
# scope rather than retaining one route's True verdict for another route.
check_github_pr_evidence_requirements._hermes_context_scoped = True  # type: ignore[attr-defined]


def _new_cursor(scope: EvidenceScope, cursor: _Cursor) -> str:
    with scope.lock:
        if len(scope.cursors) >= _MAX_ACTIVE_CURSORS:
            raise RuntimeError("GitHub evidence cursor limit exceeded")
        while True:
            token = secrets.token_urlsafe(24)
            if token not in scope.cursors:
                scope.cursors[token] = cursor
                return token


def _error(scope: Optional[EvidenceScope], message: str, *, fatal: bool = False) -> str:
    if scope is not None and fatal:
        scope.fatal_error = message
    return json.dumps(
        {"success": False, "error": message, "fatal": fatal}, ensure_ascii=False
    )


def _run_gh_json(args: list[str]) -> Any:
    result = subprocess.run(
        ["gh", "api", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError("GitHub evidence request failed")
    if len(result.stdout.encode("utf-8")) > _MAX_JSON_BYTES:
        raise RuntimeError("GitHub evidence response exceeded the size limit")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GitHub evidence response was not valid JSON") from exc


def _run_gh_bytes(endpoint: str, *, accept: Optional[str] = None) -> bytes:
    command = ["gh", "api"]
    if accept:
        command.extend(["-H", f"Accept: {accept}"])
    command.append(endpoint)
    result = subprocess.run(
        command,
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError("GitHub evidence archive request failed")
    payload = result.stdout
    if not isinstance(payload, bytes):
        payload = str(payload).encode("utf-8", errors="replace")
    if len(payload) > _MAX_ARCHIVE_BYTES:
        raise RuntimeError("GitHub evidence archive exceeded the size limit")
    return payload


def _flatten_pages(value: Any) -> list[Any]:
    if value in ({}, None):
        return []
    if not isinstance(value, list):
        raise RuntimeError("Paginated GitHub evidence was not a list")
    flattened: list[Any] = []
    pending = list(value)
    while pending:
        item = pending.pop(0)
        if isinstance(item, list):
            pending[0:0] = item
        else:
            flattened.append(item)
    return flattened


def _paginated_items(value: Any, collection_key: str) -> list[Any]:
    """Flatten gh --paginate --slurp output, including keyed API pages."""
    pages = _flatten_pages(value)
    items: list[Any] = []
    for page in pages:
        if isinstance(page, dict) and collection_key in page:
            collection = page.get(collection_key)
            if not isinstance(collection, list):
                raise RuntimeError("Paginated GitHub evidence collection was malformed")
            items.extend(collection)
        else:
            items.append(page)
    return items


def _closing_issues(scope: EvidenceScope, value: Any) -> dict[str, Any]:
    try:
        connection = value["data"]["repository"]["pullRequest"][
            "closingIssuesReferences"
        ]
        nodes = connection["nodes"]
        page_info = connection["pageInfo"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("Closing-issue evidence was malformed") from exc
    if not isinstance(nodes, list) or not isinstance(page_info, dict):
        raise RuntimeError("Closing-issue evidence was malformed")

    issues = []
    for node in nodes:
        if not isinstance(node, dict):
            raise RuntimeError("Closing-issue evidence was malformed")
        repository = node.get("repository")
        number = node.get("number")
        if (
            not isinstance(repository, dict)
            or isinstance(number, bool)
            or not isinstance(number, int)
            or number <= 0
        ):
            raise RuntimeError("Closing-issue evidence was malformed")
        repo = repository.get("nameWithOwner")
        if repo != scope.repository:
            continue
        issues.append(
            {
                "repository": repo,
                "number": number,
                "cursor": _new_cursor(
                    scope,
                    _Cursor(
                        "linked_issue",
                        f"repos/{repo}/issues/{number}",
                        data=(repo, number),
                    ),
                ),
            }
        )

    has_next = page_info.get("hasNextPage")
    end_cursor = page_info.get("endCursor")
    if not isinstance(has_next, bool):
        raise RuntimeError("Closing-issue pagination metadata was malformed")
    next_cursor = None
    if has_next:
        if not isinstance(end_cursor, str) or not end_cursor:
            raise RuntimeError("Closing-issue pagination metadata was malformed")
        next_cursor = _new_cursor(
            scope, _Cursor("closing_issues", data={"after": end_cursor})
        )
    return {"issues": issues, "complete": not has_next, "next_cursor": next_cursor}


def _bound_large_strings(scope: EvidenceScope, value: Any, *, required: bool) -> Any:
    """Replace large untrusted strings with complete bounded data cursors."""
    if isinstance(value, str) and len(value) > _MAX_INLINE_STRING_CHARS:
        chunks = [
            value[offset : offset + _MAX_INLINE_STRING_CHARS]
            for offset in range(0, len(value), _MAX_INLINE_STRING_CHARS)
        ]
        return {
            "truncated_inline": True,
            "length": len(value),
            "cursors": [
                _new_cursor(
                    scope,
                    _Cursor(
                        "data",
                        data={
                            "part": index + 1,
                            "parts": len(chunks),
                            "text": chunk,
                        },
                        required=required,
                    ),
                )
                for index, chunk in enumerate(chunks)
            ],
        }
    if isinstance(value, list):
        return [_bound_large_strings(scope, item, required=required) for item in value]
    if isinstance(value, dict):
        return {
            key: _bound_large_strings(scope, item, required=required)
            for key, item in value.items()
        }
    return value


def _paginate_items(scope: EvidenceScope, items: list[Any], *, required: bool) -> Any:
    """Return one bounded page and opaque cursors for every remaining page."""
    pages: list[list[Any]] = []
    current: list[Any] = []
    current_size = 2
    for item in items:
        item_size = len(json.dumps(item, ensure_ascii=False)) + 1
        if current and current_size + item_size > _MAX_PAGE_CHARS:
            pages.append(current)
            current = []
            current_size = 2
        current.append(item)
        current_size += item_size
    if current or not pages:
        pages.append(current)
    if len(pages) == 1:
        return pages[0]
    remaining = [
        _new_cursor(
            scope,
            _Cursor(
                "data",
                data={
                    "page": index + 2,
                    "pages": len(pages),
                    "items": page,
                },
                required=required,
            ),
        )
        for index, page in enumerate(pages[1:])
    ]
    return {
        "page": 1,
        "pages": len(pages),
        "items": pages[0],
        "remaining_page_cursors": remaining,
    }


def _manifest(scope: EvidenceScope) -> str:
    if not scope.manifest_created:
        repo = scope.repository
        pr = scope.pr_number
        head = scope.head_sha
        endpoints = {
            "pull_request": _Cursor("pull_request", f"repos/{repo}/pulls/{pr}"),
            "tree_diff": _Cursor("tree_diff"),
            "changed_files": _Cursor(
                "changed_files", f"repos/{repo}/pulls/{pr}/files?per_page=100"
            ),
        }
        if not scope.concise_review:
            endpoints.update(
                {
                    "closing_issues": _Cursor("closing_issues"),
                    "issue_comments": _Cursor(
                        "issue_comments",
                        f"repos/{repo}/issues/{pr}/comments?per_page=100",
                    ),
                    "reviews": _Cursor(
                        "reviews", f"repos/{repo}/pulls/{pr}/reviews?per_page=100"
                    ),
                    "review_comments": _Cursor(
                        "review_comments",
                        f"repos/{repo}/pulls/{pr}/comments?per_page=100",
                    ),
                    "commits": _Cursor(
                        "commits", f"repos/{repo}/pulls/{pr}/commits?per_page=100"
                    ),
                    "checks": _Cursor(
                        "checks", f"repos/{repo}/commits/{head}/check-runs?per_page=100"
                    ),
                    "statuses": _Cursor(
                        "statuses", f"repos/{repo}/commits/{head}/statuses?per_page=100"
                    ),
                    "workflow_runs": _Cursor(
                        "workflow_runs",
                        f"repos/{repo}/actions/runs?head_sha={head}&per_page=100",
                    ),
                }
            )
        scope.manifest_cursors = {
            name: _new_cursor(scope, cursor) for name, cursor in endpoints.items()
        }
        if scope.execution_attestation_loader is not None:
            scope.manifest_cursors["gate_resolution"] = _new_cursor(
                scope, _Cursor("gate_resolution")
            )
            scope.manifest_cursors["execution_attestation"] = _new_cursor(
                scope, _Cursor("execution_attestation", required=False)
            )
        scope.manifest_created = True
    with scope.lock:
        active_manifest_cursors = {
            name: token
            for name, token in scope.manifest_cursors.items()
            if token in scope.cursors
        }
        scope.exposed_cursors.intersection_update(scope.cursors)
        scope.exposed_cursors.update(active_manifest_cursors.values())
        _fill_exposed_cursor_window(scope)
        current_required = [
            {"cursor": token, "kind": cursor.kind}
            for token, cursor in scope.cursors.items()
            if cursor.required and token in scope.exposed_cursors
        ]
        total_required = sum(
            1 for cursor in scope.cursors.values() if cursor.required
        )
    current_required.sort(
        key=lambda item: (
            item["kind"] == "execution_attestation",
            item["kind"],
            item["cursor"],
        )
    )
    return json.dumps(
        {
            "success": True,
            "tuple": scope.tuple_dict,
            "cursors": active_manifest_cursors,
            "current_required_cursors": {
                "total": total_required,
                "truncated": total_required
                > min(len(current_required), _MAX_RECOVERY_CURSOR_INVENTORY),
                "items": current_required[:_MAX_RECOVERY_CURSOR_INVENTORY],
            },
            "next_parameters": {"operation": "read", "cursor": "opaque cursor"},
            "coverage": _coverage(scope),
        },
        ensure_ascii=False,
    )


def _coverage(scope: EvidenceScope) -> dict[str, Any]:
    with scope.lock:
        required_outstanding = sum(
            1 for cursor in scope.cursors.values() if cursor.required
        )
        optional_available = len(scope.cursors) - required_outstanding
        return {
            "outstanding": required_outstanding,
            "required_outstanding": required_outstanding,
            "optional_available": optional_available,
            "fatal": bool(scope.fatal_error),
            "fatal_error": scope.fatal_error,
            "complete": scope.manifest_created
            and required_outstanding == 0
            and not scope.fatal_error
            and scope.pull_validated
            and scope.expected_changed_files is not None
            and scope.observed_changed_files == scope.expected_changed_files
            and scope.tree_diff_reconciled
            and scope.canonical_files_materialized
            and (
                scope.concise_review
                or (
                    scope.workflow_runs_observed > 0
                    and scope.required_logs_materialized
                    and scope.required_artifact_inventories_materialized
                )
            ),
            "review_attestation": {
                "tree_diff_reconciled": scope.tree_diff_reconciled,
                "canonical_files_materialized": scope.canonical_files_materialized,
                "required_logs_materialized": scope.required_logs_materialized,
                "required_artifact_inventories_materialized": (
                    scope.required_artifact_inventories_materialized
                ),
            },
            "execution_attestation": {
                "complete": scope.execution_attestation_valid,
                "gate_resolution_complete": scope.gate_resolution_valid,
                "policy_version": scope.execution_gate_policy_version,
                "policy_sha256": scope.execution_gate_policy_sha256,
                "resolution_manifest_sha256": scope.gate_resolution_manifest_sha256,
                "required_gates": list(scope.required_execution_gates),
            },
        }


_OMIT_CURSOR = object()


def _fill_exposed_cursor_window(scope: EvidenceScope) -> list[dict[str, str]]:
    """Expose a bounded rolling window of live required cursors.

    Callers must hold ``scope.lock``. Previously exposed live cursors retain
    their place so concurrent tool results cannot collectively fan out beyond
    the configured window.
    """
    scope.exposed_cursors.intersection_update(scope.cursors)
    available = max(0, _MAX_EXPOSED_CURSORS - len(scope.exposed_cursors))
    if not available:
        return []
    candidates = [
        (token, cursor)
        for token, cursor in scope.cursors.items()
        if cursor.required and token not in scope.exposed_cursors
    ]
    candidates.sort(
        key=lambda item: (
            item[1].kind == "execution_attestation",
            item[1].kind,
            item[0],
        )
    )
    exposed = []
    for token, cursor in candidates[:available]:
        scope.exposed_cursors.add(token)
        exposed.append({"cursor": token, "kind": cursor.kind})
    return exposed


def _bound_cursor_exposure(
    scope: EvidenceScope, value: Any
) -> tuple[Any, dict[str, int]]:
    """Hide surplus live cursor tokens from one result without dropping evidence.

    Hidden cursors remain required inside the scope and are revealed through
    the rolling window after earlier cursors are consumed. This bounds the
    aggregate tool output that can enter a single model turn.
    """
    shown: set[str] = set()
    hidden: set[str] = set()

    with scope.lock:
        scope.exposed_cursors.intersection_update(scope.cursors)

        def walk(candidate: Any) -> Any:
            if isinstance(candidate, str) and candidate in scope.cursors:
                if candidate in scope.exposed_cursors:
                    shown.add(candidate)
                    return candidate
                if len(scope.exposed_cursors) < _MAX_EXPOSED_CURSORS:
                    scope.exposed_cursors.add(candidate)
                    shown.add(candidate)
                    return candidate
                hidden.add(candidate)
                return _OMIT_CURSOR
            if isinstance(candidate, list):
                bounded = []
                for item in candidate:
                    result = walk(item)
                    if result is not _OMIT_CURSOR:
                        bounded.append(result)
                return bounded
            if isinstance(candidate, dict):
                bounded = {}
                for key, item in candidate.items():
                    result = walk(item)
                    if result is not _OMIT_CURSOR:
                        bounded[key] = result
                return bounded
            return candidate

        bounded_value = walk(value)
        live_window = len(scope.exposed_cursors)

    return bounded_value, {
        "shown": len(shown),
        "hidden": len(hidden),
        "live_window": live_window,
        "window_limit": _MAX_EXPOSED_CURSORS,
    }


def _validate_pull_request(scope: EvidenceScope, value: Any) -> None:
    if not isinstance(value, dict):
        raise RuntimeError("Pull-request evidence was malformed")
    base = value.get("base")
    head = value.get("head")
    if (
        value.get("number") not in (None, scope.pr_number)
        or not isinstance(base, dict)
        or base.get("sha") != scope.base_sha
        or not isinstance(head, dict)
        or head.get("sha") != scope.head_sha
    ):
        raise RuntimeError("Pull-request evidence did not match the trusted tuple")
    changed_files = value.get("changed_files")
    if isinstance(changed_files, bool) or not isinstance(changed_files, int):
        raise RuntimeError("Pull-request evidence omitted the changed-file count")
    scope.expected_changed_files = changed_files
    scope.pull_validated = True
    if (
        scope.observed_changed_files is not None
        and scope.observed_changed_files != changed_files
    ):
        raise RuntimeError("Changed-file evidence was incomplete")


def _maybe_reconcile_changed_inventories(scope: EvidenceScope) -> None:
    if scope.observed_changed_files is None or not scope.base_tree:
        return
    normalized = set(scope.tree_raw_inventory)
    for status, old_path, new_path in scope.api_changed_inventory:
        if status != "renamed":
            continue
        removed = ("removed", old_path, "")
        added = ("added", "", new_path)
        if removed in normalized and added in normalized:
            normalized.remove(removed)
            normalized.remove(added)
            normalized.add(("renamed", old_path, new_path))
    scope.tree_changed_inventory = normalized
    if scope.api_changed_inventory != scope.tree_changed_inventory:
        raise RuntimeError("GitHub and immutable tree changed-file inventories disagree")
    scope.tree_diff_reconciled = True


def _changed_files(scope: EvidenceScope, items: list[Any]) -> list[Any]:
    scope.observed_changed_files = len(items)
    if (
        scope.expected_changed_files is not None
        and len(items) != scope.expected_changed_files
    ):
        raise RuntimeError("Changed-file evidence was incomplete")
    inventory: set[tuple[str, str, str]] = set()
    bounded_items: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError("Changed-file evidence was malformed")
        filename = item.get("filename")
        if not isinstance(filename, str) or not filename:
            raise RuntimeError("Changed-file evidence omitted a filename")
        status = item.get("status")
        previous = item.get("previous_filename")
        if status == "renamed":
            if not isinstance(previous, str) or not previous:
                raise RuntimeError("Renamed file evidence omitted its previous filename")
            inventory.add(("renamed", previous, filename))
        elif status == "modified":
            inventory.add(("modified", filename, filename))
        elif status == "added":
            inventory.add(("added", "", filename))
        elif status == "removed":
            inventory.add(("removed", filename, ""))
        else:
            raise RuntimeError("Changed-file evidence had an unsupported status")
        if scope.concise_review:
            bounded = dict(item)
            patch = bounded.get("patch")
            if isinstance(patch, str) and len(patch) > _MAX_CONCISE_PATCH_CHARS:
                half = _MAX_CONCISE_PATCH_CHARS // 2
                bounded["patch"] = (
                    patch[:half] + "\n... [patch abbreviated] ...\n" + patch[-half:]
                )
                bounded["patch_truncated"] = True
                bounded["patch_length"] = len(patch)
                bounded["patch_sha256"] = hashlib.sha256(patch.encode()).hexdigest()
            bounded_items.append(bounded)
        else:
            bounded_items.append(item)
    scope.api_changed_inventory = inventory
    _maybe_reconcile_changed_inventories(scope)
    return bounded_items


def _tree_map(value: Any) -> tuple[str, dict[str, dict[str, Any]]]:
    if not isinstance(value, dict) or value.get("truncated") is not False:
        raise RuntimeError("Immutable Git tree evidence was truncated or malformed")
    tree_sha = value.get("sha")
    entries = value.get("tree")
    if not _SHA_RE.fullmatch(str(tree_sha)) or not isinstance(entries, list):
        raise RuntimeError("Immutable Git tree evidence was malformed")
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("Immutable Git tree entry was malformed")
        path = entry.get("path")
        mode = entry.get("mode")
        kind = entry.get("type")
        sha = entry.get("sha")
        if (
            not isinstance(path, str)
            or not path
            or not isinstance(mode, str)
            or kind not in {"blob", "commit", "tree"}
            or not _SHA_RE.fullmatch(str(sha))
        ):
            raise RuntimeError("Immutable Git tree entry was malformed")
        if kind == "commit" or mode == "160000":
            raise RuntimeError("Immutable Git tree contains unsupported submodule gitlink")
        if kind != "tree":
            result[path] = {"path": path, "mode": mode, "type": kind, "sha": sha}
    return str(tree_sha), result


def _comparison_merge_base(scope: EvidenceScope) -> str:
    """Resolve the immutable merge base used by GitHub's pull-request diff."""
    comparison = _run_gh_json(
        [
            f"repos/{scope.repository}/compare/{scope.base_sha}...{scope.head_sha}",
            "--jq",
            "{base_commit:{sha:.base_commit.sha},merge_base_commit:{sha:.merge_base_commit.sha}}",
        ]
    )
    base_commit = comparison.get("base_commit") if isinstance(comparison, dict) else None
    merge_base = (
        comparison.get("merge_base_commit") if isinstance(comparison, dict) else None
    )
    merge_base_sha = merge_base.get("sha") if isinstance(merge_base, dict) else None
    if (
        not isinstance(base_commit, dict)
        or base_commit.get("sha") != scope.base_sha
        or not isinstance(merge_base_sha, str)
        or _SHA_RE.fullmatch(merge_base_sha) is None
    ):
        raise RuntimeError("Pull-request merge-base evidence was malformed")
    return merge_base_sha


def _commit_tree_identity(repository: str, commit_sha: str) -> str:
    commit = _run_gh_json([f"repos/{repository}/git/commits/{commit_sha}"])
    tree = commit.get("tree") if isinstance(commit, dict) else None
    tree_sha = tree.get("sha") if isinstance(tree, dict) else None
    if (
        not isinstance(commit, dict)
        or commit.get("sha") != commit_sha
        or not isinstance(tree_sha, str)
        or _SHA_RE.fullmatch(tree_sha) is None
    ):
        raise RuntimeError("Exact commit tree identity was malformed")
    return tree_sha


def _new_blob_cursor(
    scope: EvidenceScope,
    entry: dict[str, Any],
    *,
    source: str,
    purpose: str,
) -> str:
    sha = entry["sha"]
    existing = scope.blob_tokens_by_sha.get(sha)
    if existing is not None:
        cursor = scope.cursors.get(existing)
        if cursor is not None and isinstance(cursor.data, dict):
            cursor.data["paths"].append({"path": entry["path"], "source": source})
            cursor.data["purposes"].add(purpose)
            if purpose == "canonical":
                scope.canonical_blob_tokens.add(existing)
            return existing
    token = _new_cursor(
        scope,
        _Cursor(
            "blob",
            endpoint=f"repos/{scope.repository}/git/blobs/{sha}",
            data={
                "sha": sha,
                "paths": [{"path": entry["path"], "source": source}],
                "purposes": {purpose},
            },
        ),
    )
    scope.blob_tokens_by_sha[sha] = token
    if purpose == "canonical":
        scope.canonical_blob_tokens.add(token)
    return token


def _is_canonical_gate_path(path: str) -> bool:
    """Select immutable rule files and the complete repository gate roots."""
    name = PurePosixPath(path).name
    lower = name.lower()
    return bool(
        path in _CANONICAL_REVIEW_PATHS
        or path.startswith(".github/workflows/")
        or name in {"package.json", "package-lock.json", "npm-shrinkwrap.json"}
        or lower
        in {
            ".dockerignore",
            ".prettierignore",
            "pnpm-lock.yaml",
            "yarn.lock",
            "compose.yml",
            "compose.yaml",
            "turbo.json",
            "caddyfile",
            "config.alloy",
        }
        or re.fullmatch(r"(?:docker-)?compose(?:\.[a-z0-9_-]+)?\.ya?ml", lower)
        or lower.startswith("docker-compose.")
        or name == "Dockerfile"
        or name.endswith(".Dockerfile")
        or lower.startswith(
            (
                "playwright.",
                "vitest.",
                "vite.",
                "jest.",
                "webpack.",
                "eslint.",
                "prettier.",
                "tsconfig.",
                "next.",
                "postcss.",
                "tailwind.",
            )
        )
    )


def _tree_diff(scope: EvidenceScope) -> dict[str, Any]:
    merge_base_sha = _comparison_merge_base(scope)
    base_tree_sha, base = _tree_map(
        _run_gh_json([f"repos/{scope.repository}/git/trees/{scope.base_sha}?recursive=1"])
    )
    if merge_base_sha == scope.base_sha:
        merge_base_tree_sha = base_tree_sha
        merge_base = base
    else:
        merge_base_tree_sha, merge_base = _tree_map(
            _run_gh_json(
                [f"repos/{scope.repository}/git/trees/{merge_base_sha}?recursive=1"]
            )
        )
    head_tree_sha, head = _tree_map(
        _run_gh_json([f"repos/{scope.repository}/git/trees/{scope.head_sha}?recursive=1"])
    )
    if scope.concise_review:
        expected_base_tree = _commit_tree_identity(scope.repository, scope.base_sha)
        expected_merge_base_tree = expected_base_tree
        if merge_base_sha != scope.base_sha:
            expected_merge_base_tree = _commit_tree_identity(
                scope.repository, merge_base_sha
            )
        expected_head_tree = _commit_tree_identity(scope.repository, scope.head_sha)
        if (
            base_tree_sha != expected_base_tree
            or merge_base_tree_sha != expected_merge_base_tree
            or head_tree_sha != expected_head_tree
        ):
            raise RuntimeError("Immutable tree did not match exact commit identity")
    scope.base_tree_sha = base_tree_sha
    scope.merge_base_sha = merge_base_sha
    scope.merge_base_tree_sha = merge_base_tree_sha
    scope.head_tree_sha = head_tree_sha
    scope.base_tree = base
    scope.merge_base_tree = merge_base
    scope.head_tree = head

    removed = set(merge_base) - set(head)
    added = set(head) - set(merge_base)
    inventory: set[tuple[str, str, str]] = set()
    rename_targets: dict[tuple[str, str, str], list[str]] = {}
    for path in added:
        entry = head[path]
        rename_targets.setdefault((entry["sha"], entry["mode"], entry["type"]), []).append(path)
    for old_path in sorted(tuple(removed)):
        entry = merge_base[old_path]
        candidates = rename_targets.get((entry["sha"], entry["mode"], entry["type"]), [])
        if candidates:
            new_path = sorted(candidates)[0]
            candidates.remove(new_path)
            removed.remove(old_path)
            added.remove(new_path)
            inventory.add(("renamed", old_path, new_path))
    for path in sorted(removed):
        inventory.add(("removed", path, ""))
    for path in sorted(added):
        inventory.add(("added", "", path))
    for path in sorted(set(merge_base) & set(head)):
        if merge_base[path] != head[path]:
            inventory.add(("modified", path, path))
    scope.tree_raw_inventory = inventory
    scope.tree_changed_inventory = set(inventory)
    _maybe_reconcile_changed_inventories(scope)
    inventory = scope.tree_changed_inventory

    changed_paths = {path for _, old, new in inventory for path in (old, new) if path}
    canonical_paths = {
        path
        for path in set(base) | set(head)
        if _is_canonical_gate_path(path)
    }
    essential = {"AGENTS.md", "docs/DEV.md", "docs/TESTING.md", "package.json"}
    if (
        not essential.issubset(head)
        or not any(path.startswith("playwright.config.") for path in head)
        or not any(path.startswith(".github/workflows/") for path in head)
    ):
        raise RuntimeError("Canonical review/gate files were absent from the immutable trees")
    blob_cursors: dict[str, list[str]] = {"changed": [], "canonical": []}
    if scope.concise_review:
        scope.canonical_files_materialized = True
        scope.required_logs_materialized = True
        scope.required_artifact_inventories_materialized = True
    else:
        trees = [("merge_base", merge_base)]
        if merge_base_sha != scope.base_sha:
            trees.append(("base", base))
        trees.append(("head", head))
        for source, tree in trees:
            for path in sorted(changed_paths | canonical_paths):
                entry = tree.get(path)
                if entry is None or entry["type"] != "blob":
                    continue
                if source == "base" and path not in canonical_paths:
                    continue
                purpose = "canonical" if path in canonical_paths else "changed"
                token = _new_blob_cursor(scope, entry, source=source, purpose=purpose)
                if token not in blob_cursors[purpose]:
                    blob_cursors[purpose].append(token)
    return {
        "base_tree_sha": base_tree_sha,
        "merge_base_sha": merge_base_sha,
        "merge_base_tree_sha": merge_base_tree_sha,
        "head_tree_sha": head_tree_sha,
        "changes": [
            {"status": status, "base_path": old, "head_path": new}
            for status, old, new in sorted(inventory)
        ],
        "canonical_paths": sorted(canonical_paths),
        "blob_cursors": blob_cursors,
        "reconciled": scope.tree_diff_reconciled,
    }


def _workflow_runs(scope: EvidenceScope, items: list[Any]) -> list[Any]:
    if not items:
        raise RuntimeError("No exact-head workflow runs were found")
    scope.workflow_runs_observed = len(items)
    result = []
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError("Workflow-run evidence was malformed")
        if item.get("head_sha") != scope.head_sha:
            raise RuntimeError("Workflow-run evidence was not for the trusted head")
        run_id = item.get("id")
        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
            raise RuntimeError("Workflow-run evidence omitted a valid run ID")
        run_attempt = item.get("run_attempt", 1)
        if (
            isinstance(run_attempt, bool)
            or not isinstance(run_attempt, int)
            or run_attempt <= 0
        ):
            raise RuntimeError("Workflow-run evidence omitted a valid attempt")
        if item.get("status") != "completed":
            raise RuntimeError("Exact-head workflow-run evidence was not completed")
        attempts = []
        for attempt in range(1, run_attempt + 1):
            log_cursor = _new_cursor(
                scope,
                _Cursor(
                    "job_log",
                    f"repos/{scope.repository}/actions/runs/{run_id}/attempts/{attempt}/logs",
                    {"path": f"workflow-run-{run_id}-attempt-{attempt}.log"},
                ),
            )
            scope.required_log_tokens.add(log_cursor)
            attempts.append(
                {
                    "attempt": attempt,
                    "jobs": _new_cursor(
                        scope,
                        _Cursor(
                            "jobs",
                            f"repos/{scope.repository}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100",
                            {"run_id": run_id, "attempt": attempt},
                        ),
                    ),
                    "logs": log_cursor,
                }
            )
        child = {
            "attempts": attempts,
            "artifacts": _new_cursor(
                scope,
                _Cursor(
                    "artifacts",
                    f"repos/{scope.repository}/actions/runs/{run_id}/artifacts?per_page=100",
                ),
            ),
        }
        result.append({**item, "evidence_cursors": child})
    return result


def _jobs(
    scope: EvidenceScope, items: list[Any], context: dict[str, Any]
) -> list[Any]:
    if not items:
        raise RuntimeError("Workflow-job evidence was empty")
    result = []
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError("Workflow-job evidence was malformed")
        job_id = item.get("id")
        if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id <= 0:
            raise RuntimeError("Workflow-job evidence omitted a valid job ID")
        if item.get("status") != "completed":
            raise RuntimeError("Workflow-job evidence was not completed")
        run_id = context.get("run_id")
        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
            raise RuntimeError("Workflow-job evidence omitted its trusted run")
        expected_url = (
            f"https://github.com/{scope.repository}/actions/runs/{run_id}/job/{job_id}"
        )
        if item.get("html_url") not in (None, expected_url):
            raise RuntimeError("Workflow-job evidence URL did not match its trusted run")
        scope.observed_action_jobs[job_id] = {
            "run_id": run_id,
            "url": expected_url,
            "conclusion": item.get("conclusion"),
            "log_sha256": "",
        }
        log_cursor = _new_cursor(
            scope,
            _Cursor(
                "job_log",
                f"repos/{scope.repository}/actions/jobs/{job_id}/logs",
                {
                    "path": f"workflow-job-{job_id}.log",
                    "job_id": job_id,
                    "run_id": run_id,
                },
            ),
        )
        scope.required_log_tokens.add(log_cursor)
        result.append({**item, "log_cursor": log_cursor})
    return result


def _artifacts(scope: EvidenceScope, items: list[Any]) -> list[Any]:
    scope.artifacts_discovered = True
    result = []
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError("Workflow-artifact evidence was malformed")
        artifact_id = item.get("id")
        if isinstance(artifact_id, bool) or not isinstance(artifact_id, int) or artifact_id <= 0:
            raise RuntimeError("Workflow artifact omitted a valid ID")
        archive_cursor = _new_cursor(
            scope,
            _Cursor(
                "archive",
                f"repos/{scope.repository}/actions/artifacts/{artifact_id}/zip",
                {"archive_kind": "artifact"},
            ),
        )
        scope.required_artifact_tokens.add(archive_cursor)
        result.append({**item, "archive_cursor": archive_cursor})
    if not scope.required_artifact_tokens:
        scope.required_artifact_inventories_materialized = True
    return result


def _safe_archive(
    scope: EvidenceScope,
    payload: bytes,
    archive_kind: str,
    *,
    entries_required: bool = False,
    log_evidence: bool = False,
) -> dict[str, Any]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise RuntimeError("GitHub evidence archive was not a valid ZIP") from exc
    infos = archive.infolist()
    if len(infos) > _MAX_ARCHIVE_ENTRIES:
        raise RuntimeError("GitHub evidence archive had too many entries")
    total = 0
    entries = []
    for info in infos:
        path = PurePosixPath(info.filename)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise RuntimeError("GitHub evidence archive contained an unsafe path")
        if info.flag_bits & 0x1:
            raise RuntimeError("GitHub evidence archive contained an encrypted entry")
        file_type = (info.external_attr >> 16) & 0o170000
        if file_type == 0o120000:
            raise RuntimeError("GitHub evidence archive contained a symlink")
        if info.is_dir():
            continue
        total += info.file_size
        if total > _MAX_TOTAL_UNCOMPRESSED or info.file_size > _MAX_ENTRY_BYTES:
            raise RuntimeError("GitHub evidence archive exceeded extraction limits")
        if info.compress_size == 0 and info.file_size > 0:
            raise RuntimeError("GitHub evidence archive had an invalid compression ratio")
        if info.compress_size and info.file_size / info.compress_size > _MAX_COMPRESSION_RATIO:
            raise RuntimeError("GitHub evidence archive exceeded the compression-ratio limit")
        content = archive.read(info)
        entry_cursors = [
            _new_cursor(
                scope,
                _Cursor(
                    "archive_entry",
                    data={
                        "path": info.filename,
                        "content": content,
                        "offset": 0,
                        "log_evidence": log_evidence,
                    },
                    required=entries_required,
                ),
            )
        ]
        if log_evidence:
            scope.required_log_tokens.update(entry_cursors)
        entries.append(
            {
                "path": info.filename,
                "size": info.file_size,
                "cursors": entry_cursors,
            }
        )
    return {"archive_kind": archive_kind, "entries": entries}


def _job_log(
    scope: EvidenceScope, payload: bytes, metadata: dict[str, Any]
) -> dict[str, Any]:
    """Expose a plain-text job log or safely unpack a zipped run-log bundle."""
    path = metadata["path"]
    job_id = metadata.get("job_id")
    if job_id is not None:
        observed = scope.observed_action_jobs.get(job_id)
        if observed is None or observed.get("run_id") != metadata.get("run_id"):
            raise RuntimeError("Workflow-job log was not bound to an observed exact-head job")
        observed["log_sha256"] = hashlib.sha256(payload).hexdigest()
    if payload.startswith(b"PK\x03\x04"):
        return _safe_archive(
            scope,
            payload,
            "job_logs",
            entries_required=True,
            log_evidence=True,
        )
    child = _new_cursor(
        scope,
        _Cursor(
            "archive_entry",
            data={
                "path": path,
                "content": payload,
                "offset": 0,
                "log_evidence": True,
            },
        ),
    )
    scope.required_log_tokens.add(child)
    return {
        "archive_kind": "job_logs",
        "entries": [
            {
                "path": path,
                "size": len(payload),
                "cursors": [child],
            }
        ],
    }


def _raw_file(
    scope: EvidenceScope,
    payload: bytes,
    metadata: dict[str, Any],
    consumed_token: str,
) -> dict[str, Any]:
    """Expose immutable file bytes as complete bounded opaque chunks."""
    canonical = "canonical" in metadata["purposes"]
    if canonical:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            text = ""
        raw_references = {
            match.group(1).rstrip(".,;:)]}") for match in _REFERENCED_PATH_RE.finditer(text)
        }
        for path_info in metadata["paths"]:
            tree = scope.base_tree if path_info["source"] == "base" else scope.head_tree
            source_path = PurePosixPath(path_info["path"])
            for reference in sorted(raw_references):
                direct = reference.removeprefix("./")
                relative = posixpath.normpath(str(source_path.parent.joinpath(reference)))
                candidates = (direct, relative)
                entry = next((tree.get(path) for path in candidates if tree.get(path)), None)
                if entry is not None and entry.get("type") == "blob":
                    _new_blob_cursor(
                        scope,
                        entry,
                        source=path_info["source"],
                        purpose="canonical",
                    )
    scope.canonical_blob_tokens.discard(consumed_token)
    child = _new_cursor(
        scope,
        _Cursor(
            "archive_entry",
            data={
                "path": metadata["paths"][0]["path"],
                "content": payload,
                "offset": 0,
                "canonical": canonical,
            },
        ),
    )
    if canonical:
        scope.canonical_blob_tokens.add(child)
    return {
        "sha": metadata["sha"],
        "paths": metadata["paths"],
        "size": len(payload),
        "cursors": [child],
    }


def _archive_entry(
    scope: EvidenceScope,
    value: dict[str, Any],
    consumed_token: str,
    *,
    required: bool,
) -> dict[str, Any]:
    full_content = value["content"]
    offset = value.get("offset", 0)
    if not isinstance(full_content, bytes) or not isinstance(offset, int) or offset < 0:
        raise RuntimeError("GitHub evidence archive entry was malformed")
    parts = max(
        1,
        (len(full_content) + _ARCHIVE_ENTRY_CHUNK_BYTES - 1)
        // _ARCHIVE_ENTRY_CHUNK_BYTES,
    )
    part = offset // _ARCHIVE_ENTRY_CHUNK_BYTES + 1
    content = full_content[offset : offset + _ARCHIVE_ENTRY_CHUNK_BYTES]
    next_offset = offset + len(content)
    next_cursor = None
    canonical = value.get("canonical") is True
    log_evidence = value.get("log_evidence") is True
    if canonical:
        scope.canonical_blob_tokens.discard(consumed_token)
    if log_evidence:
        scope.required_log_tokens.discard(consumed_token)
    if next_offset < len(full_content):
        next_cursor = _new_cursor(
            scope,
            _Cursor(
                "archive_entry",
                data={
                    "path": value["path"],
                    "content": full_content,
                    "offset": next_offset,
                    "canonical": canonical,
                    "log_evidence": log_evidence,
                },
                required=required,
            ),
        )
        if canonical:
            scope.canonical_blob_tokens.add(next_cursor)
        if log_evidence:
            scope.required_log_tokens.add(next_cursor)
    elif canonical and not scope.canonical_blob_tokens:
        scope.canonical_files_materialized = True
    if log_evidence and not scope.required_log_tokens:
        scope.required_logs_materialized = True
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "path": value["path"],
            "part": part,
            "parts": parts,
            "encoding": "base64",
            "content": base64.b64encode(content).decode("ascii"),
            "next_cursor": next_cursor,
        }
    return {
        "path": value["path"],
        "part": part,
        "parts": parts,
        "encoding": "utf-8",
        "content": text,
        "next_cursor": next_cursor,
    }


def _execution_attestation_ready(scope: EvidenceScope, token: str) -> bool:
    """Keep the final attestation retryable until all of its inputs exist."""
    if not scope.gate_resolution_valid:
        return False
    return not any(
        candidate != token and cursor.required
        for candidate, cursor in scope.cursors.items()
    )


def _require_execution_attestation(scope: EvidenceScope) -> None:
    token = scope.manifest_cursors.get("execution_attestation")
    if token is None:
        return
    with scope.lock:
        cursor = scope.cursors.get(token)
        if cursor is not None and not cursor.required:
            scope.cursors[token] = _Cursor(
                cursor.kind,
                endpoint=cursor.endpoint,
                data=cursor.data,
                required=True,
            )


def _read(scope: EvidenceScope, token: str) -> str:
    with scope.lock:
        candidate = scope.cursors.get(token)
        if (
            candidate is not None
            and candidate.kind == "execution_attestation"
            and not _execution_attestation_ready(scope, token)
        ):
            return _error(
                scope,
                "Execution attestation prerequisites are incomplete",
            )
        cursor = scope.cursors.pop(token, None)
        if cursor is not None:
            scope.exposed_cursors.discard(token)
    if cursor is None:
        return _error(scope, "Unknown or already-consumed evidence cursor")
    try:
        if cursor.kind == "blob":
            value = _raw_file(
                scope,
                _run_gh_bytes(
                    cursor.endpoint,
                    accept="application/vnd.github.raw+json",
                ),
                cursor.data,
                token,
            )
        elif cursor.kind == "tree_diff":
            value = _tree_diff(scope)
        elif cursor.kind == "execution_attestation":
            loader = scope.execution_attestation_loader
            if loader is None:
                raise RuntimeError("Execution attestation loader is unavailable")
            payload, signature = loader()
            if not record_execution_attestation(payload, signature):
                raise RuntimeError("Execution attestation was invalid or incomplete")
            value = {
                "attested": True,
                "required_gates": list(scope.required_execution_gates),
                "gates": scope.execution_attestation.get("gates", []),
            }
        elif cursor.kind == "gate_resolution":
            loader = scope.gate_resolution_loader
            if loader is None:
                raise RuntimeError("Execution gate resolver is unavailable")
            payload, signature = loader()
            if not record_gate_resolution(payload, signature):
                raise RuntimeError("Execution gate resolution was invalid or incomplete")
            _require_execution_attestation(scope)
            value = {
                "resolved": True,
                "policy_version": scope.execution_gate_policy_version,
                "policy_sha256": scope.execution_gate_policy_sha256,
                "manifest_sha256": scope.gate_resolution_manifest_sha256,
                "required_gates": list(scope.required_execution_gates),
                "gate_contracts": scope.execution_gate_contracts,
            }
        elif cursor.kind == "archive":
            scope.required_artifact_tokens.discard(token)
            value = _safe_archive(
                scope,
                _run_gh_bytes(cursor.endpoint),
                cursor.data["archive_kind"],
            )
            if scope.artifacts_discovered and not scope.required_artifact_tokens:
                scope.required_artifact_inventories_materialized = True
        elif cursor.kind == "job_log":
            scope.required_log_tokens.discard(token)
            value = _job_log(
                scope,
                _run_gh_bytes(cursor.endpoint),
                cursor.data,
            )
            if not scope.required_log_tokens:
                scope.required_logs_materialized = True
        elif cursor.kind == "archive_entry":
            value = _archive_entry(
                scope, cursor.data, token, required=cursor.required
            )
        elif cursor.kind == "data":
            value = cursor.data
        else:
            if cursor.kind == "closing_issues":
                owner, name = scope.repository.split("/", 1)
                args = [
                    "graphql",
                    "-f",
                    f"query={_CLOSING_ISSUES_QUERY}",
                    "-f",
                    f"owner={owner}",
                    "-f",
                    f"name={name}",
                    "-F",
                    f"number={scope.pr_number}",
                ]
                after = cursor.data.get("after") if isinstance(cursor.data, dict) else None
                if after:
                    args.extend(["-f", f"after={after}"])
            elif cursor.kind in {"pull_request", "blob", "linked_issue"}:
                args = [cursor.endpoint]
            else:
                args = ["--paginate", "--slurp", cursor.endpoint]
            value = _run_gh_json(args)
            if cursor.kind == "pull_request":
                _validate_pull_request(scope, value)
            elif cursor.kind == "closing_issues":
                value = _closing_issues(scope, value)
            elif cursor.kind == "linked_issue":
                linked_repo, linked_number = cursor.data
                comments_cursor = _new_cursor(
                    scope,
                    _Cursor(
                        "linked_issue_comments",
                        f"repos/{linked_repo}/issues/{linked_number}/comments?per_page=100",
                    ),
                )
                value = {**value, "discussion_cursor": comments_cursor}
            elif cursor.kind == "changed_files":
                value = _changed_files(scope, _flatten_pages(value))
            elif cursor.kind == "workflow_runs":
                value = _workflow_runs(
                    scope, _paginated_items(value, "workflow_runs")
                )
            elif cursor.kind == "jobs":
                value = _jobs(scope, _paginated_items(value, "jobs"), cursor.data)
            elif cursor.kind == "artifacts":
                value = _artifacts(scope, _paginated_items(value, "artifacts"))
            elif cursor.kind in {
                "issue_comments",
                "reviews",
                "review_comments",
                "commits",
                "checks",
                "statuses",
                "linked_issue_comments",
            }:
                if cursor.kind == "checks":
                    value = _paginated_items(value, "check_runs")
                elif cursor.kind == "statuses":
                    value = _paginated_items(value, "statuses")
        if cursor.kind not in {"archive_entry", "data"}:
            value = _bound_large_strings(scope, value, required=cursor.required)
        if isinstance(value, list):
            value = _paginate_items(scope, value, required=cursor.required)
        bounded_value, cursor_exposure = _bound_cursor_exposure(scope, value)
        with scope.lock:
            next_required_cursors = _fill_exposed_cursor_window(scope)
            cursor_exposure["live_window"] = len(scope.exposed_cursors)
        response = {
            "success": True,
            "kind": cursor.kind,
            "items": bounded_value,
            "next_required_cursors": next_required_cursors,
            "cursor_exposure": cursor_exposure,
            "coverage": _coverage(scope),
        }
        encoded = json.dumps(response, ensure_ascii=False)
        if len(encoded) > _MAX_RESULT_CHARS:
            raise RuntimeError("Evidence page exceeded the tool result limit")
        return encoded
    except Exception as exc:
        return _error(scope, str(exc), fatal=True)


def github_pr_evidence_tool(operation: str, cursor: Optional[str] = None) -> str:
    """Return manifest metadata or consume one opaque evidence cursor."""
    scope = current_evidence_scope()
    if scope is None:
        return _error(None, "GitHub PR evidence is unavailable in this context")
    if operation == "manifest":
        return _manifest(scope)
    if operation == "read":
        if not isinstance(cursor, str) or not cursor:
            return _error(scope, "An opaque evidence cursor is required")
        return _read(scope, cursor)
    return _error(scope, "Unsupported evidence operation")


def _scope_matches(
    scope: Optional[EvidenceScope],
    contract_version: str,
    repository: str,
    pr_number: int,
    base_sha: str,
    head_sha: str,
) -> bool:
    return bool(
        scope is not None
        and scope.contract_version == contract_version
        and scope.repository == repository
        and scope.pr_number == pr_number
        and scope.base_sha == base_sha
        and scope.head_sha == head_sha
    )


def review_evidence_complete_for(
    contract_version: str,
    repository: str,
    pr_number: int,
    base_sha: str,
    head_sha: str,
) -> bool:
    scope = current_evidence_scope()
    return bool(
        _scope_matches(scope, contract_version, repository, pr_number, base_sha, head_sha)
        and scope is not None
        and _coverage(scope)["complete"]
    )


def execution_evidence_complete_for(
    contract_version: str,
    repository: str,
    pr_number: int,
    base_sha: str,
    head_sha: str,
) -> bool:
    scope = current_evidence_scope()
    return bool(
        _scope_matches(scope, contract_version, repository, pr_number, base_sha, head_sha)
        and scope is not None
        and scope.execution_attestation_valid
    )


def evidence_complete_for(
    contract_version: str,
    repository: str,
    pr_number: int,
    base_sha: str,
    head_sha: str,
) -> bool:
    """Require independent immutable-review and execution attestations."""
    return review_evidence_complete_for(
        contract_version, repository, pr_number, base_sha, head_sha
    ) and execution_evidence_complete_for(
        contract_version, repository, pr_number, base_sha, head_sha
    )


def record_gate_resolution(payload: bytes, signature: str) -> bool:
    """Verify a trusted, tuple-bound resolution of baseline and feature gates."""
    scope = current_evidence_scope()
    if (
        scope is None
        or not isinstance(payload, bytes)
        or not payload
        or len(payload) > 1_000_000
        or not isinstance(signature, str)
        or len(scope.execution_attestation_public_key) != 32
        or not scope.execution_gate_policy_version
        or not re.fullmatch(r"[0-9a-f]{64}", scope.execution_gate_policy_sha256)
        or not scope.baseline_execution_gates
    ):
        return False
    try:
        decoded_signature = base64.b64decode(signature, validate=True)
        Ed25519PublicKey.from_public_bytes(
            scope.execution_attestation_public_key
        ).verify(decoded_signature, payload)
        manifest = json.loads(payload)
    except (InvalidSignature, ValueError, TypeError, json.JSONDecodeError):
        return False
    if not isinstance(manifest, dict):
        return False
    expected = {
        **scope.tuple_dict,
        "policy_version": scope.execution_gate_policy_version,
        "policy_sha256": scope.execution_gate_policy_sha256,
        "baseline_gates": list(scope.baseline_execution_gates),
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        return False
    resolved = manifest.get("resolved_gates")
    contracts = manifest.get("gate_contracts")
    if (
        not isinstance(resolved, list)
        or not resolved
        or len(resolved) != len(set(resolved))
        or any(
            not isinstance(gate, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", gate) is None
            for gate in resolved
        )
        or not set(scope.baseline_execution_gates).issubset(resolved)
        or not isinstance(contracts, dict)
        or set(contracts) != set(resolved)
        or any(not _valid_gate_contract(contract) for contract in contracts.values())
    ):
        return False
    with scope.lock:
        scope.required_execution_gates = tuple(resolved)
        scope.execution_gate_contracts = contracts
        scope.gate_resolution_manifest_sha256 = hashlib.sha256(payload).hexdigest()
        scope.gate_resolution_valid = True
    return True


def _valid_gate_contract(contract: Any) -> bool:
    if not isinstance(contract, dict) or not contract:
        return False
    if contract.get("kind") != "command":
        return True
    command = contract.get("command")
    runner = contract.get("runner")
    exit_codes = contract.get("exit_codes")
    statuses = contract.get("statuses")
    valid_status_contract = (
        contract.get("status") in {"pass", "pr-fail"}
        if statuses is None
        else isinstance(statuses, list)
        and bool(statuses)
        and len(statuses) == len(set(statuses))
        and set(statuses).issubset({"pass", "pr-fail", "unavailable"})
    )
    return bool(
        isinstance(command, list)
        and command
        and all(isinstance(part, str) and part for part in command)
        and contract.get("executor") in {"github_actions", "review_worker"}
        and isinstance(runner, dict)
        and runner.get("kind") == contract.get("executor")
        and isinstance(runner.get("name"), str)
        and runner["name"]
        and valid_status_contract
        and isinstance(exit_codes, list)
        and exit_codes
        and all(isinstance(code, int) and not isinstance(code, bool) for code in exit_codes)
    )


def _valid_gate_evidence(
    scope: EvidenceScope, evidence: Any, status: Any
) -> bool:
    if not isinstance(evidence, dict):
        return False
    digest = evidence.get("log_sha256") or evidence.get("artifact_sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        return False
    if evidence.get("kind") == "github_actions":
        job_id = evidence.get("job_id")
        if isinstance(job_id, bool) or not isinstance(job_id, int):
            return False
        observed = scope.observed_action_jobs.get(job_id)
        return bool(
            observed
            and evidence.get("url") == observed.get("url")
            and digest == observed.get("log_sha256")
            and (
                (status == "pass" and observed.get("conclusion") == "success")
                or (status == "pr-fail" and observed.get("conclusion") == "failure")
            )
        )
    if evidence.get("kind") == "artifact":
        return isinstance(evidence.get("path"), str) and bool(evidence["path"])
    if evidence.get("kind") == "local_worker":
        reason = evidence.get("reason")
        return bool(
            status in {"pass", "pr-fail", "unavailable"}
            and isinstance(reason, str)
            and (status != "unavailable" or bool(reason))
        )
    return False


def _gate_matches_contract(gate: dict[str, Any], contract: dict[str, Any]) -> bool:
    kind = contract.get("kind")
    if kind == "command":
        expected_runner = contract["runner"]
        runner = gate.get("runner")
        expected_statuses = contract.get("statuses")
        status_matches = (
            gate.get("status") == contract.get("status")
            if expected_statuses is None
            else gate.get("status") in expected_statuses
        )
        return bool(
            gate.get("command") == contract.get("command")
            and gate.get("executor") == contract.get("executor")
            and status_matches
            and gate.get("exit_code") in contract.get("exit_codes", [])
            and isinstance(runner, dict)
            and all(runner.get(key) == value for key, value in expected_runner.items())
        )
    if kind == "voice_eval_dry_run":
        plan = gate.get("plan")
        command = gate.get("command")
        return bool(
            gate.get("status") == "pass"
            and gate.get("exit_code") == 0
            and isinstance(command, list)
            and command[-1:] == ["--dry-run"]
            and isinstance(plan, dict)
            and plan.get("cases") == contract.get("cases")
            and isinstance(plan.get("estimated_cost_usd"), (int, float))
            and plan["estimated_cost_usd"] <= contract.get("max_estimated_cost_usd")
            and plan.get("thresholds") == contract.get("thresholds")
        )
    if kind == "voice_eval_paid":
        artifact = gate.get("declared_artifact")
        capability = gate.get("capability")
        result = gate.get("result")
        command = gate.get("command", [])
        if not all(isinstance(value, dict) for value in (artifact, capability, result)):
            return False
        assert isinstance(artifact, dict)
        assert isinstance(capability, dict)
        assert isinstance(result, dict)
        overall_pass = result.get("overall_pass")
        return bool(
            isinstance(overall_pass, bool)
            and gate.get("status") == ("pass" if overall_pass else "pr-fail")
            and isinstance(artifact.get("path"), str)
            and artifact["path"] in command
            and re.fullmatch(r"[0-9a-f]{64}", str(artifact.get("sha256")))
            and command[-1:] == ["--confirm-cost"]
            and capability.get("provider") == result.get("provider")
            and capability.get("single_use") is True
            and capability.get("endpoint_scoped") is True
            and capability.get("allowed_endpoints") == contract.get("allowed_endpoints")
            and isinstance(capability.get("budget_cap_usd"), (int, float))
            and capability["budget_cap_usd"] <= contract.get("max_budget_usd")
            and capability.get("production_credential") is False
            and capability.get("long_lived_credential") is False
            and all(
                isinstance(result.get(key), str) and result[key]
                for key in ("provider", "model", "voice")
            )
            and isinstance(result.get("thresholds"), dict)
            and bool(result["thresholds"])
        )
    if kind == "browser_scenarios":
        return bool(
            gate.get("status") == "pass"
            and gate.get("exit_code") == 0
            and gate.get("scenarios") == contract.get("required_scenarios")
        )
    if kind == "requirement_contradiction":
        return bool(
            gate.get("status") == "pr-fail"
            and gate.get("issue_number") == contract.get("issue_number")
            and gate.get("criterion") == contract.get("criterion")
            and gate.get("checked") is True
            and gate.get("prose_open") is True
            and gate.get("minimum") == contract.get("minimum")
            and isinstance(gate.get("observed"), (int, float))
            and gate["observed"] < gate["minimum"]
            and gate.get("contradiction") is True
        )
    return False


def _valid_gate_timing(gate: dict[str, Any]) -> bool:
    started = gate.get("started_at")
    completed = gate.get("completed_at")
    duration_ms = gate.get("duration_ms")
    if (
        not isinstance(started, str)
        or not isinstance(completed, str)
        or not started.endswith("Z")
        or not completed.endswith("Z")
        or isinstance(duration_ms, bool)
        or not isinstance(duration_ms, int)
        or duration_ms < 0
    ):
        return False
    try:
        started_at = datetime.fromisoformat(started.removesuffix("Z") + "+00:00")
        completed_at = datetime.fromisoformat(completed.removesuffix("Z") + "+00:00")
    except ValueError:
        return False
    elapsed_ms = (completed_at - started_at).total_seconds() * 1000
    return completed_at >= started_at and elapsed_ms == duration_ms


def record_execution_attestation(payload: bytes, signature: str) -> bool:
    """Verify and record a tuple-bound structured worker/gate attestation.

    The signing public key and required gate IDs come only from trusted static
    route state installed on the active scope. Invalid or incomplete reports
    fail closed without replacing a previously valid report.
    """
    scope = current_evidence_scope()
    if (
        scope is None
        or not isinstance(payload, bytes)
        or not payload
        or not isinstance(signature, str)
        or not signature
        or len(scope.execution_attestation_public_key) != 32
        or not scope.gate_resolution_valid
        or not scope.required_execution_gates
        or not _SHA_RE.fullmatch(scope.base_tree_sha)
        or not _SHA_RE.fullmatch(scope.head_tree_sha)
    ):
        return False
    try:
        decoded_signature = base64.b64decode(signature, validate=True)
        Ed25519PublicKey.from_public_bytes(
            scope.execution_attestation_public_key
        ).verify(decoded_signature, payload)
        report = json.loads(payload)
    except (InvalidSignature, ValueError, TypeError, json.JSONDecodeError):
        return False
    if not isinstance(report, dict):
        return False
    expected_tuple = {
        **scope.tuple_dict,
        "base_tree_sha": scope.base_tree_sha,
        "head_tree_sha": scope.head_tree_sha,
    }
    if any(report.get(key) != value for key, value in expected_tuple.items()):
        return False

    gate_resolution = report.get("gate_resolution")
    if gate_resolution != {
        "policy_version": scope.execution_gate_policy_version,
        "policy_sha256": scope.execution_gate_policy_sha256,
        "manifest_sha256": scope.gate_resolution_manifest_sha256,
        "resolved_gates": list(scope.required_execution_gates),
    }:
        return False

    worker = report.get("worker")
    if not isinstance(worker, dict) or not isinstance(worker.get("required"), bool):
        return False
    worker_required = any(
        contract.get("executor") == "review_worker"
        or contract.get("kind") != "command"
        for contract in scope.execution_gate_contracts.values()
    )
    if worker_required and worker.get("required") is not True:
        return False
    if worker.get("required") is False:
        if set(worker) != {"required"}:
            return False
    preflight = worker.get("preflight")
    required_preflight = {
        "disposable_home",
        "credentials_absent",
        "host_mounts_absent",
        "host_docker_socket_absent",
        "resources_bounded",
        "egress_default_deny",
    }
    if worker.get("required") is True:
        if (
            worker.get("head_sha") != scope.head_sha
            or worker.get("base_present") is not True
            or worker.get("tree_before") != scope.head_tree_sha
            or worker.get("tree_after") != scope.head_tree_sha
            or not isinstance(preflight, dict)
            or any(preflight.get(key) is not True for key in required_preflight)
        ):
            return False

    gates = report.get("gates")
    if not isinstance(gates, list):
        return False
    observed: dict[str, dict[str, Any]] = {}
    for gate in gates:
        if not isinstance(gate, dict):
            return False
        gate_id = gate.get("id")
        if not isinstance(gate_id, str) or not gate_id or gate_id in observed:
            return False
        runner = gate.get("runner")
        command = gate.get("command")
        evidence = gate.get("evidence")
        contract = scope.execution_gate_contracts.get(gate_id)
        if (
            not isinstance(contract, dict)
            or gate.get("executor") not in {"github_actions", "review_worker"}
            or gate.get("status") not in {"pass", "pr-fail", "unavailable"}
            or gate.get("head_sha") != scope.head_sha
            or not isinstance(gate.get("attempted"), bool)
            or (
                gate.get("status") in {"pass", "pr-fail"}
                and gate.get("attempted") is not True
            )
            or not isinstance(runner, dict)
            or runner.get("kind") != gate.get("executor")
            or not isinstance(runner.get("name"), str)
            or not runner["name"]
            or not isinstance(command, list)
            or not command
            or any(not isinstance(part, str) or not part for part in command)
            or isinstance(gate.get("exit_code"), bool)
            or not isinstance(gate.get("exit_code"), int)
            or not _valid_gate_timing(gate)
            or gate.get("tree_before") != scope.head_tree_sha
            or gate.get("tree_after") != scope.head_tree_sha
            or not _valid_gate_evidence(scope, evidence, gate.get("status"))
            or not _gate_matches_contract(gate, contract)
        ):
            return False
        observed[gate_id] = gate
    if set(observed) != set(scope.required_execution_gates):
        return False
    declared_artifacts = {
        artifact["path"]
        for gate in observed.values()
        if isinstance((artifact := gate.get("declared_artifact")), dict)
        and isinstance(artifact.get("path"), str)
    }
    mutations = worker.get("mutations", [])
    if (
        not isinstance(mutations, list)
        or any(not isinstance(path, str) or not path for path in mutations)
        or not set(mutations).issubset(declared_artifacts)
    ):
        return False

    with scope.lock:
        scope.execution_attestation = report
        scope.execution_attestation_valid = True
    return True


registry.register(
    name="github_pr_evidence",
    toolset="github_pr_evidence",
    schema={
        "name": "github_pr_evidence",
        "description": (
            "Read complete, immutable GitHub pull-request evidence bound to the "
            "trusted webhook tuple. Consume every required cursor and use optional "
            "cursors only for evidence-driven drill-down. Never issue more than 16 "
            "github_pr_evidence calls in one assistant turn. Continue with cursors "
            "from next_required_cursors; if context loss removes continuation "
            "tokens, call manifest again and resume only from its bounded "
            "current_required_cursors inventory. A recalled manifest omits consumed "
            "control-plane cursors."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["manifest", "read"],
                },
                "cursor": {
                    "type": "string",
                    "description": "Opaque cursor returned by this tool.",
                },
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
    },
    handler=lambda args, **kwargs: github_pr_evidence_tool(
        operation=args.get("operation", ""), cursor=args.get("cursor")
    ),
    check_fn=check_github_pr_evidence_requirements,
    description="Tuple-bound read-only GitHub PR evidence",
    emoji="🔎",
    max_result_size_chars=_MAX_RESULT_CHARS,
)
