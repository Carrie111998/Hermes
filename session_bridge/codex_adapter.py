from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import hashlib
from itertools import islice
import json
import math
from pathlib import Path
import re
import time
from typing import Any, Protocol

from agent.transports.codex_app_server import CodexRequestCancelled
from agent.transports.codex_event_projector import CodexEventProjector

from .models import (
    BridgeMarkerPayload,
    InvalidBridgeMarker,
    OriginKind,
    ProjectedMessage,
    Provider,
    SessionProjection,
    canonical_session_id,
    decode_bridge_marker,
    encode_bridge_marker,
)
from .claude_adapter import (
    AmbiguousPlaceholderCreation,
    PlaceholderCreationError,
    PlaceholderResult,
    _same_filesystem_location,
)
from .sidebar import VerifiedSidebarThread
from .sidebar_placement import (
    filesystem_path_identity,
    placement_paths_equivalent,
)
from .sidebar_reconciliation import (
    SidebarReconciliationEvidence,
    SidebarReconciliationState,
)


_PARSER_VERSION = 1
_REQUEST_TIMEOUT = 30.0
_TARGET_SOURCE_KINDS = ("vscode", "appServer")
_CWD_ALIASES = ("cwd", "workingDirectory", "working_directory")
_CODEX_DELEGATION_PREFIX = "<codex_delegation>"
_SUPPORTED_ITEM_TYPES = frozenset({
    "agentMessage",
    "commandExecution",
    "dynamicToolCall",
    "fileChange",
    "mcpToolCall",
    "reasoning",
    "userMessage",
})
_MARKER_CANDIDATE_RE = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"HERMES_SESSION_BRIDGE_V1:[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"(?![A-Za-z0-9_-])"
)


class _RequestClient(Protocol):
    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
        *,
        cancel_event: Any = None,
    ) -> dict[str, Any]: ...

    def take_notification(self, timeout: float = 0.0) -> dict[str, Any] | None: ...


class _VisibilityInventoryCancelled(RuntimeError):
    """Internal control flow for cancellation of Codex visibility inventory."""


@dataclass(frozen=True)
class CodexThreadSummary:
    native_id: str
    title: str | None
    cwd: str | None
    started_at: float
    last_active: float
    archived: bool
    revision: str
    git_root: str | None = None
    git_branch: str | None = None
    git_head: str | None = None
    worktree_id: str | None = None
    source_kind: str | None = None
    automation_only: bool = False
    subagent_only: bool = False
    preview: str | None = None
    native_path: str | None = None
    thread_source: str | None = None
    trusted_origin_bridge_id: str | None = None
    trusted_origins_checked: bool = field(default=False, compare=False, repr=False)


class CodexInventoryProtocolError(ValueError):
    """A fixed-code failure while reconciling Codex inventory metadata."""

    def __init__(self, code: str, *, field: str | None = None) -> None:
        if code not in {"metadata_conflict"}:
            raise ValueError("Codex inventory protocol error code is not fixed")
        if field not in {None, "source kind"}:
            raise ValueError("Codex inventory protocol error field is not fixed")
        self.code = code
        message = code if field is None else f"{code}: {field}"
        super().__init__(message)


class SidebarVerificationError(RuntimeError):
    """A sanitized native-sidebar lineage verification failure."""

    def __init__(self, code: str) -> None:
        if code not in {
            "bridge_temporarily_unavailable",
            "codex_thread_conflict",
            "inbox_unavailable",
            "marker_conflict",
            "native_task_not_indexed",
            "placement_mismatch",
            "provider_mismatch",
            "source_identity_mismatch",
        }:
            raise ValueError("sidebar verification error code is not fixed")
        self.code = code
        super().__init__(code)


class _ConflictingCodexBridgeMarkers(ValueError):
    def __init__(self, payloads: tuple[BridgeMarkerPayload, ...]) -> None:
        self.payloads = payloads
        super().__init__("Codex thread has conflicting bridge markers")


class _CodexReadBudgetExceeded(RuntimeError):
    pass


class _SidebarReadOnlyInventory(Protocol):
    def list_sidebar_inventory(
        self, *, deadline: float | None, page_cap: int
    ) -> list[CodexThreadSummary]: ...

    def find_sidebar_thread(
        self, thread_id: str, *, deadline: float | None, page_cap: int
    ) -> CodexThreadSummary | None: ...

    def read_sidebar_thread(
        self, summary: CodexThreadSummary, *, deadline: float | None
    ) -> SessionProjection: ...


class SidebarThreadVerifier:
    """Authenticate native Codex sidebar threads through read-only inventory."""

    _ZERO_INTERVAL_READ_BUDGET_SECONDS = 30.0
    _COMPATIBILITY_EVIDENCE_TTL_SECONDS = 30.0

    def __init__(
        self,
        source_adapter: _SidebarReadOnlyInventory,
        *,
        marker_secret: bytes,
        reconciliation_interval: float,
        poll_interval: float = 0.25,
        inventory_page_cap: int = 250,
        inventory_thread_cap: int = 250,
        monotonic=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        if not all(
            callable(getattr(source_adapter, name, None))
            for name in (
                "list_sidebar_inventory",
                "find_sidebar_thread",
                "read_sidebar_thread",
            )
        ):
            raise TypeError("sidebar verifier requires a read-only Codex inventory")
        for label, value in (
            ("reconciliation interval", reconciliation_interval),
            ("poll interval", poll_interval),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < 0
            ):
                raise ValueError(f"{label} must be a non-negative finite number")
        for label, value in (
            ("inventory page cap", inventory_page_cap),
            ("inventory thread cap", inventory_thread_cap),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{label} must be a positive integer")
        if not isinstance(marker_secret, bytes) or not marker_secret:
            raise ValueError("sidebar verifier marker secret is unavailable")
        self._source_adapter = source_adapter
        self._marker_secret = marker_secret
        self._reconciliation_interval = float(reconciliation_interval)
        self._poll_interval = float(poll_interval)
        self._inventory_page_cap = inventory_page_cap
        self._inventory_thread_cap = inventory_thread_cap
        self._monotonic = monotonic
        self._sleep = sleep

    def verify_thread(
        self, *, thread_id: str, expected: BridgeMarkerPayload
    ) -> VerifiedSidebarThread:
        native_id = _required_sidebar_identity(thread_id, "Codex thread ID")
        expected = _validated_sidebar_marker_payload(expected)
        started = self._monotonic()
        polling_enabled = self._reconciliation_interval > 0
        deadline = started + (
            self._reconciliation_interval
            if polling_enabled
            else self._ZERO_INTERVAL_READ_BUDGET_SECONDS
        )
        completed_zero_scan = False
        while True:
            if completed_zero_scan and self._monotonic() >= deadline:
                raise SidebarVerificationError("native_task_not_indexed")
            try:
                summary = self._source_adapter.find_sidebar_thread(
                    native_id,
                    deadline=deadline,
                    page_cap=self._inventory_page_cap,
                )
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                raise SidebarVerificationError(
                    "bridge_temporarily_unavailable"
                ) from None
            if summary is not None:
                try:
                    projection = self._source_adapter.read_sidebar_thread(
                        summary,
                        deadline=deadline,
                    )
                except _ConflictingCodexBridgeMarkers:
                    raise SidebarVerificationError("marker_conflict") from None
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception:
                    raise SidebarVerificationError(
                        "bridge_temporarily_unavailable"
                    ) from None
                if projection.native_id != native_id:
                    raise SidebarVerificationError("source_identity_mismatch")
                verified = _verified_sidebar_projection(
                    projection,
                    expected=expected,
                    marker_secret=self._marker_secret,
                    strict=True,
                )
                assert verified is not None
                return verified
            completed_zero_scan = True
            now = self._monotonic()
            if not polling_enabled or now >= deadline or self._poll_interval == 0:
                raise SidebarVerificationError("native_task_not_indexed")
            self._sleep(min(self._poll_interval, deadline - now))

    def find_by_marker(
        self, expected: BridgeMarkerPayload
    ) -> VerifiedSidebarThread | None:
        return self._find_by_marker_compatibility(expected)

    def find_by_marker_including_archived(
        self,
        expected: BridgeMarkerPayload,
    ) -> VerifiedSidebarThread | None:
        """Find signed marker evidence across active and archived inventory."""

        return self._find_by_marker_compatibility(expected)

    def _find_by_marker_compatibility(
        self,
        expected: BridgeMarkerPayload,
    ) -> VerifiedSidebarThread | None:
        evidence = self.reconcile_marker(
            expected,
            now=time.time(),
            ttl_seconds=self._COMPATIBILITY_EVIDENCE_TTL_SECONDS,
        )
        if evidence.state is SidebarReconciliationState.BLOCKED:
            assert evidence.fixed_reason is not None
            raise SidebarVerificationError(evidence.fixed_reason)
        if evidence.state is SidebarReconciliationState.ABSENCE_PROVEN:
            return None
        assert evidence.recovered_thread_id is not None
        return VerifiedSidebarThread(
            thread_id=evidence.recovered_thread_id,
            source_session_id=expected.source_session_id,
            bridge_id=expected.bridge_id,
        )

    def reconcile_marker(
        self,
        expected: BridgeMarkerPayload,
        *,
        now: float,
        ttl_seconds: float,
    ) -> SidebarReconciliationEvidence:
        """Produce fresh, complete, authenticated native-inventory evidence."""

        expected = _validated_sidebar_marker_payload(expected)
        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(float(now))
        ):
            raise ValueError("sidebar reconciliation time must be finite")
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or not math.isfinite(float(ttl_seconds))
            or float(ttl_seconds) <= 0
        ):
            raise ValueError("sidebar reconciliation TTL must be positive and finite")
        completed_at = float(now)
        expires_at = completed_at + float(ttl_seconds)
        if not math.isfinite(expires_at):
            raise ValueError("sidebar reconciliation expiry must be finite")
        try:
            projections = self._fresh_marker_inventory_projections(expected)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise SidebarVerificationError("bridge_temporarily_unavailable") from None

        marker = encode_bridge_marker(expected, self._marker_secret)
        marker_digest = hashlib.sha256(marker.encode("utf-8")).hexdigest()
        inventory_digest = _sidebar_inventory_digest(projections)
        generation = f"codex:{int(completed_at * 1_000_000)}:{inventory_digest}"
        matches: dict[str, VerifiedSidebarThread] = {}
        fixed_reason: str | None = None
        for projection in projections:
            try:
                verified = _verified_sidebar_projection(
                    projection,
                    expected=expected,
                    marker_secret=self._marker_secret,
                    strict=False,
                )
            except SidebarVerificationError as exc:
                fixed_reason = (
                    exc.code
                    if fixed_reason in {None, exc.code}
                    else "marker_conflict"
                )
                continue
            if verified is not None:
                matches[verified.thread_id] = verified

        if len(matches) > 1:
            fixed_reason = "marker_conflict"
        if fixed_reason is not None:
            return SidebarReconciliationEvidence.create(
                state=SidebarReconciliationState.BLOCKED,
                generation=generation,
                completed_at=completed_at,
                expires_at=expires_at,
                inventory_digest=inventory_digest,
                marker_digest=marker_digest,
                match_count=len(matches),
                recovered_thread_id=None,
                fixed_reason=fixed_reason,
            )
        recovered = next(iter(matches.values()), None)
        return SidebarReconciliationEvidence.create(
            state=(
                SidebarReconciliationState.RECOVERED
                if recovered is not None
                else SidebarReconciliationState.ABSENCE_PROVEN
            ),
            generation=generation,
            completed_at=completed_at,
            expires_at=expires_at,
            inventory_digest=inventory_digest,
            marker_digest=marker_digest,
            match_count=int(recovered is not None),
            recovered_thread_id=(
                recovered.thread_id if recovered is not None else None
            ),
            fixed_reason=None,
        )

    def find_by_recovery_key(
        self,
        recovery_key: str,
        *,
        expected_cwd: str,
        deadline: float,
    ) -> str | None:
        key = _nonempty_string(recovery_key)
        if key is None or key != recovery_key:
            raise ValueError("Codex recovery key is malformed")
        cwd = _nonempty_string(expected_cwd)
        if cwd is None or cwd != expected_cwd:
            raise ValueError("Codex recovery cwd is malformed")
        if filesystem_path_identity(cwd) is None:
            raise ValueError("Codex recovery cwd must be absolute") from None
        try:
            summaries = self._source_adapter.list_sidebar_inventory(
                deadline=deadline,
                page_cap=self._inventory_page_cap,
            )
        except CodexInventoryProtocolError:
            raise SidebarVerificationError("codex_thread_conflict") from None
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise SidebarVerificationError("bridge_temporarily_unavailable") from None
        matches = [summary for summary in summaries if summary.thread_source == key]
        if any(not placement_paths_equivalent(summary.cwd, cwd) for summary in matches):
            raise SidebarVerificationError("codex_thread_conflict")
        native_ids = {summary.native_id for summary in matches}
        if len(native_ids) > 1:
            raise SidebarVerificationError("codex_thread_conflict")
        return next(iter(native_ids), None)

    def _fresh_marker_inventory_projections(
        self,
        expected: BridgeMarkerPayload,
    ) -> tuple[SessionProjection, ...]:
        """Read a complete current inventory without consulting snapshot state."""

        started = self._monotonic()
        deadline = started + (
            self._reconciliation_interval
            if self._reconciliation_interval > 0
            else self._ZERO_INTERVAL_READ_BUDGET_SECONDS
        )
        supports_search = getattr(
            self._source_adapter,
            "supports_sidebar_search",
            None,
        )
        search_inventory = getattr(
            self._source_adapter,
            "search_sidebar_inventory",
            None,
        )
        if (
            callable(supports_search)
            and supports_search()
            and callable(search_inventory)
        ):
            marker_prefix = encode_bridge_marker(
                expected,
                self._marker_secret,
            ).rsplit(".", 1)[0]
            summaries = search_inventory(
                marker_prefix,
                deadline=deadline,
                page_cap=self._inventory_page_cap,
            )
        else:
            summaries = self._source_adapter.list_sidebar_inventory(
                deadline=deadline,
                page_cap=self._inventory_page_cap,
            )
        return self._read_inventory_projections(summaries, deadline=deadline)

    def _read_inventory_projections(
        self,
        summaries: list[CodexThreadSummary],
        *,
        deadline: float,
    ) -> tuple[SessionProjection, ...]:
        if len(summaries) > self._inventory_thread_cap:
            raise _CodexReadBudgetExceeded("Codex sidebar thread cap exceeded")
        projections: list[SessionProjection] = []
        for summary in summaries:
            try:
                projection = self._source_adapter.read_sidebar_thread(
                    summary,
                    deadline=deadline,
                )
            except _ConflictingCodexBridgeMarkers as exc:
                projection = _conflicting_marker_projection(
                    summary,
                    exc.payloads,
                    marker_secret=self._marker_secret,
                )
            projections.append(projection)
        return tuple(projections)


