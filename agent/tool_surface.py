"""Shared, non-mutating assembly for the complete model-facing tool surface."""

from __future__ import annotations

import functools
import logging
import threading
import types
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, cast

logger = logging.getLogger(__name__)


@dataclass
class FullToolSurface:
    """Result of assembling registry and externally provided tool schemas."""

    tool_defs: List[Dict[str, Any]]
    pre_assembly_tool_defs: List[Dict[str, Any]]
    injected_names: Dict[str, List[str]] = field(
        default_factory=lambda: {"memory": [], "context_engine": []}
    )
    skipped: Dict[str, List[Dict[str, str]]] = field(
        default_factory=lambda: {"memory": [], "context_engine": []}
    )
    tool_search_activated: bool = False
    deferred_names: List[str] = field(default_factory=list)
    deferred_tokens: int = 0
    threshold_tokens: int = 0
    tool_search_tier: int = 0
    tool_search_listing_form: str = "none"


@dataclass(frozen=True)
class AgentToolSurfaceSnapshot:
    """Atomically published envelope consumed by runtime tool readers."""

    tool_defs: tuple[Dict[str, Any], ...]
    catalog_tool_defs: tuple[Dict[str, Any], ...]
    valid_tool_names: frozenset[str]
    memory_provider_tool_names: frozenset[str]
    context_engine_tool_names: frozenset[str]
    deferred_tool_names: frozenset[str]
    registry_entries: tuple[tuple[str, Any], ...]
    memory_manager: Any
    context_engine: Any
    registry_generation: int
    selection_revision: int
    enabled_toolsets: Optional[tuple[str, ...]]
    disabled_toolsets: Optional[tuple[str, ...]]
    context_engine_provenance: Any = None


_surface_lock_creation = threading.Lock()
_fallback_surface_lock = threading.RLock()


def _agent_surface_lock(agent: Any):
    lock = getattr(agent, "_tool_surface_lock", None)
    if lock is not None:
        return lock
    with _surface_lock_creation:
        lock = getattr(agent, "_tool_surface_lock", None)
        if lock is None:
            lock = threading.RLock()
            try:
                agent._tool_surface_lock = lock
            except Exception:
                return _fallback_surface_lock
    return lock


def _capture_registry_entries(
    tool_defs: Iterable[Dict[str, Any]],
    *,
    external_names: Iterable[str],
) -> tuple[tuple[str, Any], ...]:
    from tools.registry import registry

    external = set(external_names)
    names: list[str] = []
    seen: set[str] = set()
    for tool in tool_defs:
        name = _tool_name(tool)
        if not name or name in seen or name in external:
            continue
        seen.add(name)
        names.append(name)
    captured = registry.snapshot_entries_by_name(names)
    return captured[1] if captured is not None else ()


def _capture_registry_entries_by_name(
    names: Iterable[str],
    *,
    external_names: Iterable[str],
) -> tuple[tuple[str, Any], ...]:
    from tools.registry import registry

    external = frozenset(external_names)
    captured = registry.snapshot_entries_by_name(
        name for name in sorted(set(names)) if name and name not in external
    )
    return captured[1] if captured is not None else ()


def capture_registry_entries_for_generation(
    tool_defs: Iterable[Dict[str, Any]],
    *,
    external_names: Iterable[str],
    expected_generation: int,
) -> Optional[tuple[tuple[str, Any], ...]]:
    """Capture dispatch entries only if schemas still match the registry."""
    from tools.registry import registry

    external = set(external_names)
    names: list[str] = []
    seen: set[str] = set()
    for tool in tool_defs:
        name = _tool_name(tool)
        if not name or name in seen or name in external:
            continue
        seen.add(name)
        names.append(name)
    captured = registry.snapshot_entries_by_name(
        names,
        expected_generation=expected_generation,
    )
    return captured[1] if captured is not None else None


def _catalog_deferred_names(
    tool_defs: Iterable[Dict[str, Any]],
) -> frozenset[str]:
    try:
        from tools.tool_search import scoped_deferrable_names

        return scoped_deferrable_names(list(tool_defs))
    except Exception:
        return frozenset()


def _freeze_selection(value: Optional[Iterable[str]]) -> Optional[tuple[str, ...]]:
    if value is None:
        return None
    return tuple(str(item) for item in value)


