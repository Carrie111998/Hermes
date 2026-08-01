"""Run-scoped Tool exposure for the Orchestrator Runtime bridge.

The Orchestrator supplies the executable Tool set. Hermes decides which of
those schemas are direct, deferred behind Tool Search, or hidden from the
model. Exposure never grants execution authority and never depends on Skills.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from tools.tool_search import (
    BRIDGE_TOOL_NAMES,
    CatalogEntry,
    bridge_tool_schemas,
    build_catalog,
    estimate_tokens_from_schemas,
    search_catalog,
)

logger = logging.getLogger("gateway.runtime_tool_exposure")

_DIRECT_PLATFORM_TOOLS = frozenset({
    "ask_user_question",
    "platform.tool_output_read",
})
_DEFAULT_SEARCH_LIMIT = 5
_MAX_SEARCH_LIMIT = 20
_AUTO_DEFER_THRESHOLD_TOKENS = 20_000


@dataclass(frozen=True)
class RuntimeToolExposure:
    direct_schemas: tuple[dict[str, Any], ...]
    deferred_catalog: tuple[CatalogEntry, ...]
    deferred_names: frozenset[str]
    hidden_names: frozenset[str]

    @property
    def model_schemas(self) -> list[dict[str, Any]]:
        visible = [dict(schema) for schema in self.direct_schemas]
        if self.deferred_catalog:
            bridge = bridge_tool_schemas(len(self.deferred_catalog))
            for schema in bridge:
                function = schema.get("function") or {}
                if function.get("name") == "tool_search":
                    query = (
                        (function.get("parameters") or {})
                        .get("properties", {})
                        .get("query", {})
                    )
                    query["description"] = (
                        "English capability keywords or an exact Tool name."
                    )
                elif function.get("name") == "tool_describe":
                    function["description"] = (
                        "Load the full JSON schema for a deferred Tool. If a Skill "
                        "or task already supplies its exact Tool name, describe it "
                        "directly without searching first."
                    )
            visible.extend(bridge)
        return visible

    def search(self, args: dict[str, Any]) -> str:
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
        }, ensure_ascii=False, separators=(",", ":"))

    def describe(self, args: dict[str, Any]) -> str:
        name = str(args.get("name") or "").strip()
        if not name:
            return _error("name is required")
        for entry in self.deferred_catalog:
            if entry.name == name:
                function = entry.schema.get("function") or {}
                return json.dumps({
                    "name": name,
                    "description": function.get("description", ""),
                    "parameters": function.get("parameters", {}),
                }, ensure_ascii=False, separators=(",", ":"))
        return _error(f"'{name}' is not a deferred Tool available to this Run")

    def resolve_call(
        self,
        args: dict[str, Any],
    ) -> tuple[str | None, dict[str, Any], str | None]:
        name = str(args.get("name") or "").strip()
        if not name:
            return None, {}, "tool_call requires a name"
        if name in BRIDGE_TOOL_NAMES:
            return None, {}, "tool_call cannot invoke another bridge Tool"
        if name not in self.deferred_names:
            return None, {}, f"'{name}' is not a deferred Tool available to this Run"
        arguments = args.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return None, {}, "tool_call arguments must be valid JSON"
        if not isinstance(arguments, dict):
            return None, {}, "tool_call arguments must be an object"
        return name, dict(arguments), None


def build_runtime_tool_exposure(
    definitions: list[dict[str, Any]],
    schemas: list[dict[str, Any]],
) -> RuntimeToolExposure:
    if len(definitions) != len(schemas):
        raise ValueError("Tool definitions and schemas differ")
    classified: list[tuple[str, dict[str, Any]]] = []
    automatic: list[dict[str, Any]] = []
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
            classified.append(("automatic", schema))
            automatic.append(schema)
    automatic_tokens = estimate_tokens_from_schemas(automatic)
    defer_automatic = automatic_tokens >= _AUTO_DEFER_THRESHOLD_TOKENS
    for classification, schema in classified:
        if classification == "direct" or (
            classification == "automatic" and not defer_automatic
        ):
            direct.append(schema)
        elif classification == "deferred" or classification == "automatic":
            deferred.append(schema)
    catalog = build_catalog(deferred)
    logger.info(
        "runtime Tool exposure: direct=%d deferred=%d hidden=%d "
        "automatic_schema_tokens=%d automatic_deferred=%s",
        len(direct),
        len(catalog),
        len(hidden),
        automatic_tokens,
        defer_automatic,
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