# How long a native_id -> summary inventory index may be reused before refetching.
# A miss always falls back to a live targeted fetch, so this can never cause a
# thread to be reported missing -- it only bounds how stale a cached summary is.
#
# 2026-08-13: started at 60s, which was self-defeating. One full inventory fetch
# pages ~2,700 codex threads over 30s-bounded app-server RPCs and takes MINUTES,
# so a 60s TTL expired mid-backfill and the scan spent most of its time refetching
# the inventory it had just built (observed: 300 threads indexed in the first 19s
# after a restart, then a multi-minute stall re-paging). The TTL must comfortably
# exceed the fetch cost for the index to pay for itself.
_INVENTORY_INDEX_TTL_SECONDS = 900.0


class CodexSourceAdapter:
    def __init__(
        self,
        client: _RequestClient,
        *,
        marker_secret: bytes,
        monotonic=time.monotonic,
        trusted_origins: Mapping[str, str]
        | Callable[[], Mapping[str, str]]
        | None = None,
    ) -> None:
        self._client = client
        self._marker_secret = marker_secret
        self._monotonic = monotonic
        self._initialized = False
        self._initialization_failed = False
        self._experimental_search_enabled = False
        self._seen_inventory: dict[str, CodexThreadSummary] = {}
        self._inventory_cache: dict[str, CodexThreadSummary] = {}
        # TTL'd native_id -> summary index per inventory flavour; see
        # _inventory_index() for why this exists.
        self._inventory_index_cache: dict[
            tuple[bool, tuple[str, ...] | None, bool],
            tuple[float, dict[str, CodexThreadSummary]],
        ] = {}
        if trusted_origins is None:
            self._trusted_origins_resolver: Callable[[], Mapping[str, str]] = dict
        elif isinstance(trusted_origins, Mapping):
            snapshot = dict(trusted_origins)
            self._trusted_origins_resolver = lambda: snapshot
        elif callable(trusted_origins):
            self._trusted_origins_resolver = trusted_origins
        else:
            raise TypeError("Codex trusted origins must be a mapping or callable")

    def stderr_tail(self, n: int = 20) -> list[str]:
        limit = max(0, int(n))
        if limit == 0:
            return []
        method = getattr(self._client, "stderr_tail", None)
        return list(islice(method(limit), limit)) if callable(method) else []

    def _load_trusted_origins(self) -> dict[str, str]:
        value = self._trusted_origins_resolver()
        if not isinstance(value, Mapping):
            raise ValueError("Codex trusted origin resolver returned no mapping")
        origins: dict[str, str] = {}
        for native_id, bridge_id in value.items():
            if (
                not isinstance(native_id, str)
                or not native_id
                or native_id != native_id.strip()
                or not isinstance(bridge_id, str)
                or not bridge_id
                or bridge_id != bridge_id.strip()
            ):
                raise ValueError("Codex trusted origin mapping is malformed")
            origins[native_id] = bridge_id
        return origins

    def _with_trusted_origin(
        self,
        summary: CodexThreadSummary,
        origins: Mapping[str, str],
    ) -> CodexThreadSummary:
        bridge_id = origins.get(summary.native_id)
        if bridge_id == summary.trusted_origin_bridge_id and (
            summary.trusted_origins_checked
        ):
            return summary
        return replace(
            summary,
            trusted_origin_bridge_id=bridge_id,
            trusted_origins_checked=True,
        )

    def _refresh_trusted_origins(
        self, summaries: list[CodexThreadSummary]
    ) -> list[CodexThreadSummary]:
        origins = self._load_trusted_origins()
        return [self._with_trusted_origin(summary, origins) for summary in summaries]

    def _reconcile_trusted_origin(
        self,
        summary: CodexThreadSummary,
        origin_kind: OriginKind,
        origin_bridge_id: str | None,
    ) -> tuple[OriginKind, str | None]:
        trusted_bridge_id = summary.trusted_origin_bridge_id
        if not summary.trusted_origins_checked:
            resolved_bridge_id = self._load_trusted_origins().get(summary.native_id)
            if (
                trusted_bridge_id is not None
                and resolved_bridge_id != trusted_bridge_id
            ):
                raise ValueError("Codex trusted origin mapping conflicts with summary")
            trusted_bridge_id = resolved_bridge_id
        if trusted_bridge_id is None:
            return origin_kind, origin_bridge_id
        if origin_kind is OriginKind.NATIVE:
            return OriginKind.BRIDGE_PLACEHOLDER, trusted_bridge_id
        if origin_bridge_id != trusted_bridge_id:
            raise ValueError("Codex trusted origin conflicts with signed marker")
        return origin_kind, origin_bridge_id

    def supports_sidebar_search(self) -> bool:
        self._ensure_initialized()
        return self._experimental_search_enabled

    def list_sidebar_inventory(
        self, *, deadline: float | None, page_cap: int
    ) -> list[CodexThreadSummary]:
        self._ensure_initialized()
        active, used = self._bounded_sidebar_inventory_kind(
            archived=False,
            deadline=deadline,
            page_cap=page_cap,
        )
        archived, _ = self._bounded_sidebar_inventory_kind(
            archived=True,
            deadline=deadline,
            page_cap=page_cap - used,
        )
        combined: dict[str, CodexThreadSummary] = {}
        for summary in (*active, *archived):
            prior = combined.get(summary.native_id)
            if prior is not None and prior != summary:
                raise CodexInventoryProtocolError("metadata_conflict")
            combined[summary.native_id] = summary
        return [combined[native_id] for native_id in sorted(combined)]

    def search_sidebar_inventory(
        self,
        search_term: str,
        *,
        deadline: float | None,
        page_cap: int,
    ) -> list[CodexThreadSummary]:
        term = _nonempty_string(search_term)
        if term is None or term != search_term:
            raise ValueError("Codex sidebar search term is malformed")
        self._ensure_initialized()
        active, used = self._bounded_sidebar_search_kind(
            search_term=term,
            archived=False,
            deadline=deadline,
            page_cap=page_cap,
        )
        archived, _ = self._bounded_sidebar_search_kind(
            search_term=term,
            archived=True,
            deadline=deadline,
            page_cap=page_cap - used,
        )
        combined: dict[str, CodexThreadSummary] = {}
        for summary in (*active, *archived):
            prior = combined.get(summary.native_id)
            if prior is not None and prior != summary:
                raise CodexInventoryProtocolError("metadata_conflict")
            combined[summary.native_id] = summary
        return [combined[native_id] for native_id in sorted(combined)]

    def find_sidebar_thread(
        self, thread_id: str, *, deadline: float | None, page_cap: int
    ) -> CodexThreadSummary | None:
        wanted = _nonempty_string(thread_id)
        if wanted is None:
            return None
        self._ensure_initialized()
        cached = self._inventory_cache.get(wanted)
        if cached is not None:
            return cached
        try:
            response = self._bounded_sidebar_request(
                "thread/read",
                {"threadId": wanted, "includeTurns": True},
                deadline=deadline,
            )
            thread = _thread_from_response(response)
            summary = _normalize_summary(thread, archived=False)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            summary = None
        if summary is not None and summary.native_id == wanted:
            self._inventory_cache[wanted] = summary
            return summary
        active, used = self._bounded_sidebar_inventory_kind(
            archived=False,
            deadline=deadline,
            page_cap=page_cap,
        )
        found = next(
            (summary for summary in active if summary.native_id == wanted), None
        )
        if found is not None:
            return found
        archived, _ = self._bounded_sidebar_inventory_kind(
            archived=True,
            deadline=deadline,
            page_cap=page_cap - used,
        )
        found = next(
            (summary for summary in archived if summary.native_id == wanted),
            None,
        )
        if found is not None:
            return found
        return None

    def read_sidebar_thread(
        self, summary: CodexThreadSummary, *, deadline: float | None
    ) -> SessionProjection:
        projection, _reconciled_summary = self._read_sidebar_thread_details(
            summary, deadline=deadline
        )
        return projection

    def _read_sidebar_thread_details(
        self,
        summary: CodexThreadSummary,
        *,
        deadline: float | None,
        stop: Any = None,
    ) -> tuple[SessionProjection, CodexThreadSummary]:
        response = self._bounded_sidebar_request(
            "thread/read",
            {"threadId": summary.native_id, "includeTurns": True},
            deadline=deadline,
            stop=stop,
        )
        thread = _thread_from_response(response)
        reconciled = _reconcile_summary_metadata(summary, thread)
        return self.project_thread(reconciled, response=response), reconciled

    def _bounded_sidebar_inventory_kind(
        self,
        *,
        archived: bool,
        deadline: float | None,
        page_cap: int,
    ) -> tuple[list[CodexThreadSummary], int]:
        if type(page_cap) is not int or page_cap <= 0:
            raise _CodexReadBudgetExceeded("Codex sidebar page cap exceeded")
        cursor: Any = None
        seen_cursors: set[str] = set()
        normalized: dict[str, CodexThreadSummary] = {}
        pages = 0
        while True:
            if pages >= page_cap:
                raise _CodexReadBudgetExceeded("Codex sidebar page cap exceeded")
            params: dict[str, Any] = {"archived": archived}
            if cursor is not None:
                params["cursor"] = cursor
            response = self._bounded_sidebar_request(
                "thread/list",
                params,
                deadline=deadline,
            )
            pages += 1
            if not isinstance(response, dict):
                raise ValueError("Codex thread/list response must be an object")
            entries = _first(response, "data", "threads")
            if not isinstance(entries, list):
                raise ValueError("Codex thread/list response has no entries list")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValueError(
                        "Codex thread/list inventory entry must be an object"
                    )
                try:
                    summary = _normalize_summary(entry, archived=archived)
                except CodexInventoryProtocolError:
                    raise
                except (TypeError, ValueError):
                    raise ValueError(
                        "Codex thread/list inventory entry is invalid"
                    ) from None
                prior = normalized.get(summary.native_id)
                if prior is None:
                    normalized[summary.native_id] = summary
                elif prior != summary:
                    raise CodexInventoryProtocolError("metadata_conflict")
            next_cursor = _first(response, "nextCursor", "next_cursor")
            if next_cursor in (None, ""):
                break
            cursor_key = _canonical_json(next_cursor)
            if cursor_key in seen_cursors:
                raise ValueError("Codex thread/list returned a repeated cursor")
            seen_cursors.add(cursor_key)
            cursor = next_cursor
        return [normalized[key] for key in sorted(normalized)], pages

    def _bounded_sidebar_search_kind(
        self,
        *,
        search_term: str,
        archived: bool,
        deadline: float | None,
        page_cap: int,
    ) -> tuple[list[CodexThreadSummary], int]:
        if type(page_cap) is not int or page_cap <= 0:
            raise _CodexReadBudgetExceeded("Codex sidebar page cap exceeded")
        cursor: Any = None
        seen_cursors: set[str] = set()
        normalized: dict[str, CodexThreadSummary] = {}
        pages = 0
        while True:
            if pages >= page_cap:
                raise _CodexReadBudgetExceeded("Codex sidebar page cap exceeded")
            params: dict[str, Any] = {
                "archived": archived,
                "searchTerm": search_term,
            }
            if cursor is not None:
                params["cursor"] = cursor
            response = self._bounded_sidebar_request(
                "thread/search",
                params,
                deadline=deadline,
            )
            pages += 1
            if not isinstance(response, dict):
                raise ValueError("Codex thread/search response must be an object")
            entries = _first(response, "data", "threads")
            if not isinstance(entries, list):
                raise ValueError("Codex thread/search response has no entries list")
            for result in entries:
                if not isinstance(result, dict):
                    raise ValueError(
                        "Codex thread/search result must be an object"
                    )
                entry = result.get("thread")
                if not isinstance(entry, dict):
                    raise ValueError(
                        "Codex thread/search result has no thread object"
                    )
                try:
                    summary = _normalize_summary(entry, archived=archived)
                except CodexInventoryProtocolError:
                    raise
                except (TypeError, ValueError):
                    raise ValueError(
                        "Codex thread/search inventory entry is invalid"
                    ) from None
                prior = normalized.get(summary.native_id)
                if prior is None:
                    normalized[summary.native_id] = summary
                elif prior != summary:
                    raise CodexInventoryProtocolError("metadata_conflict")
            next_cursor = _first(response, "nextCursor", "next_cursor")
            if next_cursor in (None, ""):
                break
            cursor_key = _canonical_json(next_cursor)
            if cursor_key in seen_cursors:
                raise ValueError("Codex thread/search returned a repeated cursor")
            seen_cursors.add(cursor_key)
            cursor = next_cursor
        return [normalized[key] for key in sorted(normalized)], pages

    def _bounded_sidebar_request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        deadline: float | None,
        stop: Any = None,
    ) -> dict[str, Any]:
        timeout = _REQUEST_TIMEOUT
        if deadline is not None:
            remaining = deadline - float(self._monotonic())
            if remaining <= 0:
                raise _CodexReadBudgetExceeded("Codex sidebar deadline exhausted")
            timeout = min(timeout, remaining)
        try:
            if stop is None:
                response = self._client.request(method, params, timeout=timeout)
            else:
                response = self._client.request(
                    method,
                    params,
                    timeout=timeout,
                    cancel_event=stop,
                )
        except CodexRequestCancelled:
            raise _VisibilityInventoryCancelled() from None
        if deadline is not None and float(self._monotonic()) > deadline:
            raise _CodexReadBudgetExceeded("Codex sidebar deadline exhausted")
        return response

    def list_inventory(self, *, archived: bool) -> list[CodexThreadSummary]:
        self._ensure_initialized()
        summaries = self._fetch_inventory(archived=archived)

        changed = [
            summary
            for summary in summaries
            if self._seen_inventory.get(summary.native_id) != summary
        ]
        next_seen = dict(self._seen_inventory)
        next_cache = dict(self._inventory_cache)
        for summary in summaries:
            next_seen[summary.native_id] = summary
            next_cache[summary.native_id] = summary
        self._seen_inventory = next_seen
        self._inventory_cache = next_cache
        return changed

    def list_recent_inventory(
        self,
        *,
        archived: bool,
        after: float,
        known_native_ids: frozenset[str] = frozenset(),
    ) -> list[CodexThreadSummary]:
        """Return recent state-DB summaries without paging full task payloads."""

        cutoff = float(after)
        if not math.isfinite(cutoff):
            raise ValueError("Codex inventory cutoff must be finite")
        self._ensure_initialized()
        summaries = self._fetch_inventory_pages(
            archived=archived,
            source_kinds=None,
            state_db_only=True,
            stop_after=cutoff,
            known_native_ids=known_native_ids,
        )
        summaries = [
            summary for summary in summaries if summary.last_active >= cutoff
        ]
        summaries = self._refresh_trusted_origins(summaries)
        next_cache = dict(self._inventory_cache)
        for summary in summaries:
            next_cache[summary.native_id] = summary
        self._inventory_cache = next_cache
        return summaries

    def list_full_inventory(
        self,
        *,
        archived: bool,
        deadline: float | None = None,
    ) -> list[CodexThreadSummary]:
        """Return every inventory row without applying the changed-thread cache."""

        self._ensure_initialized(deadline=deadline)
        summaries = self._fetch_inventory(archived=archived, deadline=deadline)
        next_cache = dict(self._inventory_cache)
        for summary in summaries:
            next_cache[summary.native_id] = summary
        self._inventory_cache = next_cache
        return summaries

    def _fetch_visibility_full_inventory(
        self, *, archived: bool, deadline: float, stop: Any = None
    ) -> list[CodexThreadSummary]:
        summaries = self._fetch_inventory(
            archived=archived, deadline=deadline, stop=stop
        )
        next_cache = dict(self._inventory_cache)
        for summary in summaries:
            next_cache[summary.native_id] = summary
        self._inventory_cache = next_cache
        return summaries

    def list_claude_visibility_sources(
        self,
        *,
        after: float,
        state_db_only: bool = False,
        indexed_sources: Mapping[str, Any] | None = None,
        known_visibility_source_ids: frozenset[str] = frozenset(),
        discovery_timeout: float = _REQUEST_TIMEOUT,
        stop: Any = None,
    ) -> tuple[Any, ...]:
        """Read active and archived Codex sources with optional indexed reuse."""

        cutoff = float(after)
        if not math.isfinite(cutoff):
            raise ValueError("Codex visibility cutoff must be finite")
        if (
            isinstance(discovery_timeout, bool)
            or not isinstance(discovery_timeout, (int, float))
            or not math.isfinite(float(discovery_timeout))
            or discovery_timeout <= 0
        ):
            raise ValueError("Codex visibility discovery timeout must be positive")
        deadline = self._monotonic() + float(discovery_timeout)
        if stop is not None and stop.is_set():
            raise _VisibilityInventoryCancelled()
        self._ensure_initialized(deadline=deadline, stop=stop)
        combined: dict[str, CodexThreadSummary] = {}
        for archived in (False, True):
            summaries = (
                self._fetch_inventory_pages(
                    archived=archived,
                    source_kinds=None,
                    state_db_only=True,
                    stop_after=cutoff,
                    deadline=deadline,
                    stop=stop,
                )
                if state_db_only
                else self._fetch_visibility_full_inventory(
                    archived=archived, deadline=deadline, stop=stop
                )
            )
            for summary in summaries:
                if summary.last_active < cutoff:
                    continue
                prior = combined.get(summary.native_id)
                if prior is not None and prior != summary:
                    raise ValueError(
                        "Codex thread/list contains conflicting inventory entries"
                    )
                if prior is None:
                    combined[summary.native_id] = summary
        summaries = sorted(
            combined.values(), key=lambda item: (-item.last_active, item.native_id)
        )
        summaries = self._refresh_trusted_origins(summaries)
        from .claude_visibility import evaluate_claude_visibility
        from .store import SidebarSource

        sources: list[SidebarSource] = []
        budget_exhausted = False
        for summary in summaries:
            source_session_id = canonical_session_id(
                Provider.CODEX, summary.native_id
            )
            cached = (
                indexed_sources.get(summary.native_id)
                if indexed_sources is not None
                else None
            )
            if cached is not None:
                cached_projection = cached.projection
                if (
                    cached.source_session_id != source_session_id
                    or cached_projection.provider is not Provider.CODEX
                    or cached_projection.native_id != summary.native_id
                ):
                    raise ValueError("indexed Codex source identity mismatch")
                cache_is_current = (
                    cached_projection.parser_version == _PARSER_VERSION
                    and float(cached_projection.last_active)
                    == float(summary.last_active)
                )
                origin_kind, origin_bridge_id = self._reconcile_trusted_origin(
                    summary,
                    cached_projection.origin_kind,
                    cached_projection.origin_bridge_id,
                )
                cached_projection = replace(
                    cached_projection,
                    title=summary.title
                    if summary.title is not None
                    else cached_projection.title,
                    cwd=summary.cwd
                    if summary.cwd is not None
                    else cached_projection.cwd,
                    started_at=summary.started_at,
                    last_active=summary.last_active,
                    native_path=summary.native_path
                    if summary.native_path is not None
                    else cached_projection.native_path,
                    native_status="archived" if summary.archived else "active",
                    origin_kind=origin_kind,
                    origin_bridge_id=origin_bridge_id,
                    git_branch=summary.git_branch
                    if summary.git_branch is not None
                    else cached_projection.git_branch,
                )
                cached = replace(
                    cached,
                    projection=cached_projection,
                    git_root=summary.git_root
                    if summary.git_root is not None
                    else cached.git_root,
                    git_head=summary.git_head
                    if summary.git_head is not None
                    else cached.git_head,
                    worktree_id=summary.worktree_id
                    if summary.worktree_id is not None
                    else cached.worktree_id,
                    automation_only=summary.automation_only,
                    subagent_only=(
                        summary.subagent_only
                        or _starts_with_codex_delegation(cached_projection)
                    ),
                )
                if cache_is_current and summary.source_kind is not None:
                    exclusion = evaluate_claude_visibility(
                        cached_projection,
                        automation_only=cached.automation_only,
                        subagent_only=cached.subagent_only,
                    )
                    if (
                        exclusion != "eligible"
                        or source_session_id in known_visibility_source_ids
                    ):
                        sources.append(cached)
                        continue
            structurally_excluded = (
                summary.source_kind is not None
                and (
                    summary.automation_only
                    or summary.subagent_only
                    or summary.trusted_origin_bridge_id is not None
                )
            )
            if budget_exhausted or structurally_excluded:
                projection = self._project_state_db_summary(summary)
                reconciled = summary
            else:
                try:
                    projection, reconciled = self._read_sidebar_thread_details(
                        summary, deadline=deadline, stop=stop
                    )
                except _CodexReadBudgetExceeded:
                    if indexed_sources is not None:
                        raise
                    budget_exhausted = True
                    projection = self._project_state_db_summary(summary)
                    reconciled = summary
                except TimeoutError:
                    if indexed_sources is not None:
                        raise
                    if float(self._monotonic()) >= deadline:
                        budget_exhausted = True
                    projection = self._project_state_db_summary(summary)
                    reconciled = summary
            if reconciled.source_kind is None:
                raise ValueError("Codex thread source kind is missing")
            sources.append(
                SidebarSource(
                    source_session_id=source_session_id,
                    projection=projection,
                    git_root=reconciled.git_root,
                    git_head=reconciled.git_head,
                    worktree_id=reconciled.worktree_id,
                    automation_only=reconciled.automation_only,
                    subagent_only=(
                        reconciled.subagent_only
                        or _starts_with_codex_delegation(projection)
                    ),
                )
            )
        return tuple(sources)

    def _project_state_db_summary(
        self, summary: CodexThreadSummary
    ) -> SessionProjection:
        messages: list[ProjectedMessage] = []
        if summary.preview is not None:
            messages.append(
                ProjectedMessage(
                    native_event_id=f"state-db-preview:{summary.native_id}",
                    ordinal=0,
                    role="user",
                    content=summary.preview,
                    timestamp=summary.started_at,
                )
            )
        origin_kind, origin_bridge_id = _detect_origin(
            messages, marker_secret=self._marker_secret
        )
        origin_kind, origin_bridge_id = self._reconcile_trusted_origin(
            summary,
            origin_kind,
            origin_bridge_id,
        )
        return SessionProjection(
            provider=Provider.CODEX,
            native_id=summary.native_id,
            title=summary.title,
            cwd=summary.cwd,
            started_at=summary.started_at,
            last_active=summary.last_active,
            messages=messages,
            native_path=summary.native_path,
            native_status="archived" if summary.archived else "active",
            native_cursor=summary.revision,
            native_hash=_projection_hash(summary, messages),
            parser_version=_PARSER_VERSION,
            origin_kind=origin_kind,
            origin_bridge_id=origin_bridge_id,
            git_branch=summary.git_branch,
        )

    def project_thread(
        self,
        summary: CodexThreadSummary,
        *,
        response: dict[str, Any] | None = None,
    ) -> SessionProjection:
        self._ensure_initialized()
        if response is None:
            try:
                response = self._client.request(
                    "thread/read",
                    {"threadId": summary.native_id, "includeTurns": True},
                    timeout=_REQUEST_TIMEOUT,
                )
            except TimeoutError:
                if summary.preview is None:
                    raise
                return self._project_state_db_summary(summary)
        thread = _thread_from_response(response)
        response_native_id = _nonempty_string(
            _first(thread, "id", "threadId", "thread_id", "sessionId", "session_id")
        )
        if response_native_id is None:
            raise ValueError("Codex thread/read response has no thread identity")
        if response_native_id != summary.native_id:
            raise ValueError("Codex thread/read returned a different thread identity")

        summary_started_at, summary_last_active = _normalized_activity(
            summary.started_at, summary.last_active, context="Codex thread summary"
        )

        projector = CodexEventProjector()
        projected: list[ProjectedMessage] = []
        fallback_occurrences: dict[str, int] = {}
        turns = thread["turns"]
        for turn in turns:
            if not isinstance(turn, dict):
                raise ValueError("Codex thread/read turn must be an object")
            turn_timestamp = _timestamp_from(turn)
            if "items" not in turn:
                raise ValueError("Codex thread/read turn has no items list")
            items = turn["items"]
            if not isinstance(items, list):
                raise ValueError("Codex thread/read turn items must be a list")
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_type = _nonempty_string(item.get("type"))
                if item_type not in _SUPPORTED_ITEM_TYPES:
                    continue
                candidate = deepcopy(projector)
                try:
                    result = candidate.project_item(item)
                except (AttributeError, TypeError, ValueError):
                    continue
                if not _valid_reasoning_item(item):
                    continue
                item_timestamp = _timestamp_from(item)
                timestamp = (
                    item_timestamp
                    if item_timestamp is not None
                    else turn_timestamp
                    if turn_timestamp is not None
                    else summary_started_at
                )
                try:
                    fallback_identity, fallback_digest = _fallback_identity(
                        item,
                        result.messages,
                        timestamp=timestamp,
                        occurrences=fallback_occurrences,
                    )
                    item_messages = _project_messages(
                        item,
                        result.messages,
                        timestamp=timestamp,
                        fallback_identity=fallback_identity,
                    )
                except (TypeError, ValueError):
                    continue
                projector = candidate
                projected.extend(item_messages)
                if fallback_digest is not None:
                    fallback_occurrences[fallback_digest] = (
                        fallback_occurrences.get(fallback_digest, 0) + 1
                    )

        origin_kind, origin_bridge_id = _detect_origin(
            projected, marker_secret=self._marker_secret
        )
        origin_kind, origin_bridge_id = self._reconcile_trusted_origin(
            summary,
            origin_kind,
            origin_bridge_id,
        )
        native_path = _nonempty_string(
            _first(thread, "rolloutPath", "rollout_path")
        ) or _nonempty_string(
            _first(response, "rolloutPath", "rollout_path")
        ) or summary.native_path
        message_timestamps = [message.timestamp for message in projected]
        started_at = min([summary_started_at, *message_timestamps])
        last_active = max([summary_last_active, *message_timestamps])
        normalized_summary = CodexThreadSummary(
            native_id=summary.native_id,
            title=summary.title,
            cwd=summary.cwd,
            started_at=started_at,
            last_active=last_active,
            archived=summary.archived,
            revision=summary.revision,
        )
        native_hash = _projection_hash(normalized_summary, projected)
        return SessionProjection(
            provider=Provider.CODEX,
            native_id=summary.native_id,
            title=summary.title,
            cwd=summary.cwd,
            started_at=started_at,
            last_active=last_active,
            messages=projected,
            native_path=native_path,
            native_status="archived" if summary.archived else "active",
            native_cursor=summary.revision,
            native_hash=native_hash,
            parser_version=_PARSER_VERSION,
            origin_kind=origin_kind,
            origin_bridge_id=origin_bridge_id,
            git_branch=summary.git_branch,
        )

    def projection_has_marker_payload(
        self,
        projection: SessionProjection,
        payload: BridgeMarkerPayload,
    ) -> bool:
        marker = encode_bridge_marker(payload, self._marker_secret)
        return _projection_has_exact_marker(projection, marker=marker)

    def _inventory_index(
        self,
        *,
        archived: bool,
        source_kinds: tuple[str, ...] | None,
        state_db_only: bool,
    ) -> dict[str, CodexThreadSummary] | None:
        """native_id -> summary for one inventory flavour, cached for a TTL.

        Collapses the per-thread full-inventory paging in find_native_thread into
        ONE fetch per TTL. A miss here is never treated as absence: the caller
        falls back to the original targeted fetch, so a thread created inside the
        TTL window is still found. Returns None if the fetch fails, which also
        routes the caller to the original path.
        """
        key = (archived, source_kinds, state_db_only)
        now = time.monotonic()
        entry = self._inventory_index_cache.get(key)
        if entry is not None and (now - entry[0]) < _INVENTORY_INDEX_TTL_SECONDS:
            return entry[1]
        try:
            summaries = self._fetch_inventory(
                archived=archived,
                source_kinds=source_kinds,
                state_db_only=state_db_only,
            )
        except Exception:
            return None
        index = {
            summary.native_id: summary
            for summary in summaries
            if isinstance(getattr(summary, "native_id", None), str)
        }
        self._inventory_index_cache[key] = (now, index)
        return index

    def find_native_thread(
        self,
        native_id: str,
        *,
        source_kinds: tuple[str, ...] | None = None,
        state_db_only: bool = False,
        allow_cached_index: bool = False,
    ) -> CodexThreadSummary | None:
        """Resolve one native thread, searching active inventory then archived.

        ``allow_cached_index`` opts into the TTL'd inventory index, which trades
        bounded staleness for one inventory fetch per TTL instead of one per
        lookup.  Only bulk scan resolution may set it; callers that need
        authoritative state (refresh, characterization) must not, because the
        index only covers ACTIVE threads -- see below.
        """

        if not isinstance(native_id, str) or not native_id.strip():
            return None
        self._ensure_initialized()
        wanted = native_id.strip()

        # 2026-08-13: try the TTL'd index BEFORE paging. This method used to page
        # the whole inventory on every call -- one 30s-bounded app-server RPC per
        # page -- to resolve a single thread, so a scan of N threads cost O(N x
        # pages). py-spy caught the scan parked here (asyncio_13 ->
        # _fetch_inventory_pages:1438 -> _fetch_inventory:1381 -> find_native_thread),
        # and codex sat at indexed_total=1000 / remaining=1722 with no progress and
        # no errors for 20+ minutes; its catalog freshness then aged past the 33s
        # limit and pinned session-bridge-catalog/service/continuity to 'unknown'.
        # Note _inventory_cache was already written here (below) but never read.
        #
        # 2026-08-19: that shortcut was UNCONDITIONAL, which broke this method two
        # ways, so it is now opt-in and never applies to a bounded lookup.
        #  1. The index covers archived=False only, so a thread archived inside the
        #     900s TTL still resolved from the stale ACTIVE index and returned
        #     early -- the archived search never ran and refresh_session reported
        #     native_status='active' for an archived thread.
        #  2. _inventory_index() fetches WITHOUT stop_on_native_id, so it pages the
        #     whole inventory and follows nextCursor. Under state_db_only -- which
        #     exists precisely to be one bounded page -- that both violated the
        #     bound and, on the resulting failure, fell through to fetch the same
        #     inventory a SECOND time.
        if allow_cached_index and not state_db_only:
            indexed = self._inventory_index(
                archived=False, source_kinds=source_kinds, state_db_only=state_db_only
            )
            if indexed is not None:
                hit = indexed.get(wanted)
                if hit is not None:
                    self._inventory_cache[wanted] = hit
                    return hit

        active = self._fetch_inventory(
            archived=False,
            source_kinds=source_kinds,
            state_db_only=state_db_only,
            stop_on_native_id=wanted,
        )
        found = next(
            (summary for summary in active if summary.native_id == wanted), None
        )
        if found is not None:
            self._inventory_cache[wanted] = found
            return found

        archived = self._fetch_inventory(
            archived=True,
            source_kinds=source_kinds,
            state_db_only=state_db_only,
            stop_on_native_id=wanted,
        )
        found = next(
            (summary for summary in archived if summary.native_id == wanted), None
        )
        if found is not None:
            self._inventory_cache[wanted] = found
        return found

    def read_native_thread(self, native_id: str) -> SessionProjection:
        """Read one exact native thread without paging the full Codex inventory."""

        wanted = _nonempty_string(native_id)
        if wanted is None or wanted != native_id:
            raise ValueError("Codex thread ID is malformed")
        self._ensure_initialized()
        response = self._client.request(
            "thread/read",
            {"threadId": wanted, "includeTurns": True},
            timeout=_REQUEST_TIMEOUT,
        )
        thread = _thread_from_response(response)
        observed = _nonempty_string(
            _first(thread, "id", "threadId", "thread_id", "sessionId", "session_id")
        )
        if observed != wanted:
            raise ValueError("Codex thread/read returned a different thread identity")
        try:
            summary = _normalize_summary(thread, archived=False)
        except ValueError as exc:
            if str(exc) != "Codex inventory entry has no valid timestamps":
                raise
            summary = self.find_native_thread(wanted)
            if summary is None:
                raise RuntimeError("Codex thread is unavailable") from exc
        summary = self._refresh_trusted_origins([summary])[0]
        self._inventory_cache[wanted] = summary
        return self.project_thread(summary, response=response)

    def _ensure_initialized(
        self, *, deadline: float | None = None, stop: Any = None
    ) -> None:
        if self._initialization_failed:
            raise RuntimeError(
                "Codex app-server initialization outcome is unknown; replace the "
                "client before retrying"
            )
        if self._initialized:
            return
        if getattr(self._client, "_initialized", False) is True:
            self._experimental_search_enabled = (
                getattr(
                    self._client,
                    "_session_bridge_experimental_api",
                    False,
                )
                is True
            )
            self._initialized = True
            return
        initialize = getattr(self._client, "initialize", None)
        if not callable(initialize):
            self._initialized = True
            return
        try:
            initialize_kwargs: dict[str, Any] = {
                "capabilities": {"experimentalApi": True}
            }
            if deadline is not None:
                remaining = deadline - float(self._monotonic())
                if remaining <= 0:
                    raise _CodexReadBudgetExceeded(
                        "Codex sidebar deadline exhausted"
                    )
                initialize_kwargs["timeout"] = remaining
            if stop is not None:
                initialize_kwargs["cancel_event"] = stop
            initialize(**initialize_kwargs)
            if stop is not None and stop.is_set():
                raise _VisibilityInventoryCancelled()
            if deadline is not None and float(self._monotonic()) > deadline:
                raise _CodexReadBudgetExceeded("Codex sidebar deadline exhausted")
        except CodexRequestCancelled:
            raise _VisibilityInventoryCancelled() from None
        except (_CodexReadBudgetExceeded, _VisibilityInventoryCancelled):
            raise
        except Exception as exc:
            self._initialization_failed = True
            raise RuntimeError(
                "Codex app-server initialization outcome is unknown; replace the "
                "client before retrying"
            ) from exc
        if hasattr(self._client, "_initialized") and not getattr(
            self._client, "_initialized"
        ):
            self._initialization_failed = True
            raise RuntimeError(
                "Codex app-server initialization did not complete; replace the client"
            )
        try:
            setattr(self._client, "_session_bridge_experimental_api", True)
        except (AttributeError, TypeError):
            pass
        self._experimental_search_enabled = True
        self._initialized = True

    def _fetch_inventory(
        self,
        *,
        archived: bool,
        source_kinds: tuple[str, ...] | None = None,
        state_db_only: bool = False,
        stop_on_native_id: str | None = None,
        deadline: float | None = None,
        stop: Any = None,
    ) -> list[CodexThreadSummary]:
        if source_kinds is None:
            summaries = self._fetch_inventory_pages(
                archived=archived,
                source_kinds=None,
                state_db_only=state_db_only,
                stop_on_native_id=stop_on_native_id,
                deadline=deadline,
                stop=stop,
            )
            return self._refresh_trusted_origins(summaries)
        try:
            summaries = self._fetch_inventory_pages(
                archived=archived,
                source_kinds=source_kinds,
                state_db_only=state_db_only,
                stop_on_native_id=stop_on_native_id,
                deadline=deadline,
                stop=stop,
            )
        except Exception as exc:
            retry_without_filter = _is_source_kinds_schema_error(exc)
            failure = exc
        else:
            return self._refresh_trusted_origins(summaries)
        if not retry_without_filter:
            raise failure
        summaries = self._fetch_inventory_pages(
            archived=archived,
            source_kinds=None,
            state_db_only=state_db_only,
            stop_on_native_id=stop_on_native_id,
            deadline=deadline,
            stop=stop,
        )
        return self._refresh_trusted_origins(summaries)

    def _fetch_inventory_pages(
        self,
        *,
        archived: bool,
        source_kinds: tuple[str, ...] | None,
        state_db_only: bool = False,
        stop_after: float | None = None,
        known_native_ids: frozenset[str] = frozenset(),
        stop_on_native_id: str | None = None,
        deadline: float | None = None,
        stop: Any = None,
    ) -> list[CodexThreadSummary]:
        cursor: Any = None
        seen_cursors: set[str] = set()
        normalized: dict[str, CodexThreadSummary] = {}
        raw_entry_count = 0
        trusted_origins = self._load_trusted_origins()
        while True:
            if stop is not None and stop.is_set():
                raise _VisibilityInventoryCancelled()
            params: dict[str, Any] = {"archived": archived}
            if state_db_only:
                params.update({
                    "limit": 100,
                    "sortKey": "updated_at",
                    "sortDirection": "desc",
                    "useStateDbOnly": True,
                })
            if source_kinds is not None:
                params["sourceKinds"] = list(source_kinds)
            if cursor is not None:
                params["cursor"] = cursor
            response = self._bounded_sidebar_request(
                "thread/list", params, deadline=deadline, stop=stop
            )
            if not isinstance(response, dict):
                raise ValueError("Codex thread/list response must be an object")
            entries = _first(response, "data", "threads")
            if entries is None:
                raise ValueError("Codex thread/list response has no entries list")
            if not isinstance(entries, list):
                raise ValueError("Codex thread/list entries must be a list")
            raw_entry_count += len(entries)
            page_native_ids: set[str] = set()
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValueError(
                        "Codex thread/list inventory entry must be an object"
                    )
                try:
                    summary = self._with_trusted_origin(
                        _normalize_summary(entry, archived=archived),
                        trusted_origins,
                    )
                except (TypeError, ValueError):
                    raise ValueError(
                        "Codex thread/list inventory entry is invalid"
                    ) from None
                prior = normalized.get(summary.native_id)
                page_native_ids.add(summary.native_id)
                if prior is None:
                    normalized[summary.native_id] = summary
                elif prior != summary:
                    raise ValueError(
                        "Codex thread/list contains conflicting inventory entries"
                    )

            next_cursor = _first(response, "nextCursor", "next_cursor")
            if stop_on_native_id is not None and stop_on_native_id in page_native_ids:
                break
            if (
                known_native_ids
                and page_native_ids
                and page_native_ids <= known_native_ids
            ):
                break
            if stop_after is not None and any(
                summary.last_active < stop_after for summary in normalized.values()
            ):
                break
            if next_cursor in (None, ""):
                break
            cursor_key = _canonical_json(next_cursor)
            if cursor_key in seen_cursors:
                raise ValueError("Codex thread/list returned a repeated cursor")
            seen_cursors.add(cursor_key)
            cursor = next_cursor
        if raw_entry_count and not normalized:
            raise ValueError("Codex thread/list contained no valid inventory entries")
        return [normalized[native_id] for native_id in sorted(normalized)]