def _pinned_provider_or_live(agent: Any, ceiling_attr: str, live_attr: str) -> Any:
    """Return an explicit provider ceiling, including an explicit ``None``."""
    state = getattr(agent, "__dict__", None)
    if isinstance(state, dict) and ceiling_attr in state:
        return state[ceiling_attr]
    return getattr(agent, live_attr, None)


def _callable_provenance(value: Any) -> Any:
    if not callable(value):
        return None
    if isinstance(value, types.MethodType):
        return ("method", value.__func__)
    if isinstance(value, types.BuiltinMethodType):
        return ("builtin_method", type(value.__self__), value.__name__)
    if isinstance(value, functools.partial):
        return ("partial", _callable_provenance(value.func))
    if isinstance(value, (types.FunctionType, types.BuiltinFunctionType)):
        return ("callable", value)
    return ("callable_type", type(value), getattr(type(value), "__call__", None))


def context_engine_provenance(engine: Any) -> Any:
    """Return stable code provenance for legitimate per-agent engine instances."""
    if engine is None:
        return None
    engine_type = type(engine)
    schema_impl = _callable_provenance(getattr(engine, "get_tool_schemas", None))
    handler_impl = _callable_provenance(getattr(engine, "handle_tool_call", None))
    if schema_impl is None or handler_impl is None:
        return None
    try:
        name = str(getattr(engine, "name", "") or "")
    except Exception:
        return None
    return (engine_type, name, schema_impl, handler_impl)


def get_agent_tool_surface(agent: Any) -> AgentToolSurfaceSnapshot:
    """Read one coherent surface, falling back for legacy/test agents."""
    with _agent_surface_lock(agent):
        snapshot = getattr(agent, "_tool_surface_snapshot", None)
        if isinstance(snapshot, AgentToolSurfaceSnapshot):
            legacy_tools = getattr(agent, "tools", None)
            tools_replaced = (
                legacy_tools is not None
                and id(legacy_tools)
                != getattr(agent, "_tool_surface_legacy_tools_id", id(legacy_tools))
            )
            legacy_valid = frozenset(
                getattr(agent, "valid_tool_names", None) or ()
            )
            legacy_memory = frozenset(
                getattr(agent, "_memory_provider_tool_names", None) or ()
            )
            legacy_context = frozenset(
                getattr(agent, "_context_engine_tool_names", None) or ()
            )
            legacy_memory_manager = _pinned_provider_or_live(
                agent,
                "_tool_surface_memory_manager_ceiling",
                "_memory_manager",
            )
            legacy_context_engine = _pinned_provider_or_live(
                agent,
                "_tool_surface_context_engine_ceiling",
                "context_compressor",
            )
            if (
                tools_replaced
                or legacy_valid != snapshot.valid_tool_names
                or legacy_memory != snapshot.memory_provider_tool_names
                or legacy_context != snapshot.context_engine_tool_names
                or legacy_memory_manager is not snapshot.memory_manager
                or legacy_context_engine is not snapshot.context_engine
            ):
                tool_defs = tuple(legacy_tools or ())
                valid_names = legacy_valid
                if tools_replaced and legacy_valid == snapshot.valid_tool_names:
                    valid_names = frozenset(
                        name for tool in tool_defs if (name := _tool_name(tool))
                    )
                catalog_defs = (
                    tool_defs if tools_replaced else snapshot.catalog_tool_defs
                )
                return AgentToolSurfaceSnapshot(
                    tool_defs=tool_defs,
                    catalog_tool_defs=catalog_defs,
                    valid_tool_names=valid_names,
                    memory_provider_tool_names=legacy_memory,
                    context_engine_tool_names=legacy_context,
                    deferred_tool_names=(
                        _catalog_deferred_names(catalog_defs)
                        if tools_replaced
                        else snapshot.deferred_tool_names
                    ),
                    registry_entries=(
                        _capture_registry_entries_by_name(
                            set(valid_names)
                            | {
                                name
                                for tool in catalog_defs
                                if (name := _tool_name(tool))
                            },
                            external_names=legacy_memory | legacy_context,
                        )
                        if tools_replaced
                        or legacy_valid != snapshot.valid_tool_names
                        or legacy_memory != snapshot.memory_provider_tool_names
                        or legacy_context != snapshot.context_engine_tool_names
                        else snapshot.registry_entries
                    ),
                    memory_manager=legacy_memory_manager,
                    context_engine=legacy_context_engine,
                    registry_generation=snapshot.registry_generation,
                    selection_revision=snapshot.selection_revision,
                    enabled_toolsets=snapshot.enabled_toolsets,
                    disabled_toolsets=snapshot.disabled_toolsets,
                    context_engine_provenance=(
                        snapshot.context_engine_provenance
                        if legacy_context_engine is snapshot.context_engine
                        else context_engine_provenance(legacy_context_engine)
                    ),
                )
            return snapshot

        tool_defs = tuple(getattr(agent, "tools", None) or ())
        valid_names = getattr(agent, "valid_tool_names", None)
        if valid_names is None:
            valid_names = {_tool_name(tool) for tool in tool_defs if _tool_name(tool)}
        memory_names = frozenset(
            getattr(agent, "_memory_provider_tool_names", None) or ()
        )
        context_names = frozenset(
            getattr(agent, "_context_engine_tool_names", None) or ()
        )
        registry_generation = getattr(agent, "_tool_snapshot_generation", 0)
        selection_revision = getattr(agent, "_tool_selection_revision", 0)
        fallback_context_engine = _pinned_provider_or_live(
            agent,
            "_tool_surface_context_engine_ceiling",
            "context_compressor",
        )
        return AgentToolSurfaceSnapshot(
            tool_defs=tool_defs,
            catalog_tool_defs=tool_defs,
            valid_tool_names=frozenset(valid_names),
            memory_provider_tool_names=memory_names,
            context_engine_tool_names=context_names,
            deferred_tool_names=_catalog_deferred_names(tool_defs),
            registry_entries=_capture_registry_entries(
                tool_defs,
                external_names=memory_names | context_names,
            ),
            memory_manager=_pinned_provider_or_live(
                agent,
                "_tool_surface_memory_manager_ceiling",
                "_memory_manager",
            ),
            context_engine=fallback_context_engine,
            registry_generation=(
                registry_generation if isinstance(registry_generation, int) else 0
            ),
            selection_revision=(
                selection_revision if isinstance(selection_revision, int) else 0
            ),
            enabled_toolsets=_freeze_selection(
                getattr(agent, "enabled_toolsets", None)
            ),
            disabled_toolsets=_freeze_selection(
                getattr(agent, "disabled_toolsets", None)
            ),
            context_engine_provenance=context_engine_provenance(
                fallback_context_engine
            ),
        )


