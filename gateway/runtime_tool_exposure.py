"""Run-scoped Tool exposure for the Orchestrator Runtime bridge.

The Orchestrator supplies the executable Tool set. Hermes decides which of
those schemas are direct, deferred behind Tool Search, or hidden from the
model. Exposure never grants execution authority and never depends on Skills.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from tools.tool_search import (
    BRIDGE_TOOL_NAMES,
    CatalogEntry,
    bridge_tool_schemas,
    build_catalog,
    search_catalog,
)

logger = logging.getLogger("gateway.runtime_tool_exposure")

_DIRECT_PLATFORM_TOOLS = frozenset({
    "ask_user_question",
    "platform.tool_output_read",
})
_DEFAULT_SEARCH_LIMIT = 5
_MAX_SEARCH_LIMIT = 20


@dataclass
class RuntimeToolExposure:
    direct_schemas: tuple[dict[str, Any], ...]
    deferred_catalog: tuple[CatalogEntry, ...]
    deferred_names: frozenset[str]
    hidden_names: frozenset[str]
    activated_names: set[str] = field(default_factory=set)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    @property
    def model_schemas(self) -> list[dict[str, Any]]:
        visible = [dict(schema) for schema in self.direct_schemas]
        with self._lock:
            activated = set(self.activated_names)
        visible.extend(
            dict(entry.schema)
            for entry in self.deferred_catalog
            if entry.name in activated
        )
        if self.deferred_catalog:
            search_schema = bridge_tool_schemas(len(self.deferred_catalog))[0]
            function = search_schema.get("function") or {}
            function["description"] = (
                f"Search {len(self.deferred_catalog)} additional Tools available "
                "to this Run. Matching real Tool schemas are loaded into the "
                "model's Tool surface and become directly callable on the next step."
            )
            query = (
                (function.get("parameters") or {})
                .get("properties", {})
                .get("query", {})
            )
            query["description"] = "English capability keywords or an exact Tool name."
            visible.append(search_schema)
        return visible

    def search_and_activate(self, args: dict[str, Any]) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            return _error("query is required")
        raw_limit = args.get("limit", _DEFAULT_SEARCH_LIMIT)
        if isinstance(raw_limit, bool):
            return _error("limit must be an integer")
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return _error("limit must be an integer")
        limit = max(1, min(_MAX_SEARCH_LIMIT, limit))
        normalized_query = query.casefold()
        exact_matches = [
            entry
            for entry in self.deferred_catalog
            if entry.name.casefold() == normalized_query
            or entry.name.casefold() in normalized_query
        ]
        matches = exact_matches[:limit] or search_catalog(
            list(self.deferred_catalog),
            query,
            limit,
        )
        matched_names = [entry.name for entry in matches]
        with self._lock:
            already_loaded = [
                name for name in matched_names if name in self.activated_names
            ]
            self.activated_names.update(matched_names)
        return json.dumps({
            "query": query,
            "total_available": len(self.deferred_catalog),
            "matches": [
                {
                    "name": entry.name,
                    "description": entry.description[:400],
                    "source": "platform",
                }
                for entry in matches
            ],
            "loaded_tools": matched_names,
            "already_loaded": already_loaded,
            "callable_on_next_step": bool(matched_names),
        }, ensure_ascii=False, separators=(",", ":"))

    def activate_names(self, names: set[str]) -> None:
        with self._lock:
            self.activated_names.update(names & self.deferred_names)

    def snapshot_activated_names(self) -> list[str]:
        """Return a stable list of loaded deferred Tools."""
        with self._lock:
            return sorted(self.activated_names & self.deferred_names)

    def is_callable(self, name: str) -> bool:
        direct_names = {
            str((schema.get("function") or {}).get("name") or "")
            for schema in self.direct_schemas
        }
        with self._lock:
            return name in direct_names or name in self.activated_names


def build_runtime_tool_exposure(
    definitions: list[dict[str, Any]],
    schemas: list[dict[str, Any]],
) -> RuntimeToolExposure:
    if len(definitions) != len(schemas):
        raise ValueError("Tool definitions and schemas differ")
    classified: list[tuple[str, dict[str, Any]]] = []
    direct: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    hidden: set[str] = set()
    for definition, schema in zip(definitions, schemas, strict=True):
        name = str(definition.get("name") or "").strip()
        if name in BRIDGE_TOOL_NAMES:
            raise ValueError(f"platform Tool name {name} is reserved")
        requested = str(definition.get("exposure") or "").strip().lower()
        if requested and requested not in {"direct", "deferred", "hidden"}:
            raise ValueError(f"Tool exposure is invalid for {name}")
        if requested == "hidden":
            hidden.add(name)
            classified.append(("hidden", schema))
        elif requested == "direct" or (
            not requested and name in _DIRECT_PLATFORM_TOOLS
        ):
            classified.append(("direct", schema))
        elif requested == "deferred":
            classified.append(("deferred", schema))
        else:
            classified.append(("direct", schema))
    for classification, schema in classified:
        if classification == "direct":
            direct.append(schema)
        elif classification == "deferred":
            deferred.append(schema)
    catalog = build_catalog(deferred)
    logger.info(
        "runtime Tool exposure: direct=%d deferred=%d hidden=%d",
        len(direct),
        len(catalog),
        len(hidden),
    )
    return RuntimeToolExposure(
        direct_schemas=tuple(direct),
        deferred_catalog=tuple(catalog),
        deferred_names=frozenset(entry.name for entry in catalog),
        hidden_names=frozenset(hidden),
    )


def _error(message: str) -> str:
    return json.dumps(
        {"error": {"code": "invalid_tool_request", "message": message}},
        ensure_ascii=False,
        separators=(",", ":"),
    )