class CodexTargetAdapter:
    def __init__(
        self,
        client: _RequestClient,
        *,
        source_adapter: CodexSourceAdapter,
        marker_secret: bytes,
        clock=time.time,
        monotonic=time.monotonic,
        sleep=time.sleep,
        request_timeout: float = _REQUEST_TIMEOUT,
        require_registration_turn: bool | None = True,
        verification_timeout: float = 60.0,
        verification_poll_interval: float = 0.1,
    ) -> None:
        if require_registration_turn is not None and not isinstance(
            require_registration_turn, bool
        ):
            raise TypeError("require_registration_turn must be boolean or None")
        if (
            isinstance(request_timeout, bool)
            or not isinstance(request_timeout, (int, float))
            or not math.isfinite(float(request_timeout))
            or float(request_timeout) <= 0
        ):
            raise ValueError("request timeout must be a positive finite number")
        for label, value in (
            ("verification timeout", verification_timeout),
            ("verification poll interval", verification_poll_interval),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < 0
            ):
                raise ValueError(f"{label} must be a non-negative finite number")
        self._client = client
        self._source_adapter = source_adapter
        self._marker_secret = marker_secret
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._request_timeout = float(request_timeout)
        self._require_registration_turn = require_registration_turn
        self._verification_timeout = float(verification_timeout)
        self._verification_poll_interval = float(verification_poll_interval)

    def create_placeholder(
        self,
        *,
        title: str,
        source_session_id: str,
        bridge_id: str,
        policy_generation: int,
        cwd: Path | str | None = None,
    ) -> PlaceholderResult:
        title = _target_required_text(title, label="title")
        source_session_id = _target_required_text(
            source_session_id, label="source session ID"
        )
        bridge_id = _target_required_text(bridge_id, label="bridge ID")
        if (
            not isinstance(policy_generation, int)
            or isinstance(policy_generation, bool)
            or policy_generation < 0
        ):
            raise ValueError("policy generation must be a non-negative integer")
        marker = encode_bridge_marker(
            BridgeMarkerPayload(
                bridge_id=bridge_id,
                source_session_id=source_session_id,
                target_provider=Provider.CODEX,
                policy_generation=policy_generation,
            ),
            self._marker_secret,
        )
        hydration = _codex_hydration_contract(
            marker=marker,
            source_session_id=source_session_id,
            bridge_id=bridge_id,
        )
        initialization_failure: PlaceholderCreationError | None = None
        try:
            self._source_adapter._ensure_initialized()
        except Exception:
            initialization_failure = PlaceholderCreationError(
                "codex_initialization_failed"
            )
        if initialization_failure is not None:
            raise initialization_failure
        start_params: dict[str, Any] = {
            "baseInstructions": hydration,
            "developerInstructions": hydration,
            "threadSource": "user",
        }
        existing_cwd = _codex_existing_directory(cwd)
        if existing_cwd is not None:
            start_params["cwd"] = existing_cwd
        start_failure: PlaceholderCreationError | None = None
        try:
            started = self._client.request(
                "thread/start", start_params, timeout=self._request_timeout
            )
        except TimeoutError:
            started = None
            start_failure = AmbiguousPlaceholderCreation("codex_creation_ambiguous")
        except Exception:
            started = None
            start_failure = PlaceholderCreationError("codex_thread_start_failed")
        if start_failure is not None:
            raise start_failure
        assert started is not None
        identity_failure: AmbiguousPlaceholderCreation | None = None
        try:
            native_id = _started_thread_id(started)
        except PlaceholderCreationError as exc:
            native_id = ""
            identity_failure = AmbiguousPlaceholderCreation(exc.code)
        if identity_failure is not None:
            raise identity_failure

        name_ambiguous = False
        name_failure: AmbiguousPlaceholderCreation | None = None
        try:
            self._client.request(
                "thread/name/set",
                {"threadId": native_id, "name": title},
                timeout=self._request_timeout,
            )
        except TimeoutError:
            name_ambiguous = True
        except Exception:
            name_failure = AmbiguousPlaceholderCreation(
                "codex_thread_name_failed", native_id=native_id
            )
        if name_failure is not None:
            raise name_failure

        used_registration_turn = False
        registration_turn_id: str | None = None
        if self._require_registration_turn is True:
            used_registration_turn = True
            registration_turn_id = self._start_registration_turn(
                native_id=native_id, hydration=hydration
            )

        verification_failure: AmbiguousPlaceholderCreation | None = None
        try:
            if self._require_registration_turn is True:
                projection = self._poll_authenticated_projection(
                    native_id=native_id,
                    title=title,
                    cwd=existing_cwd,
                    bridge_id=bridge_id,
                    marker=marker,
                    registration_turn_id=registration_turn_id,
                )
            else:
                try:
                    summary = self._verify_inventory_target(
                        native_id=native_id,
                        title=title,
                        cwd=existing_cwd,
                    )
                except PlaceholderCreationError as exc:
                    if self._require_registration_turn is False:
                        if self._verification_timeout == 0:
                            raise
                        summary = self._poll_inventory_target(
                            native_id=native_id,
                            title=title,
                            cwd=existing_cwd,
                        )
                    elif exc.code == "codex_target_not_found":
                        try:
                            self._verify_exact_thread_read(native_id)
                        except Exception as read_exc:
                            missing_rollout, error_code = (
                                classify_codex_empty_read_error(read_exc, native_id)
                            )
                            if not missing_rollout:
                                raise PlaceholderCreationError(error_code) from read_exc
                        used_registration_turn = True
                        registration_turn_id = self._start_registration_turn(
                            native_id=native_id, hydration=hydration
                        )
                        projection = self._poll_authenticated_projection(
                            native_id=native_id,
                            title=title,
                            cwd=existing_cwd,
                            bridge_id=bridge_id,
                            marker=marker,
                            registration_turn_id=registration_turn_id,
                        )
                    else:
                        raise
                if not used_registration_turn:
                    try:
                        projection = self._source_adapter.project_thread(summary)
                    except TimeoutError as exc:
                        raise AmbiguousPlaceholderCreation(
                            "codex_target_read_ambiguous", native_id=native_id
                        ) from exc
                    except Exception as exc:
                        raise AmbiguousPlaceholderCreation(
                            "codex_target_read_unreadable", native_id=native_id
                        ) from exc
            if projection.native_id != native_id:
                raise PlaceholderCreationError("codex_target_mismatch")
            if not _codex_projection_is_authenticated(
                projection, bridge_id=bridge_id, marker=marker
            ):
                if (
                    _projection_has_authenticated_marker(
                        projection, marker_secret=self._marker_secret
                    )
                    or projection.origin_kind is not OriginKind.NATIVE
                ):
                    raise PlaceholderCreationError("codex_target_marker_mismatch")
                if used_registration_turn:
                    raise PlaceholderCreationError("codex_target_marker_mismatch")
                used_registration_turn = True
                registration_turn_id = self._start_registration_turn(
                    native_id=native_id, hydration=hydration
                )
                projection = self._poll_authenticated_projection(
                    native_id=native_id,
                    title=title,
                    cwd=existing_cwd,
                    bridge_id=bridge_id,
                    marker=marker,
                    registration_turn_id=registration_turn_id,
                )
        except TimeoutError:
            verification_failure = AmbiguousPlaceholderCreation(
                "codex_verification_ambiguous", native_id=native_id
            )
        except PlaceholderCreationError as exc:
            verification_failure = AmbiguousPlaceholderCreation(
                exc.code, native_id=native_id
            )
        except Exception:
            code = (
                "codex_name_outcome_ambiguous"
                if name_ambiguous
                else "codex_target_inventory_unreadable"
            )
            verification_failure = AmbiguousPlaceholderCreation(
                code, native_id=native_id
            )
        if verification_failure is not None:
            raise verification_failure
        return PlaceholderResult(
            native_id=native_id,
            canonical_session_id=canonical_session_id(Provider.CODEX, native_id),
            used_registration_turn=used_registration_turn,
            verified_at=float(self._clock()),
        )

    def _verify_exact_thread_read(self, native_id: str) -> None:
        response = self._client.request(
            "thread/read",
            {"threadId": native_id, "includeTurns": True},
            timeout=self._request_timeout,
        )
        thread = _thread_from_response(response)
        observed = _nonempty_string(
            _first(thread, "id", "threadId", "thread_id", "sessionId", "session_id")
        )
        if observed != native_id:
            raise PlaceholderCreationError("codex_target_mismatch")

    def _start_registration_turn(self, *, native_id: str, hydration: str) -> str:
        registration = (
            "Hermes Session Bridge registration only. "
            "This registration input is metadata, not a substantive user message. "
            "Do not call session_continue or any other tool during this registration "
            "turn. The hydration instruction below applies only to a later substantive "
            f"user message:\n{hydration}\n"
            "Do not perform project work. Reply with exactly READY and nothing else."
        )
        request_failure: AmbiguousPlaceholderCreation | None = None
        try:
            response = self._client.request(
                "turn/start",
                {
                    "threadId": native_id,
                    "input": [{"type": "text", "text": registration}],
                },
                timeout=self._request_timeout,
            )
        except TimeoutError:
            response = None
            request_failure = AmbiguousPlaceholderCreation(
                "codex_registration_turn_ambiguous", native_id=native_id
            )
        except Exception:
            response = None
            request_failure = AmbiguousPlaceholderCreation(
                "codex_registration_turn_failed", native_id=native_id
            )
        if request_failure is not None:
            raise request_failure
        assert response is not None

        identity_failure: AmbiguousPlaceholderCreation | None = None
        try:
            turn_id = _started_turn_id(response)
        except PlaceholderCreationError as exc:
            turn_id = ""
            identity_failure = AmbiguousPlaceholderCreation(
                exc.code, native_id=native_id
            )
        if identity_failure is not None:
            raise identity_failure

        self._wait_for_registration_completion(native_id=native_id, turn_id=turn_id)
        return turn_id

    def _wait_for_registration_completion(
        self, *, native_id: str, turn_id: str
    ) -> None:
        deadline = self._monotonic() + max(
            self._verification_timeout, self._request_timeout
        )
        while True:
            notification_failed = False
            try:
                notification = self._client.take_notification(timeout=0.25)
            except Exception:
                notification = None
                notification_failed = True
            if (
                isinstance(notification, dict)
                and notification.get("method") == "turn/completed"
            ):
                params = notification.get("params")
                turn = params.get("turn") if isinstance(params, dict) else None
                observed_thread_id = (
                    _nonempty_string(params.get("threadId"))
                    if isinstance(params, dict)
                    else None
                )
                observed_turn_id = (
                    _nonempty_string(turn.get("id")) if isinstance(turn, dict) else None
                )
                if observed_thread_id == native_id and observed_turn_id == turn_id:
                    return
            if notification_failed:
                if self._registration_turn_completed_durably(
                    native_id=native_id, turn_id=turn_id
                ):
                    return
                raise AmbiguousPlaceholderCreation(
                    "codex_registration_completion_failed", native_id=native_id
                )
            if self._monotonic() >= deadline:
                if self._registration_turn_completed_durably(
                    native_id=native_id, turn_id=turn_id
                ):
                    return
                raise AmbiguousPlaceholderCreation(
                    "codex_registration_completion_timeout", native_id=native_id
                )

    def _registration_turn_completed_durably(
        self, *, native_id: str, turn_id: str
    ) -> bool:
        try:
            response = self._client.request(
                "thread/read",
                {"threadId": native_id, "includeTurns": True},
                timeout=self._request_timeout,
            )
            thread = _thread_from_response(response)
        except Exception as exc:
            raise AmbiguousPlaceholderCreation(
                "codex_registration_completion_failed", native_id=native_id
            ) from exc
        observed_native_id = _nonempty_string(
            _first(thread, "id", "threadId", "thread_id", "sessionId", "session_id")
        )
        if observed_native_id != native_id:
            raise AmbiguousPlaceholderCreation(
                "codex_target_mismatch", native_id=native_id
            )
        matches = [
            turn
            for turn in thread["turns"]
            if isinstance(turn, dict)
            and _nonempty_string(_first(turn, "id", "turnId", "turn_id")) == turn_id
        ]
        if not matches:
            return False
        if len(matches) != 1:
            raise AmbiguousPlaceholderCreation(
                "codex_registration_turn_conflict", native_id=native_id
            )
        status = _nonempty_string(matches[0].get("status"))
        if status == "completed":
            return True
        if status == "inProgress":
            return False
        raise AmbiguousPlaceholderCreation(
            "codex_registration_turn_not_completed", native_id=native_id
        )

    def _verify_inventory_target(
        self, *, native_id: str, title: str, cwd: str | None
    ) -> CodexThreadSummary:
        """Verify a fresh target from Codex's state database only.

        A normal ``thread/list`` scans and repairs every stored rollout. On a
        large profile that can exceed the bridge's bounded verification window
        even while the target thread is healthy. A just-created target is
        already present in the state database, making it the authoritative
        inventory for this exact, immediate verification.
        """

        summaries = self._source_adapter._fetch_inventory(
            archived=False,
            source_kinds=_TARGET_SOURCE_KINDS,
            state_db_only=True,
            stop_on_native_id=native_id,
        )
        matches = [summary for summary in summaries if summary.native_id == native_id]
        if len(matches) != 1:
            raise PlaceholderCreationError("codex_target_not_found")
        summary = matches[0]
        if summary.title != title:
            raise PlaceholderCreationError("codex_target_title_mismatch")
        if cwd is not None and not _same_filesystem_location(summary.cwd, cwd):
            raise PlaceholderCreationError("codex_target_cwd_mismatch")
        return summary

    def _poll_inventory_target(
        self, *, native_id: str, title: str, cwd: str | None
    ) -> CodexThreadSummary:
        deadline = self._monotonic() + self._verification_timeout
        while True:
            try:
                return self._verify_inventory_target(
                    native_id=native_id, title=title, cwd=cwd
                )
            except PlaceholderCreationError:
                if self._monotonic() >= deadline:
                    raise
                self._sleep(self._verification_poll_interval)

    def _poll_authenticated_projection(
        self,
        *,
        native_id: str,
        title: str,
        cwd: str | None,
        bridge_id: str,
        marker: str,
        registration_turn_id: str | None = None,
    ) -> SessionProjection:
        deadline = self._monotonic() + self._verification_timeout
        while True:
            try:
                summary = self._verify_inventory_target(
                    native_id=native_id, title=title, cwd=cwd
                )
                if registration_turn_id is None:
                    projection = self._source_adapter.project_thread(summary)
                else:
                    projection = self._read_registration_projection(
                        summary,
                        native_id=native_id,
                        turn_id=registration_turn_id,
                        marker=marker,
                    )
            except PlaceholderCreationError as exc:
                if exc.code == "codex_registration_turn_not_completed":
                    raise
                if self._monotonic() >= deadline:
                    raise
                self._sleep(self._verification_poll_interval)
                continue
            except TimeoutError:
                raise
            except Exception as exc:
                if self._monotonic() >= deadline:
                    raise PlaceholderCreationError(
                        "codex_target_read_unreadable"
                    ) from exc
                self._sleep(self._verification_poll_interval)
                continue
            if projection.native_id != native_id:
                raise PlaceholderCreationError("codex_target_mismatch")
            if _codex_projection_is_authenticated(
                projection, bridge_id=bridge_id, marker=marker
            ):
                return projection
            if (
                _projection_has_authenticated_marker(
                    projection, marker_secret=self._marker_secret
                )
                or projection.origin_kind is not OriginKind.NATIVE
            ):
                raise PlaceholderCreationError("codex_target_marker_mismatch")
            if self._monotonic() >= deadline:
                raise PlaceholderCreationError("codex_target_marker_mismatch")
            self._sleep(self._verification_poll_interval)

    def _read_registration_projection(
        self,
        summary: CodexThreadSummary,
        *,
        native_id: str,
        turn_id: str,
        marker: str,
    ) -> SessionProjection:
        response = self._client.request(
            "thread/read",
            {"threadId": native_id, "includeTurns": True},
            timeout=self._request_timeout,
        )
        thread = _thread_from_response(response)
        observed_native_id = _nonempty_string(
            _first(thread, "id", "threadId", "thread_id", "sessionId", "session_id")
        )
        if observed_native_id != native_id:
            raise PlaceholderCreationError("codex_target_mismatch")
        matches = [
            turn
            for turn in thread["turns"]
            if isinstance(turn, dict)
            and _nonempty_string(_first(turn, "id", "turnId", "turn_id")) == turn_id
        ]
        if not matches:
            raise PlaceholderCreationError("codex_registration_turn_not_found")
        if len(matches) != 1:
            raise PlaceholderCreationError("codex_registration_turn_conflict")
        exact_turn = matches[0]
        status = _nonempty_string(exact_turn.get("status"))
        if status == "inProgress":
            raise PlaceholderCreationError("codex_registration_turn_in_progress")
        if status != "completed":
            raise PlaceholderCreationError("codex_registration_turn_not_completed")
        if not _codex_turn_has_exact_marker(exact_turn, marker=marker):
            raise PlaceholderCreationError("codex_target_marker_mismatch")
        return self._source_adapter.project_thread(summary, response=response)