def tool_surface_registration_error(
    surface: AgentToolSurfaceSnapshot,
    name: str,
) -> Optional[str]:
    """Fail closed when a pinned non-provider tool lost registry ownership."""
    if name in surface.memory_provider_tool_names:
        if surface.memory_manager is not None:
            return None
        return (
            f"Tool provider is unavailable for this request snapshot: {name}. "
            "Retry the request."
        )
    if name in surface.context_engine_tool_names:
        if surface.context_engine is not None:
            return None
        return (
            f"Tool provider is unavailable for this request snapshot: {name}. "
            "Retry the request."
        )

    expected_entry = dict(surface.registry_entries).get(name)
    if expected_entry is None:
        if name in surface.valid_tool_names or name in surface.deferred_tool_names:
            return (
                f"Tool registration is unavailable for this request: {name}. "
                "Retry the request."
            )
        return None

    from tools.registry import registry

    return registry.expected_entry_error(name, expected_entry)


def publish_agent_tool_surface(
    agent: Any,
    tool_defs: Iterable[Dict[str, Any]],
    *,
    catalog_tool_defs: Optional[Iterable[Dict[str, Any]]] = None,
    memory_provider_tool_names: Iterable[str] = (),
    context_engine_tool_names: Iterable[str],
    deferred_tool_names: Optional[Iterable[str]] = None,
    registry_entries: Optional[Iterable[tuple[str, Any]]] = None,
    registry_generation: int,
    selection_revision: int,
    enabled_toolsets: Optional[Iterable[str]],
    disabled_toolsets: Optional[Iterable[str]],
) -> AgentToolSurfaceSnapshot:
    """Publish legacy attributes, then expose them as one atomic snapshot."""
    staged_defs = list(tool_defs)
    staged_catalog = (
        list(catalog_tool_defs)
        if catalog_tool_defs is not None
        else list(staged_defs)
    )
    raw_valid_name_ceiling = getattr(
        agent,
        "_tool_surface_valid_name_ceiling",
        None,
    )
    valid_name_ceiling = (
        frozenset(raw_valid_name_ceiling)
        if isinstance(raw_valid_name_ceiling, (set, frozenset, tuple, list))
        else None
    )
    raw_catalog_name_ceiling = getattr(
        agent,
        "_tool_surface_catalog_name_ceiling",
        valid_name_ceiling,
    )
    catalog_name_ceiling = (
        frozenset(raw_catalog_name_ceiling)
        if isinstance(raw_catalog_name_ceiling, (set, frozenset, tuple, list))
        else valid_name_ceiling
    )
    if valid_name_ceiling is not None:
        allowed = set(valid_name_ceiling)
        staged_defs = [tool for tool in staged_defs if _tool_name(tool) in allowed]
    if catalog_name_ceiling is not None:
        allowed_catalog = set(catalog_name_ceiling)
        staged_catalog = [
            tool for tool in staged_catalog if _tool_name(tool) in allowed_catalog
        ]
    raw_visible_definition_ceiling = getattr(
        agent,
        "_tool_surface_visible_definition_ceiling",
        None,
    )
    if isinstance(raw_visible_definition_ceiling, dict):
        staged_defs = [
            raw_visible_definition_ceiling[name]
            for tool in staged_defs
            if (name := _tool_name(tool)) in raw_visible_definition_ceiling
        ]
    raw_catalog_definition_ceiling = getattr(
        agent,
        "_tool_surface_catalog_definition_ceiling",
        None,
    )
    if isinstance(raw_catalog_definition_ceiling, dict):
        staged_catalog = [
            raw_catalog_definition_ceiling[name]
            for tool in staged_catalog
            if (name := _tool_name(tool)) in raw_catalog_definition_ceiling
        ]
    frozen_defs = tuple(deepcopy(staged_defs))
    frozen_catalog = tuple(deepcopy(staged_catalog))
    memory_names = frozenset(memory_provider_tool_names)
    context_names = frozenset(context_engine_tool_names)
    if valid_name_ceiling is not None:
        memory_names &= frozenset(valid_name_ceiling)
        context_names &= frozenset(valid_name_ceiling)
    raw_memory_name_ceiling = getattr(
        agent,
        "_tool_surface_memory_provider_name_ceiling",
        None,
    )
    if isinstance(raw_memory_name_ceiling, (set, frozenset, tuple, list)):
        memory_names &= frozenset(raw_memory_name_ceiling)
    raw_context_name_ceiling = getattr(
        agent,
        "_tool_surface_context_engine_name_ceiling",
        None,
    )
    if isinstance(raw_context_name_ceiling, (set, frozenset, tuple, list)):
        context_names &= frozenset(raw_context_name_ceiling)
    frozen_deferred_names = (
        frozenset(deferred_tool_names)
        if deferred_tool_names is not None
        else _catalog_deferred_names(frozen_catalog)
    )
    if catalog_name_ceiling is not None:
        frozen_deferred_names &= frozenset(catalog_name_ceiling)
    frozen_registry_entries = (
        tuple(registry_entries)
        if registry_entries is not None
        else _capture_registry_entries(
            list(frozen_catalog) + list(frozen_defs),
            external_names=memory_names | context_names,
        )
    )
    if catalog_name_ceiling is not None:
        allowed_entry_names = frozenset(catalog_name_ceiling) | frozenset(
            valid_name_ceiling or ()
        )
        frozen_registry_entries = tuple(
            (name, entry)
            for name, entry in frozen_registry_entries
            if name in allowed_entry_names
        )
    raw_registry_entry_ceiling = getattr(
        agent,
        "_tool_surface_registry_entry_ceiling",
        None,
    )
    if isinstance(raw_registry_entry_ceiling, dict):
        surfaced_names = {
            name
            for tool in (*frozen_defs, *frozen_catalog)
            if (name := _tool_name(tool))
        }
        frozen_registry_entries = tuple(
            (name, entry)
            for name, entry in raw_registry_entry_ceiling.items()
            if name in surfaced_names
        )
    pinned_context_engine = _pinned_provider_or_live(
        agent,
        "_tool_surface_context_engine_ceiling",
        "context_compressor",
    )
    pinned_context_engine_provenance = _pinned_provider_or_live(
        agent,
        "_tool_surface_context_engine_provenance_ceiling",
        "_context_engine_provenance",
    )
    if pinned_context_engine_provenance is None:
        pinned_context_engine_provenance = context_engine_provenance(
            pinned_context_engine
        )
    snapshot = AgentToolSurfaceSnapshot(
        tool_defs=frozen_defs,
        catalog_tool_defs=frozen_catalog,
        valid_tool_names=frozenset(
            name for tool in frozen_defs if (name := _tool_name(tool))
        ),
        memory_provider_tool_names=memory_names,
        context_engine_tool_names=context_names,
        deferred_tool_names=frozen_deferred_names,
        registry_entries=frozen_registry_entries,
        memory_manager=_pinned_provider_or_live(
            agent,
            "_tool_surface_memory_manager_ceiling",
            "_memory_manager",
        ),
        context_engine=pinned_context_engine,
        registry_generation=registry_generation,
        selection_revision=selection_revision,
        enabled_toolsets=_freeze_selection(enabled_toolsets),
        disabled_toolsets=_freeze_selection(disabled_toolsets),
        context_engine_provenance=pinned_context_engine_provenance,
    )

    # Runtime accessors continue reading the prior snapshot until this complete
    # compatibility publication is done. The final single snapshot assignment
    # is the visibility boundary for concurrent readers.
    from model_tools import record_resolved_tool_names

    with _agent_surface_lock(agent):
        agent._tool_surface_publish_in_progress = True
        try:
            agent.tools = deepcopy(list(snapshot.tool_defs))
            agent.valid_tool_names = set(snapshot.valid_tool_names)
            agent._memory_provider_tool_names = set(
                snapshot.memory_provider_tool_names
            )
            agent._context_engine_tool_names = set(
                snapshot.context_engine_tool_names
            )
            agent._tool_snapshot_generation = snapshot.registry_generation
            agent._tool_selection_revision = snapshot.selection_revision
            agent.enabled_toolsets = (
                list(snapshot.enabled_toolsets)
                if snapshot.enabled_toolsets is not None
                else None
            )
            agent.disabled_toolsets = (
                list(snapshot.disabled_toolsets)
                if snapshot.disabled_toolsets is not None
                else None
            )
            agent._tool_surface_legacy_tools_id = id(agent.tools)
            record_resolved_tool_names(list(snapshot.tool_defs))
            agent._tool_surface_snapshot = snapshot
        finally:
            agent._tool_surface_publish_in_progress = False
    return snapshot


