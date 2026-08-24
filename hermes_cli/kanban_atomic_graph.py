"""Host-owned atomic Kanban graph capability for the weekly Overseer route.

The private plugin owns authentication and transport. This module owns the
closed domain contract, board resolution, preflight, transaction, and durable
receipt. It deliberately has no caller-selectable board or database path.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any, Final, cast

from hermes_cli import kanban_db as kb

CAPABILITY_ID: Final = "hermes.kanban.atomic-graph"
CAPABILITY_VERSION: Final = 1
TARGET_BOARD: Final = "hermes-overseer-hardening"
REQUIRED_PROVIDER: Final = "overseer-weekly-token"
REQUIRED_PRINCIPAL: Final = "overseer-weekly"
REQUIRED_SCOPE: Final = "kanban.atomic-graph.apply"

_OPERATIONS: Final = frozenset(
    {"verify_collector_report", "preflight", "apply_weekly_graphs", "reconcile_weekly_graphs"}
)
_COMMON_FIELDS: Final = {
    "schema_version", "capability", "capability_version", "operation", "board", "request_digest"
}
_OPERATION_FIELDS: Final = {
    "verify_collector_report": {"report", "report_digest"},
    "preflight": {"report_digest", "profiles", "workspace"},
    "apply_weekly_graphs": {"report_digest", "profiles", "workspace", "idempotency_key", "graphs"},
    "reconcile_weekly_graphs": {"idempotency_key", "apply_request_digest"},
}
_GRAPH_FIELDS: Final = {
    "kind", "idempotency_key", "proposal_key", "expected_metric", "evidence",
    "implementation", "qa", "release_note", "release_compatibility", "recurrence_check",
}
_CARD_NAMES: Final = (
    "evidence", "implementation", "qa", "release_note", "release_compatibility", "recurrence_check"
)
_CARD_FIELDS: Final = {"key", "title", "assignee", "parents", "body"}
_BODY_FIELDS: Final = {
    "proposal_key", "failure_scope", "collector_report_digest", "evidence_ids", "expected_metric"
}
_EXPECTED_ASSIGNEES: Final = {
    "evidence": {"developer"},
    "implementation": {"developer", "senior_developer"},
    "qa": {"reviewer_qa"},
    "release_note": {"writer_docs"},
    "release_compatibility": {"developer"},
    "recurrence_check": {"reviewer_qa"},
}
_EXPECTED_METRIC: Final = "reduced recurrence in the next closed weekly window"
_MAX_ENVELOPE_BYTES: Final = 256 * 1024
_MAX_TEXT: Final = 4096
_MAX_CACHE_ENTRIES: Final = 32


class _ContractError(ValueError):
    pass


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _canonical_digest(value: object) -> str:
    return sha256(_canonical_json(value).encode()).hexdigest()


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise _ContractError(f"invalid {field}")
    return value


def _text(value: object, field: str, *, maximum: int = _MAX_TEXT) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise _ContractError(f"invalid {field}")
    return value


def _bounded_strings(
    value: object,
    field: str,
    *,
    maximum_items: int,
    allow_empty: bool = False,
) -> list[str]:
    if (
        not isinstance(value, list)
        or (not allow_empty and not value)
        or len(value) > maximum_items
        or any(not isinstance(item, str) or not item or len(item) > _MAX_TEXT for item in value)
        or len(set(value)) != len(value)
    ):
        raise _ContractError(f"invalid {field}")
    return value


def _profiles(value: object) -> list[str]:
    profiles = _bounded_strings(value, "profiles", maximum_items=16)
    if profiles != sorted(profiles) or any(len(profile) > 64 for profile in profiles):
        raise _ContractError("invalid profiles")
    return profiles


def _card(
    value: object,
    name: str,
    *,
    report_digest: str,
    proposal_key: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _ContractError("invalid card")
    expected_fields = _CARD_FIELDS | ({"window_start"} if name == "recurrence_check" else set())
    if set(value) != expected_fields:
        raise _ContractError("invalid card fields")
    _text(value.get("key"), f"{name}.key")
    _text(value.get("title"), f"{name}.title")
    assignee = _text(value.get("assignee"), f"{name}.assignee", maximum=64)
    if assignee not in _EXPECTED_ASSIGNEES[name]:
        raise _ContractError("invalid assignee")
    _bounded_strings(value.get("parents"), f"{name}.parents", maximum_items=2, allow_empty=True)
    body = value.get("body")
    if not isinstance(body, Mapping):
        raise _ContractError("invalid body")
    body_fields = set(_BODY_FIELDS)
    if name == "implementation":
        body_fields |= {"routing_reason", "developer_failure_count"}
    if name == "recurrence_check":
        body_fields.add("window_start")
    if set(body) != body_fields:
        raise _ContractError("invalid body fields")
    if _text(body.get("proposal_key"), "body.proposal_key") != proposal_key:
        raise _ContractError("mismatched proposal")
    _text(body.get("failure_scope"), "body.failure_scope")
    if _digest(body.get("collector_report_digest"), "collector_report_digest") != report_digest:
        raise _ContractError("mismatched report")
    _bounded_strings(body.get("evidence_ids"), "body.evidence_ids", maximum_items=100)
    if body.get("expected_metric") != _EXPECTED_METRIC:
        raise _ContractError("invalid metric")
    if name == "implementation":
        _text(body.get("routing_reason"), "routing_reason", maximum=256)
        failure_count = body.get("developer_failure_count")
        if type(failure_count) is not int or not 0 <= failure_count <= 1_000_000:
            raise _ContractError("invalid failure count")
    if name == "recurrence_check":
        window_start = _text(value.get("window_start"), "window_start", maximum=32)
        if body.get("window_start") != window_start:
            raise _ContractError("mismatched window")
    return value


def _graphs(value: object, *, report_digest: str, profiles: list[str]) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 3:
        raise _ContractError("invalid graphs")
    graphs: list[Mapping[str, object]] = []
    bundle_keys: set[str] = set()
    proposal_keys: set[str] = set()
    task_keys: set[str] = set()
    for graph in value:
        if not isinstance(graph, Mapping) or set(graph) != _GRAPH_FIELDS or graph.get("kind") != "improvement":
            raise _ContractError("invalid graph")
        bundle_key = _text(graph.get("idempotency_key"), "graph.idempotency_key")
        proposal_key = _text(graph.get("proposal_key"), "graph.proposal_key")
        if not bundle_key.startswith("bundle:weekly:") or bundle_key in bundle_keys or proposal_key in proposal_keys:
            raise _ContractError("duplicate graph identity")
        bundle_keys.add(bundle_key)
        proposal_keys.add(proposal_key)
        if graph.get("expected_metric") != _EXPECTED_METRIC:
            raise _ContractError("invalid graph metric")
        cards: dict[str, Mapping[str, object]] = {}
        for name in _CARD_NAMES:
            card = _card(graph.get(name), name, report_digest=report_digest, proposal_key=proposal_key)
            task_key = str(card["key"])
            if task_key in task_keys:
                raise _ContractError("duplicate task key")
            task_keys.add(task_key)
            if str(card["assignee"]) not in profiles:
                raise _ContractError("assignee outside preflight profiles")
            cards[name] = card
        expected_parents = {
            "evidence": [],
            "implementation": [cards["evidence"]["key"]],
            "qa": [cards["implementation"]["key"]],
            "release_note": [cards["qa"]["key"]],
            "release_compatibility": [cards["qa"]["key"], cards["release_note"]["key"]],
            "recurrence_check": [cards["release_compatibility"]["key"]],
        }
        if any(cards[name]["parents"] != expected for name, expected in expected_parents.items()):
            raise _ContractError("invalid graph dependencies")
        graphs.append(graph)
    return graphs


def _validate_report(value: object, expected_digest: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version", "window", "records", "proposals", "scorecards", "digest"
    }:
        raise _ContractError("invalid collector report")
    if value.get("schema_version") != 1 or value.get("digest") != expected_digest:
        raise _ContractError("invalid collector report identity")
    material = dict(value)
    del material["digest"]
    if _canonical_digest(material) != expected_digest:
        raise _ContractError("invalid collector report digest")
    if not isinstance(value.get("window"), Mapping) or set(value["window"]) != {"start", "end"}:
        raise _ContractError("invalid collector report window")
    if not isinstance(value.get("records"), list) or not isinstance(value.get("proposals"), list):
        raise _ContractError("invalid collector report lists")
    scorecards = value.get("scorecards")
    if not isinstance(scorecards, Mapping) or set(scorecards) != {"boards", "profiles"}:
        raise _ContractError("invalid collector report scorecards")
    scorecards = cast(Mapping[str, object], scorecards)
    if any(not isinstance(scorecards[name], list) for name in ("boards", "profiles")):
        raise _ContractError("invalid collector report scorecards")
    return value


def _validate_envelope(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise _ContractError("invalid envelope")
    try:
        if len(_canonical_json(value).encode()) > _MAX_ENVELOPE_BYTES:
            raise _ContractError("oversized envelope")
    except (TypeError, ValueError, OverflowError) as exc:
        raise _ContractError("unserializable envelope") from exc
    operation = value.get("operation")
    if not isinstance(operation, str) or operation not in _OPERATIONS:
        raise _ContractError("invalid operation")
    if set(value) != _COMMON_FIELDS | _OPERATION_FIELDS[operation]:
        raise _ContractError("invalid envelope fields")
    if (
        value.get("schema_version") != 1
        or value.get("capability") != CAPABILITY_ID
        or value.get("capability_version") != CAPABILITY_VERSION
        or value.get("board") != TARGET_BOARD
    ):
        raise _ContractError("invalid capability identity")
    request_digest = _digest(value.get("request_digest"), "request_digest")
    material = dict(value)
    del material["request_digest"]
    if _canonical_digest(material) != request_digest:
        raise _ContractError("invalid request digest")
    if operation == "verify_collector_report":
        report_digest = _digest(value.get("report_digest"), "report_digest")
        _validate_report(value.get("report"), report_digest)
    elif operation == "preflight":
        _digest(value.get("report_digest"), "report_digest")
        _profiles(value.get("profiles"))
        _text(value.get("workspace"), "workspace")
    elif operation == "apply_weekly_graphs":
        report_digest = _digest(value.get("report_digest"), "report_digest")
        profiles = _profiles(value.get("profiles"))
        _text(value.get("workspace"), "workspace")
        key = _text(value.get("idempotency_key"), "idempotency_key")
        if not key.startswith("weekly-plan:"):
            raise _ContractError("invalid idempotency namespace")
        _graphs(value.get("graphs"), report_digest=report_digest, profiles=profiles)
    else:
        key = _text(value.get("idempotency_key"), "idempotency_key")
        if not key.startswith("weekly-plan:"):
            raise _ContractError("invalid idempotency namespace")
        _digest(value.get("apply_request_digest"), "apply_request_digest")
    return dict(value)


def _configured_enabled() -> bool:
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
        kanban = config.get("kanban", {})
        return isinstance(kanban, Mapping) and kanban.get("atomic_graph_enabled") is True
    except Exception:
        return False


def _default_live_profiles() -> list[str]:
    return kb.list_profiles_on_disk()


def _pinned_workspace() -> Path:
    metadata = kb.read_board_metadata(TARGET_BOARD)
    configured = metadata.get("default_workdir")
    if not isinstance(configured, str) or not configured:
        raise _ContractError("pinned workspace unavailable")
    workspace = Path(configured).expanduser()
    if not workspace.is_absolute() or not workspace.is_dir():
        raise _ContractError("pinned workspace unavailable")
    return workspace.resolve()


def _rejected(code: str = "preflight_rejected") -> dict[str, object]:
    return {"outcome": "confirmed_not_applied", "error": {"code": code}}


class _AtomicGraphCapabilityV1:
    """Closed v1 facade; callers cannot select its board or storage path."""

    def __init__(
        self,
        *,
        db_path: Path | None = None,
        live_profiles: Callable[[], list[str]] = _default_live_profiles,
        workspace_root: Path | None = None,
    ) -> None:
        self._db_path = db_path or (kb.board_dir(TARGET_BOARD) / "kanban.db")
        self._live_profiles = live_profiles
        self._workspace_root = (workspace_root or _pinned_workspace()).resolve()
        if not self._workspace_root.is_absolute() or not self._workspace_root.is_dir():
            raise _ContractError("pinned workspace unavailable")
        self._verified_reports: dict[str, None] = {}
        self._preflights: dict[tuple[str, tuple[str, ...], str], None] = {}
        self._state_lock = threading.Lock()
        # Force the additive receipt migration once when the capability is
        # activated. This deliberately bypasses connect()'s steady-state path
        # cache, which may have been populated before a hot-loaded plugin first
        # asks for this newer optional table.
        kb.init_db(self._db_path)

    def _remember(self, cache: dict[Any, None], key: Any) -> None:
        cache[key] = None
        while len(cache) > _MAX_CACHE_ENTRIES:
            del cache[next(iter(cache))]

    def _live_preflight(self, envelope: Mapping[str, object]) -> tuple[str, tuple[str, ...], str]:
        digest = str(envelope["report_digest"])
        profiles = tuple(str(item) for item in cast(list[object], envelope["profiles"]))
        raw_workspace = Path(str(envelope["workspace"])).expanduser()
        if not raw_workspace.is_absolute() or not raw_workspace.is_dir():
            raise _ContractError("workspace unavailable")
        workspace = str(raw_workspace.resolve())
        if workspace != str(self._workspace_root):
            raise _ContractError("workspace is not the pinned board workdir")
        live = set(self._live_profiles())
        if not profiles or any(profile not in live for profile in profiles):
            raise _ContractError("profile unavailable")
        with self._state_lock:
            if digest not in self._verified_reports:
                raise _ContractError("report not verified")
        return digest, profiles, workspace

    def _receipt(self, key: str) -> tuple[str, dict[str, object]] | None:
        with kb.connect_closing(self._db_path) as conn:
            row = conn.execute(
                "SELECT apply_request_digest, receipt_json FROM atomic_graph_receipts WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        receipt = json.loads(row["receipt_json"])
        if not isinstance(receipt, dict):
            raise _ContractError("invalid stored receipt")
        return str(row["apply_request_digest"]), receipt

    def _reconcile(self, envelope: Mapping[str, object]) -> dict[str, object]:
        try:
            existing = self._receipt(str(envelope["idempotency_key"]))
        except Exception:
            return {"outcome": "indeterminate"}
        if existing is None:
            return {"outcome": "confirmed_not_applied"}
        stored_digest, receipt = existing
        if stored_digest != envelope["apply_request_digest"]:
            return _rejected("idempotency_conflict")
        return {"outcome": "confirmed_applied", "receipt": {**receipt, "idempotent": True}}

    def _apply(self, envelope: Mapping[str, object]) -> dict[str, object]:
        key = str(envelope["idempotency_key"])
        request_digest = str(envelope["request_digest"])
        try:
            existing = self._receipt(key)
        except Exception:
            return {"outcome": "indeterminate"}
        if existing is not None:
            stored_digest, receipt = existing
            if stored_digest != request_digest:
                return _rejected("idempotency_conflict")
            return {"outcome": "confirmed_applied", "receipt": {**receipt, "idempotent": True}}

        try:
            preflight_key = self._live_preflight(envelope)
            with self._state_lock:
                if preflight_key not in self._preflights:
                    raise _ContractError("preflight not confirmed")
            graphs = cast(list[Mapping[str, Any]], envelope["graphs"])
            task_keys = [str(graph[name]["key"]) for graph in graphs for name in _CARD_NAMES]
            with kb.connect_closing(self._db_path) as conn, kb.write_txn(conn):
                placeholders = ",".join("?" for _ in task_keys)
                if task_keys and conn.execute(
                    f"SELECT 1 FROM tasks WHERE idempotency_key IN ({placeholders}) LIMIT 1",
                    task_keys,
                ).fetchone() is not None:
                    raise _ContractError("task key already exists")
                task_ids_by_bundle: dict[str, list[str]] = {}
                graph_keys_by_bundle: dict[str, list[str]] = {}
                for graph in graphs:
                    bundle_key = str(graph["idempotency_key"])
                    ids_by_key: dict[str, str] = {}
                    bundle_ids: list[str] = []
                    bundle_graph_keys: list[str] = []
                    for name in _CARD_NAMES:
                        card = graph[name]
                        parent_ids = [ids_by_key[str(parent_key)] for parent_key in card["parents"]]
                        task_id = kb.create_task(
                            conn,
                            title=str(card["title"]),
                            body=_canonical_json(card["body"]),
                            assignee=str(card["assignee"]),
                            created_by=REQUIRED_PRINCIPAL,
                            workspace_kind="dir",
                            workspace_path=preflight_key[2],
                            parents=parent_ids,
                            idempotency_key=str(card["key"]),
                            board=TARGET_BOARD,
                        )
                        ids_by_key[str(card["key"])] = task_id
                        bundle_ids.append(task_id)
                        bundle_graph_keys.append(str(card["key"]))
                    task_ids_by_bundle[bundle_key] = bundle_ids
                    graph_keys_by_bundle[bundle_key] = bundle_graph_keys
                created = [task_id for ids in task_ids_by_bundle.values() for task_id in ids]
                receipt: dict[str, object] = {
                    "receipt_id": f"receipt:{request_digest}",
                    "idempotency_key": key,
                    "stable_bundle_keys": list(task_ids_by_bundle),
                    "created_task_ids": created,
                    "created_task_ids_by_bundle": task_ids_by_bundle,
                    "graph_task_keys_by_bundle": graph_keys_by_bundle,
                    "idempotent": False,
                }
                conn.execute(
                    "INSERT INTO atomic_graph_receipts "
                    "(idempotency_key, apply_request_digest, receipt_json, created_at) VALUES (?, ?, ?, ?)",
                    (key, request_digest, _canonical_json(receipt), int(time.time())),
                )
            return {"outcome": "confirmed_applied", "receipt": receipt}
        except Exception:
            # COMMIT can succeed before a response-path failure. Re-read the
            # durable receipt before claiming the batch was not applied.
            try:
                committed = self._receipt(key)
            except Exception:
                return {"outcome": "indeterminate"}
            if committed is not None:
                stored_digest, receipt = committed
                if stored_digest == request_digest:
                    return {"outcome": "confirmed_applied", "receipt": {**receipt, "idempotent": True}}
                return _rejected("idempotency_conflict")
            return _rejected()

    def execute(
        self,
        *,
        envelope: object,
        principal: object,
        provider: object,
    ) -> dict[str, object]:
        if principal != REQUIRED_PRINCIPAL or provider != REQUIRED_PROVIDER:
            return _rejected()
        try:
            closed = _validate_envelope(envelope)
        except Exception:
            return _rejected()
        operation = str(closed["operation"])
        if operation == "verify_collector_report":
            digest = str(closed["report_digest"])
            with self._state_lock:
                self._remember(self._verified_reports, digest)
            return {"outcome": "confirmed_not_applied", "report_digest": digest}
        if operation == "preflight":
            try:
                key = self._live_preflight(closed)
            except Exception:
                return _rejected()
            with self._state_lock:
                self._remember(self._preflights, key)
            return {"outcome": "confirmed_not_applied", "preflight": "confirmed"}
        if operation == "reconcile_weekly_graphs":
            return self._reconcile(closed)
        return self._apply(closed)


_CAPABILITY: _AtomicGraphCapabilityV1 | None = None
_CAPABILITY_LOCK = threading.Lock()


def get_capability_v1() -> _AtomicGraphCapabilityV1 | None:
    """Return the enabled v1 facade, or ``None`` when unavailable/disabled."""
    global _CAPABILITY
    if not _configured_enabled() or not kb.board_exists(TARGET_BOARD):
        return None
    with _CAPABILITY_LOCK:
        if _CAPABILITY is None:
            try:
                _CAPABILITY = _AtomicGraphCapabilityV1()
            except Exception:
                return None
        return _CAPABILITY