def _codex_hydration_contract(
    *, marker: str, source_session_id: str, bridge_id: str
) -> str:
    return (
        "Hermes Session Bridge placeholder.\n"
        f"Signed marker: {marker}\n"
        f"Canonical source session: {source_session_id}\n"
        "On the first substantive user message, call session_continue with "
        f'bridge ID "{bridge_id}" before answering.'
    )


def _codex_projection_is_authenticated(
    projection: SessionProjection, *, bridge_id: str, marker: str
) -> bool:
    return (
        projection.origin_kind
        in (OriginKind.BRIDGE_PLACEHOLDER, OriginKind.BRIDGE_CONTINUATION)
        and projection.origin_bridge_id == bridge_id
        and _projection_has_exact_marker(projection, marker=marker)
    )


def _projection_has_exact_marker(projection: SessionProjection, *, marker: str) -> bool:
    return any(
        message.role == "user"
        and bool(message.content)
        and any(
            match.group(0) == marker
            for match in _MARKER_CANDIDATE_RE.finditer(message.content or "")
        )
        for message in projection.messages
    )


def _codex_turn_has_exact_marker(turn: dict[str, Any], *, marker: str) -> bool:
    items = turn.get("items")
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "userMessage":
            continue
        content = _message_content(item.get("content"))
        if content is not None and any(
            match.group(0) == marker for match in _MARKER_CANDIDATE_RE.finditer(content)
        ):
            return True
    return False