def publish_agent_tool_surface_for_generation(
    agent: Any,
    tool_defs: Iterable[Dict[str, Any]],
    *,
    catalog_tool_defs: Iterable[Dict[str, Any]],
    memory_provider_tool_names: Iterable[str] = (),
    context_engine_tool_names: Iterable[str],
    deferred_tool_names: Optional[Iterable[str]] = None,
    expected_registry_generation: int,
    selection_revision: int,
    enabled_toolsets: Optional[Iterable[str]],
    disabled_toolsets: Optional[Iterable[str]],
) -> Optional[AgentToolSurfaceSnapshot]:
    """Capture registry entries and publish them under one generation lease."""
    from tools.registry import registry

    staged_defs = list(tool_defs)
    staged_catalog = list(catalog_tool_defs)
    memory_names = frozenset(memory_provider_tool_names)
    context_names = frozenset(context_engine_tool_names)
    external_names = memory_names | context_names
    names: list[str] = []
    seen: set[str] = set()
    for tool in (*staged_catalog, *staged_defs):
        name = _tool_name(tool)
        if not name or name in seen or name in external_names:
            continue
        seen.add(name)
        names.append(name)

    def _publish(
        generation: int,
        entries: tuple[tuple[str, Any], ...],
    ) -> AgentToolSurfaceSnapshot:
        return publish_agent_tool_surface(
            agent,
            staged_defs,
            catalog_tool_defs=staged_catalog,
            memory_provider_tool_names=memory_names,
            context_engine_tool_names=context_names,
            deferred_tool_names=deferred_tool_names,
            registry_entries=entries,
            registry_generation=generation,
            selection_revision=selection_revision,
            enabled_toolsets=enabled_toolsets,
            disabled_toolsets=disabled_toolsets,
        )

    # Match get_agent_tool_surface's lock order so legacy fallback capture and
    # publication cannot deadlock while the registry generation is leased.
    with _agent_surface_lock(agent):
        return registry.run_with_entries_snapshot_by_name(
            names,
            _publish,
            expected_generation=expected_registry_generation,
        )