def _projection_has_authenticated_marker(
    projection: SessionProjection, *, marker_secret: bytes
) -> bool:
    for message in projection.messages:
        if message.role != "user" or not message.content:
            continue
        for match in _MARKER_CANDIDATE_RE.finditer(message.content):
            try:
                decode_bridge_marker(match.group(0), marker_secret)
            except InvalidBridgeMarker:
                continue
            return True
    return False


def _required_sidebar_identity(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise SidebarVerificationError("source_identity_mismatch")
    if any(character in value for character in "\r\n"):
        raise SidebarVerificationError("source_identity_mismatch")
    return value


def _conflicting_marker_projection(
    summary: CodexThreadSummary,
    payloads: tuple[BridgeMarkerPayload, ...],
    *,
    marker_secret: bytes,
) -> SessionProjection:
    content = "\n".join(
        encode_bridge_marker(payload, marker_secret) for payload in payloads
    )
    return SessionProjection(
        provider=Provider.CODEX,
        native_id=summary.native_id,
        title=summary.title,
        cwd=summary.cwd,
        started_at=summary.started_at,
        last_active=summary.last_active,
        messages=(
            ProjectedMessage(
                native_event_id="sidebar-conflicting-markers",
                ordinal=0,
                role="user",
                content=content,
                timestamp=summary.started_at,
            ),
        ),
    )


def _validated_sidebar_marker_payload(
    value: object,
) -> BridgeMarkerPayload:
    if not isinstance(value, BridgeMarkerPayload):
        raise SidebarVerificationError("source_identity_mismatch")
    if value.target_provider is not Provider.CODEX or value.policy_generation != 1:
        raise SidebarVerificationError("provider_mismatch")
    _required_sidebar_identity(value.source_session_id, "source session ID")
    _required_sidebar_identity(value.bridge_id, "bridge ID")
    return value


def _verified_sidebar_projection(
    projection: SessionProjection,
    *,
    expected: BridgeMarkerPayload,
    marker_secret: bytes,
    strict: bool,
) -> VerifiedSidebarThread | None:
    decoded: list[BridgeMarkerPayload] = []
    invalid = False
    invalid_expected = False
    expected_unsigned = encode_bridge_marker(expected, marker_secret).rsplit(".", 1)[0]
    for message in projection.messages:
        if message.role != "user" or not message.content:
            continue
        for match in _MARKER_CANDIDATE_RE.finditer(message.content):
            marker = match.group(0)
            try:
                decoded.append(decode_bridge_marker(marker, marker_secret))
            except InvalidBridgeMarker:
                invalid = True
                invalid_expected = invalid_expected or (
                    marker.rsplit(".", 1)[0] == expected_unsigned
                )
    if strict and invalid:
        raise SidebarVerificationError("marker_conflict")
    if invalid_expected:
        raise SidebarVerificationError("marker_conflict")
    exact = [payload for payload in decoded if payload == expected]
    related = [
        payload
        for payload in decoded
        if payload != expected
        and (
            payload.source_session_id == expected.source_session_id
            or payload.bridge_id == expected.bridge_id
        )
    ]
    if exact:
        if related or (strict and len(exact) != len(decoded)):
            raise SidebarVerificationError("marker_conflict")
        return VerifiedSidebarThread(
            thread_id=projection.native_id,
            source_session_id=expected.source_session_id,
            bridge_id=expected.bridge_id,
            projection=projection,
        )
    if related:
        codes = {
            "provider_mismatch"
            if (
                payload.source_session_id == expected.source_session_id
                and payload.bridge_id == expected.bridge_id
                and (
                    payload.target_provider is not Provider.CODEX
                    or payload.policy_generation != 1
                )
            )
            else "source_identity_mismatch"
            for payload in related
        }
        if len(codes) != 1 or len(related) != 1:
            raise SidebarVerificationError("marker_conflict")
        raise SidebarVerificationError(next(iter(codes)))
    if not strict:
        return None
    if decoded:
        raise SidebarVerificationError("source_identity_mismatch")
    raise SidebarVerificationError("source_identity_mismatch")


def _sidebar_inventory_digest(
    projections: tuple[SessionProjection, ...],
) -> str:
    """Hash only native identity/revision/status and full marker-bearing content."""

    inventory: list[tuple[str, str, str, tuple[str, ...]]] = []
    for projection in projections:
        marker_content = tuple(
            message.content
            for message in projection.messages
            if message.role == "user"
            and isinstance(message.content, str)
            and _MARKER_CANDIDATE_RE.search(message.content) is not None
        )
        inventory.append(
            (
                projection.native_id,
                projection.native_cursor or "",
                projection.native_status,
                marker_content,
            )
        )
    encoded = json.dumps(
        sorted(inventory, key=lambda value: value[0]),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _started_thread_id(response: Any) -> str:
    if not isinstance(response, dict):
        raise PlaceholderCreationError("codex_thread_start_malformed")
    thread = response.get("thread")
    if thread is not None and not isinstance(thread, dict):
        raise PlaceholderCreationError("codex_thread_start_malformed")
    thread = thread or {}
    native_id = _nonempty_string(
        _first(thread, "id", "sessionId", "threadId")
    ) or _nonempty_string(_first(response, "sessionId", "threadId", "id"))
    if native_id is None:
        raise PlaceholderCreationError("codex_thread_start_missing_id")
    return native_id


def _started_turn_id(response: Any) -> str:
    if not isinstance(response, dict):
        raise PlaceholderCreationError("codex_turn_start_malformed")
    turn = response.get("turn")
    if not isinstance(turn, dict):
        raise PlaceholderCreationError("codex_turn_start_malformed")
    turn_id = _nonempty_string(_first(turn, "id", "turnId", "turn_id"))
    if turn_id is None:
        raise PlaceholderCreationError("codex_turn_start_missing_id")
    return turn_id


def _target_required_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must not be empty")
    return value.strip()


def _codex_existing_directory(value: Path | str | None) -> str | None:
    if value is None:
        return None
    try:
        path = Path(value).expanduser()
        if path.is_dir():
            return str(path.resolve())
    except (OSError, TypeError, ValueError):
        pass
    return None


def classify_codex_empty_read_error(exc: Exception, native_id: str) -> tuple[bool, str]:
    message = getattr(exc, "message", None)
    if not isinstance(message, str) or native_id not in message:
        return False, "codex_empty_read_identity_unconfirmed"
    if getattr(exc, "code", None) != -32603:
        return False, "codex_empty_read_rpc_error"
    normalized = message.lower()
    missing_rollout = "rollout" in normalized and any(
        phrase in normalized for phrase in ("failed", "not found", "not persisted")
    )
    if missing_rollout:
        return True, "codex_empty_read_missing_rollout"
    missing_thread = "thread" in normalized and any(
        phrase in normalized for phrase in ("not found", "not persisted")
    )
    if missing_thread:
        return True, "codex_empty_read_missing_thread"
    return False, "codex_empty_read_unexpected"


def _is_source_kinds_schema_error(exc: Exception) -> bool:
    if getattr(exc, "code", None) != -32602:
        return False
    message = getattr(exc, "message", None)
    if not isinstance(message, str):
        return False
    normalized = message.casefold()
    if "sourcekinds" not in normalized:
        return False
    return any(
        phrase in normalized
        for phrase in (
            "invalid param",
            "invalid argument",
            "schema",
            "unknown field",
            "unknown variant",
        )
    )


def _thread_from_response(response: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise ValueError("Codex thread/read response must be an object")
    nested = response.get("thread")
    if nested is not None:
        if not isinstance(nested, dict):
            raise ValueError("Codex thread/read thread must be an object")
        thread = nested
    else:
        thread = response
    if "turns" not in thread or not isinstance(thread["turns"], list):
        raise ValueError("Codex thread/read response must include a turns list")
    return thread


def _starts_with_codex_delegation(projection: SessionProjection) -> bool:
    first_user = next(
        (message for message in projection.messages if message.role == "user"),
        None,
    )
    return bool(
        first_user is not None
        and isinstance(first_user.content, str)
        and first_user.content.startswith(_CODEX_DELEGATION_PREFIX)
    )


def _normalize_summary(entry: dict[str, Any], *, archived: bool) -> CodexThreadSummary:
    native_id = _nonempty_string(
        _first(entry, "id", "threadId", "thread_id", "sessionId", "session_id")
    )
    if native_id is None:
        raise ValueError("Codex inventory entry has no thread ID")

    title = _optional_string(_first(entry, "title", "name", "preview"))
    preview = _optional_string(_first(entry, "preview"))
    native_path = _optional_string(_first(entry, "path", "rolloutPath", "rollout_path"))
    cwd = _cwd_alias_metadata(entry)
    started_at = _inventory_timestamp(
        entry,
        aliases=(
            ("createdAt", False),
            ("created_at", False),
            ("startedAt", False),
            ("started_at", False),
            ("createdAtMs", True),
            ("startedAtMs", True),
        ),
    )
    last_active = _inventory_timestamp(
        entry,
        aliases=(
            ("updatedAt", False),
            ("updated_at", False),
            ("lastActive", False),
            ("last_active", False),
            ("updatedAtMs", True),
            ("lastActiveMs", True),
        ),
    )
    if started_at is None and last_active is None:
        raise ValueError("Codex inventory entry has no valid timestamps")
    if started_at is None:
        started_at = last_active
    if last_active is None:
        last_active = started_at
    assert started_at is not None and last_active is not None
    started_at, last_active = _normalized_activity(
        started_at, last_active, context="Codex inventory"
    )

    archived_value = entry.get("archived", archived)
    if not isinstance(archived_value, bool):
        raise ValueError("Codex inventory archived state must be boolean")
    source_kind, automation_only, subagent_only = _source_kind_metadata(
        entry, required=False
    )
    thread_source = _thread_source_metadata(entry)
    normalized: dict[str, Any] = {
        "native_id": native_id,
        "title": title,
        "cwd": cwd,
        "started_at": started_at,
        "last_active": last_active,
        "archived": archived_value,
        "source_kind": source_kind,
        "thread_source": thread_source,
    }
    revision_value = _first(entry, "revision", "version", "updatedVersion")
    revision = _normalize_revision(revision_value)
    if revision is None:
        revision = hashlib.sha256(
            _canonical_json(normalized).encode("utf-8")
        ).hexdigest()
    return CodexThreadSummary(
        native_id=native_id,
        title=title,
        cwd=cwd,
        started_at=started_at,
        last_active=last_active,
        archived=archived_value,
        revision=revision,
        git_root=_summary_metadata(
            entry,
            ("gitRoot", "git_root", "repositoryRoot", "repository_root"),
        ),
        git_branch=_summary_metadata(
            entry,
            ("gitBranch", "git_branch", "branch"),
            git_aliases=("branch", "gitBranch", "git_branch"),
        ),
        git_head=_summary_metadata(
            entry,
            ("gitHead", "git_head", "head"),
            git_aliases=("sha", "gitHead", "git_head", "head"),
        ),
        worktree_id=_summary_metadata(entry, ("worktreeId", "worktree_id", "worktree")),
        source_kind=source_kind,
        automation_only=automation_only,
        subagent_only=subagent_only,
        preview=preview,
        native_path=native_path,
        thread_source=thread_source,
    )


def _source_kind_metadata(
    entry: dict[str, Any], *, required: bool
) -> tuple[str | None, bool, bool]:
    if "source" not in entry or entry["source"] is None:
        if required or "source" in entry:
            raise ValueError("Codex inventory source kind is missing")
        return None, False, False
    source = entry["source"]
    if isinstance(source, str):
        if source in {"cli", "vscode", "appServer"}:
            return source, False, False
        if source == "exec":
            return source, True, False
        raise ValueError("Codex inventory source kind is unknown")
    if not isinstance(source, dict) or len(source) != 1:
        raise ValueError("Codex inventory source kind is malformed")
    if source.get("custom") == "automation":
        return _canonical_json(source), True, False
    if "subAgent" in source and _valid_subagent_source(source["subAgent"]):
        return _canonical_json(source), False, True
    raise ValueError("Codex inventory source kind is unknown")


def _valid_subagent_source(value: Any) -> bool:
    if isinstance(value, str) and value in {
        "review",
        "compact",
        "memory_consolidation",
    }:
        return True
    if not isinstance(value, dict) or len(value) != 1:
        return False
    other = value.get("other")
    if isinstance(other, str) and bool(other.strip()):
        return True
    spawn = value.get("thread_spawn")
    required_keys = {"depth", "parent_thread_id"}
    optional_keys = {"agent_nickname", "agent_path", "agent_role"}
    if (
        not isinstance(spawn, dict)
        or not required_keys.issubset(spawn)
        or not set(spawn).issubset(required_keys | optional_keys)
    ):
        return False
    depth = spawn["depth"]
    parent = spawn["parent_thread_id"]
    return (
        isinstance(depth, int)
        and not isinstance(depth, bool)
        and depth >= 0
        and isinstance(parent, str)
        and bool(parent.strip())
        and all(
            spawn[key] is None or isinstance(spawn[key], str)
            for key in optional_keys & set(spawn)
        )
    )


def _summary_metadata(
    entry: dict[str, Any],
    aliases: tuple[str, ...],
    *,
    git_aliases: tuple[str, ...] | None = None,
) -> str | None:
    values: list[str] = []
    nested_aliases = aliases if git_aliases is None else git_aliases

    # gitInfo.sha is the canonical Codex commit field. Legacy aliases remain
    # accepted only when they agree with it exactly.
    for git_key in ("gitInfo", "git_info"):
        if git_key not in entry or entry[git_key] is None:
            continue
        git = entry[git_key]
        if not isinstance(git, dict):
            raise ValueError("Codex inventory git metadata must be an object")
        values.extend(_metadata_alias_values(git, nested_aliases))
    values.extend(_metadata_alias_values(entry, aliases))

    if not values:
        return None
    selected = values[0]
    if any(value != selected for value in values[1:]):
        raise CodexInventoryProtocolError("metadata_conflict")
    return selected


def _metadata_alias_values(
    entry: dict[str, Any], aliases: tuple[str, ...]
) -> list[str]:
    values: list[str] = []
    for alias in aliases:
        if alias not in entry or entry[alias] is None:
            continue
        value = _optional_string(entry[alias])
        if value is not None:
            values.append(value)
    return values


def _cwd_alias_metadata(entry: dict[str, Any]) -> str | None:
    values: list[str] = []
    for alias in _CWD_ALIASES:
        if alias not in entry:
            continue
        value = entry[alias]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Codex inventory cwd must be a non-empty string")
        normalized = value.strip()
        try:
            path = Path(normalized).expanduser()
            if not path.is_absolute():
                raise ValueError("Codex inventory cwd must be absolute")
        except (OSError, TypeError, ValueError):
            raise ValueError("Codex inventory cwd must be absolute") from None
        values.append(normalized)
    if not values:
        return None
    selected = values[0]
    if any(
        not _same_filesystem_location(selected, candidate) for candidate in values[1:]
    ):
        raise CodexInventoryProtocolError("metadata_conflict")
    return selected


def _thread_source_metadata(entry: dict[str, Any]) -> str | None:
    values: list[str] = []
    for alias in ("threadSource", "thread_source"):
        if alias not in entry or entry[alias] is None:
            continue
        value = entry[alias]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Codex inventory threadSource must be a non-empty string")
        normalized = value.strip()
        if normalized != value:
            raise ValueError("Codex inventory threadSource must be exact")
        values.append(normalized)
    if not values:
        return None
    selected = values[0]
    if any(value != selected for value in values[1:]):
        raise CodexInventoryProtocolError("metadata_conflict")
    return selected


def _read_summary_metadata(entry: dict[str, Any]) -> dict[str, str | None]:
    return {
        "cwd": _cwd_alias_metadata(entry),
        "thread_source": _thread_source_metadata(entry),
        "git_root": _summary_metadata(
            entry,
            ("gitRoot", "git_root", "repositoryRoot", "repository_root"),
        ),
        "git_branch": _summary_metadata(
            entry,
            ("gitBranch", "git_branch", "branch"),
            git_aliases=("branch", "gitBranch", "git_branch"),
        ),
        "git_head": _summary_metadata(
            entry,
            ("gitHead", "git_head", "head"),
            git_aliases=("sha", "gitHead", "git_head", "head"),
        ),
        "worktree_id": _summary_metadata(
            entry, ("worktreeId", "worktree_id", "worktree")
        ),
    }


def _reconcile_summary_metadata(
    summary: CodexThreadSummary, thread: dict[str, Any]
) -> CodexThreadSummary:
    read_source_kind, read_automation, read_subagent = _source_kind_metadata(
        thread, required=False
    )
    read_metadata = _read_summary_metadata(thread)

    def reconcile(
        left: str | None, right: str | None, *, field: str | None = None
    ) -> str | None:
        if left is not None and right is not None and left != right:
            raise CodexInventoryProtocolError("metadata_conflict", field=field)
        return right if right is not None else left

    source_kind = reconcile(summary.source_kind, read_source_kind, field="source kind")
    read_cwd = read_metadata["cwd"]
    if (
        summary.cwd is not None
        and read_cwd is not None
        and not _same_filesystem_location(summary.cwd, read_cwd)
    ):
        raise CodexInventoryProtocolError("metadata_conflict")
    cwd = read_cwd if read_cwd is not None else summary.cwd
    if read_source_kind is not None:
        automation_only = read_automation
        subagent_only = read_subagent
    else:
        automation_only = summary.automation_only
        subagent_only = summary.subagent_only
    return replace(
        summary,
        cwd=cwd,
        git_root=reconcile(summary.git_root, read_metadata["git_root"]),
        git_branch=reconcile(summary.git_branch, read_metadata["git_branch"]),
        git_head=reconcile(summary.git_head, read_metadata["git_head"]),
        worktree_id=reconcile(summary.worktree_id, read_metadata["worktree_id"]),
        thread_source=reconcile(
            summary.thread_source,
            read_metadata["thread_source"],
        ),
        source_kind=source_kind,
        automation_only=automation_only,
        subagent_only=subagent_only,
    )


def _normalize_revision(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Codex inventory revision must not be boolean")
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            raise ValueError("Codex inventory revision must not be empty")
        return normalized
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return json.dumps(value, allow_nan=False, separators=(",", ":"))
    raise ValueError("Codex inventory revision has an unsupported type")


def _project_messages(
    item: dict[str, Any],
    messages: list[dict],
    *,
    timestamp: float,
    fallback_identity: str | None,
) -> list[ProjectedMessage]:
    item_id = _nonempty_string(item.get("id"))
    base_identity = item_id or fallback_identity
    if base_identity is None:
        return []
    multiple = len(messages) > 1
    projected: list[ProjectedMessage] = []
    for ordinal, message in enumerate(messages):
        native_event_id = f"{base_identity}:{ordinal}" if multiple else base_identity
        tool_calls = message.get("tool_calls")
        tool_name = None
        if isinstance(tool_calls, list) and tool_calls:
            function = tool_calls[0].get("function")
            if isinstance(function, dict):
                tool_name = _nonempty_string(function.get("name"))
        projected.append(
            ProjectedMessage(
                native_event_id=native_event_id,
                ordinal=ordinal if multiple else 0,
                role=str(message.get("role") or "assistant"),
                content=_message_content(message.get("content")),
                timestamp=timestamp,
                tool_name=tool_name,
                tool_calls=deepcopy(tool_calls)
                if isinstance(tool_calls, list)
                else None,
                tool_call_id=_nonempty_string(message.get("tool_call_id")),
                reasoning=_nonempty_string(message.get("reasoning")),
            )
        )
    return projected


def _fallback_identity(
    item: dict[str, Any],
    messages: list[dict],
    *,
    timestamp: float,
    occurrences: dict[str, int],
) -> tuple[str | None, str | None]:
    if _nonempty_string(item.get("id")) is not None or not messages:
        return None, None
    digest = hashlib.sha256(
        _canonical_json({
            "type": item.get("type"),
            "messages": messages,
            "timestamp": timestamp,
        }).encode("utf-8")
    ).hexdigest()
    occurrence = occurrences.get(digest, 0)
    identity = digest if occurrence == 0 else f"{digest}:occ:{occurrence}"
    return identity, digest


def _message_content(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return _canonical_json(value)


def _valid_reasoning_item(item: dict[str, Any]) -> bool:
    if item.get("type") != "reasoning":
        return True
    for key in ("summary", "content"):
        fragments = item.get(key)
        if fragments is None:
            continue
        if not isinstance(fragments, list) or not all(
            isinstance(fragment, str) for fragment in fragments
        ):
            return False
    return True


def _detect_origin(
    messages: list[ProjectedMessage], *, marker_secret: bytes
) -> tuple[OriginKind, str | None]:
    marker_message_indexes: set[int] = set()
    marker_occurrences: list[tuple[int, BridgeMarkerPayload]] = []
    for index, message in enumerate(messages):
        if message.role != "user" or not message.content:
            continue
        for match in _MARKER_CANDIDATE_RE.finditer(message.content):
            try:
                payload = decode_bridge_marker(match.group(0), marker_secret)
            except InvalidBridgeMarker:
                continue
            if payload.target_provider is Provider.CODEX:
                marker_message_indexes.add(index)
                marker_occurrences.append((index, payload))

    marker_ids = {payload.bridge_id for _, payload in marker_occurrences}
    if len(marker_ids) > 1:
        raise _ConflictingCodexBridgeMarkers(
            tuple(payload for _, payload in marker_occurrences)
        )
    if not marker_ids:
        return OriginKind.NATIVE, None

    bridge_id = next(iter(marker_ids))
    first_marker_index = min(index for index, _ in marker_occurrences)
    continued = any(
        index > first_marker_index
        and index not in marker_message_indexes
        and message.role == "user"
        and bool((message.content or "").strip())
        for index, message in enumerate(messages)
    )
    return (
        OriginKind.BRIDGE_CONTINUATION if continued else OriginKind.BRIDGE_PLACEHOLDER,
        bridge_id,
    )


def _projection_hash(
    summary: CodexThreadSummary, messages: list[ProjectedMessage]
) -> str:
    summary_snapshot = asdict(summary)
    summary_snapshot.pop("trusted_origin_bridge_id", None)
    summary_snapshot.pop("trusted_origins_checked", None)
    supported = {
        "summary": summary_snapshot,
        "messages": [asdict(message) for message in messages],
    }
    return hashlib.sha256(_canonical_json(supported).encode("utf-8")).hexdigest()


def _timestamp_from(
    value: dict[str, Any],
    *,
    aliases: tuple[tuple[str, bool], ...] = (
        ("timestamp", False),
        ("createdAt", False),
        ("created_at", False),
        ("completedAt", False),
        ("completed_at", False),
        ("createdAtMs", True),
        ("completedAtMs", True),
    ),
) -> float | None:
    for key, milliseconds in aliases:
        if key not in value:
            continue
        parsed = _parse_timestamp(value[key], milliseconds=milliseconds)
        if parsed is not None:
            return parsed
    return None


def _inventory_timestamp(
    value: dict[str, Any], *, aliases: tuple[tuple[str, bool], ...]
) -> float | None:
    for key, milliseconds in aliases:
        if key not in value:
            continue
        parsed = _parse_timestamp(value[key], milliseconds=milliseconds)
        if parsed is None:
            raise ValueError(f"Codex inventory timestamp {key!r} is invalid")
        return parsed
    return None


def _normalized_activity(
    started_at: Any, last_active: Any, *, context: str
) -> tuple[float, float]:
    if (
        isinstance(started_at, bool)
        or isinstance(last_active, bool)
        or not isinstance(started_at, (int, float))
        or not isinstance(last_active, (int, float))
    ):
        raise ValueError(f"{context} activity timestamps must be numeric")
    normalized_start = float(started_at)
    normalized_last = float(last_active)
    if not math.isfinite(normalized_start) or not math.isfinite(normalized_last):
        raise ValueError(f"{context} activity timestamps must be finite")
    return min(normalized_start, normalized_last), max(
        normalized_start, normalized_last
    )


def _parse_timestamp(value: Any, *, milliseconds: bool) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        if milliseconds:
            parsed /= 1000.0
        return parsed if math.isfinite(parsed) else None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed_datetime = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed_datetime.tzinfo is None:
            parsed_datetime = parsed_datetime.replace(tzinfo=timezone.utc)
        parsed = parsed_datetime.timestamp()
    except (OverflowError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _nonempty_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Codex inventory text field must be a string")
    normalized = value.strip()
    return normalized or None


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