def _tool_name(tool: Dict[str, Any]) -> str:
    function = tool.get("function") if isinstance(tool, dict) else None
    if not isinstance(function, dict):
        return ""
    name = function.get("name")
    return name if isinstance(name, str) else ""


def _family_enabled(
    family: str,
    enabled_toolsets: Optional[Iterable[str]],
    disabled_toolsets: Optional[Iterable[str]],
    base_names: set[str],
) -> bool:
    """Resolve an external family with disabled selections taking precedence."""
    disabled = {str(name) for name in (disabled_toolsets or [])}
    if family in disabled:
        return False

    try:
        from toolsets import resolve_toolset

        if any(family in resolve_toolset(name) for name in disabled):
            return False
    except Exception:
        logger.debug(
            "Failed to resolve disabled toolsets for %s", family, exc_info=True
        )

    if enabled_toolsets is None:
        return True
    enabled = {str(name) for name in enabled_toolsets}
    if not enabled:
        return False
    if family in enabled or family in base_names:
        return True

    try:
        from toolsets import resolve_toolset

        return any(family in resolve_toolset(name) for name in enabled)
    except Exception:
        logger.debug("Failed to resolve enabled toolsets for %s", family, exc_info=True)
        return False


def _append_external_family(
    staged: List[Dict[str, Any]],
    existing_names: set[str],
    family: str,
    schemas: Iterable[Dict[str, Any]],
    *,
    enabled: bool,
    result: FullToolSurface,
) -> None:
    from agent.memory_manager import normalize_tool_schema

    for raw_schema in schemas:
        schema = normalize_tool_schema(raw_schema)
        raw_name = ""
        if isinstance(raw_schema, dict):
            candidate = raw_schema.get("name")
            if isinstance(candidate, str):
                raw_name = candidate
        if schema is None:
            result.skipped[family].append({
                "tool": raw_name or "<unnamed>",
                "reason": "invalid schema",
            })
            continue

        name = schema["name"]
        if not enabled:
            result.skipped[family].append({"tool": name, "reason": "toolset disabled"})
            continue
        if name in existing_names:
            result.skipped[family].append({
                "tool": name,
                "reason": "duplicate tool name",
            })
            continue

        staged.append({"type": "function", "function": schema})
        existing_names.add(name)
        result.injected_names[family].append(name)


def assemble_full_tool_surface(
    base_tool_defs: Iterable[Dict[str, Any]],
    *,
    enabled_toolsets: Optional[Iterable[str]] = None,
    disabled_toolsets: Optional[Iterable[str]] = None,
    memory_tool_schemas: Optional[Iterable[Dict[str, Any]]] = None,
    context_engine_tool_schemas: Optional[Iterable[Dict[str, Any]]] = None,
    apply_tool_search: bool = True,
    context_length: Optional[int] = None,
    tool_search_config: Any = None,
    quiet_mode: bool = True,
) -> FullToolSurface:
    """Assemble a complete surface without mutating inputs or runtime globals."""
    staged = list(base_tool_defs or [])
    existing_names = {name for tool in staged if (name := _tool_name(tool))}
    result = FullToolSurface(tool_defs=[], pre_assembly_tool_defs=[])

    _append_external_family(
        staged,
        existing_names,
        "memory",
        memory_tool_schemas or [],
        enabled=_family_enabled(
            "memory", enabled_toolsets, disabled_toolsets, existing_names
        ),
        result=result,
    )
    _append_external_family(
        staged,
        existing_names,
        "context_engine",
        context_engine_tool_schemas or [],
        enabled=_family_enabled(
            "context_engine", enabled_toolsets, disabled_toolsets, existing_names
        ),
        result=result,
    )

    try:
        from tools.schema_sanitizer import sanitize_tool_schemas

        staged = sanitize_tool_schemas(staged)
    except Exception as exc:  # pragma: no cover - defensive fail-soft path
        logger.warning("Schema sanitization skipped: %s", exc)
        staged = list(staged)

    result.pre_assembly_tool_defs = list(staged)
    result.tool_defs = list(staged)
    if not apply_tool_search:
        return result

    try:
        from tools.tool_search import (
            assemble_tool_defs,
            classify_tools,
            load_config,
        )

        config = tool_search_config if tool_search_config is not None else load_config()
        if config.enabled == "off":
            return result
        assembly = assemble_tool_defs(
            staged,
            context_length=context_length,
            config=config,
        )
        result.tool_defs = assembly.tool_defs
        result.tool_search_activated = assembly.activated
        result.deferred_tokens = assembly.deferred_tokens
        result.threshold_tokens = assembly.threshold_tokens
        result.tool_search_tier = assembly.tier
        result.tool_search_listing_form = assembly.listing_form
        if assembly.activated:
            _, deferred = classify_tools(staged)
            result.deferred_names = sorted(
                name for tool in deferred if (name := _tool_name(tool))
            )
            if not quiet_mode:
                forms = {
                    "full": "catalog listing embedded",
                    "names": "names-only listing embedded",
                    "mixed": "listing embedded (oversized servers summarized)",
                    "groups": "server summary embedded (search-only discovery)",
                    "none": "no listing (search-only)",
                }
                print(
                    f"🔎 Tool Search (tier {assembly.tier}): "
                    f"{assembly.deferred_count} MCP/plugin tools deferred "
                    f"(~{assembly.deferred_tokens} tokens) behind "
                    "tool_search/describe/call — "
                    f"{forms.get(assembly.listing_form, assembly.listing_form)}."
                )
    except Exception as exc:  # pragma: no cover - never break tool loading
        logger.warning("Tool search assembly skipped: %s", exc)

    return result


def assemble_agent_tool_surface(
    agent: Any,
    base_tool_defs: Iterable[Dict[str, Any]],
    *,
    quiet_mode: bool = True,
    toolset_selection: Optional[
        tuple[Optional[Iterable[str]], Optional[Iterable[str]]]
    ] = None,
) -> FullToolSurface:
    """Collect an agent's external schemas and assemble its final tool surface."""
    memory_schemas: List[Dict[str, Any]] = []
    memory_manager = getattr(agent, "_memory_manager", None)
    get_memory_schemas = (
        getattr(memory_manager, "get_all_tool_schemas", None)
        if memory_manager is not None
        else None
    )
    if callable(get_memory_schemas):
        try:
            collect_memory = cast(
                Callable[[], Iterable[Dict[str, Any]]], get_memory_schemas
            )
            memory_schemas = list(collect_memory() or [])
        except Exception:
            logger.debug("Memory-provider schema collection skipped", exc_info=True)

    context_schemas: List[Dict[str, Any]] = []
    compressor = getattr(agent, "context_compressor", None)
    get_context_schemas = (
        getattr(compressor, "get_tool_schemas", None)
        if compressor is not None
        else None
    )
    if callable(get_context_schemas):
        try:
            collect_context = cast(
                Callable[[], Iterable[Dict[str, Any]]], get_context_schemas
            )
            context_schemas = list(collect_context() or [])
        except Exception:
            logger.debug("Context-engine schema collection skipped", exc_info=True)

    context_length = getattr(compressor, "context_length", None)
    if toolset_selection is None:
        enabled_toolsets = getattr(agent, "enabled_toolsets", None)
        disabled_toolsets = getattr(agent, "disabled_toolsets", None)
    else:
        enabled_toolsets, disabled_toolsets = toolset_selection

    return assemble_full_tool_surface(
        base_tool_defs,
        enabled_toolsets=enabled_toolsets,
        disabled_toolsets=disabled_toolsets,
        memory_tool_schemas=memory_schemas,
        context_engine_tool_schemas=context_schemas,
        context_length=context_length,
        quiet_mode=quiet_mode,
    )
