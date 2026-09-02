"""Executable source shard for the legacy MCP tool seam.

The source is compiled with the original module namespace so public
imports and monkeypatch targets remain tools.mcp_tool-compatible.
"""
import linecache
from pathlib import Path

_SOURCE = r'''


def _make_check_fn(server_name: str):
    """Return a check function that verifies the MCP connection is alive."""

    def _check() -> bool:
        with _lock:
            server = _servers.get(server_name)
            if server is not None and (
                server.session is not None or server._is_recycled_stdio()
            ):
                return True
            # Lazy (schema-cache registered) servers are available: the
            # first real call spawns/connects them (#56832).
            return server_name in _lazy_server_configs

    return _check


# ---------------------------------------------------------------------------
# Discovery & registration
# ---------------------------------------------------------------------------

def _normalize_mcp_input_schema(schema: dict | None) -> dict:
    """Normalize MCP input schemas for LLM tool-calling compatibility.

    MCP servers can emit plain JSON Schema with ``definitions`` /
    ``#/definitions/...`` references.  Kimi / Moonshot rejects that form and
    requires local refs to point into ``#/$defs/...`` instead.  Normalize the
    common draft-07 shape here so MCP tool schemas remain portable across
    OpenAI-compatible providers.

    Additional MCP-server robustness repairs applied recursively:

    * Missing or ``null`` ``type`` on an object-shaped node is coerced to
      ``"object"`` (some servers omit it).  See PR #4897.
    * When an ``object`` node lacks ``properties``, an empty ``properties``
      dict is added so ``required`` entries don't dangle.
    * ``required`` arrays are pruned to only names that exist in
      ``properties``; otherwise Google AI Studio / Gemini 400s with
      ``property is not defined``.  See PR #4651.
    * MCP/Pydantic optional fields commonly arrive as
      ``anyOf: [{...}, {"type": "null"}], default: null``.  Anthropic rejects
      nullable branches in tool input schemas, so nullable unions are collapsed
      to the non-null branch and optionality remains represented solely by the
      parent object's ``required`` list.

    All repairs are provider-agnostic and ideally produce a schema valid on
    OpenAI, Anthropic, Gemini, and Moonshot in one pass.
    """
    if not schema:
        return {"type": "object", "properties": {}}

    def _rewrite_local_refs(node):
        """Walk the schema, promoting legacy ``definitions`` to ``$defs``.

        The promotion is contextual: ``definitions`` is renamed only when it
        appears as a JSON Schema *meta-keyword* (sibling of ``properties`` /
        ``$ref`` at a schema node), never when it appears as the *name of a
        property* (i.e., as a key inside a ``properties`` dict).

        Without this gate, MCP servers that legitimately expose a tool
        parameter named ``definitions`` (e.g. a CI/pipelines tool that uses
        ``definitions`` for an array of pipeline-definition IDs) would have
        that user-facing property name silently rewritten to ``$defs``.
        Anthropic and OpenAI both reject ``$`` in property names
        (``^[a-zA-Z0-9_.-]{1,64}$``), so the whole tool array gets a 400 and
        every conversation breaks.

        The gate works by treating ``properties`` and ``patternProperties``
        specially during descent: we iterate the property-name -> schema map
        directly, leaving the property names verbatim, then recurse into each
        property's schema where ordinary JSON Schema semantics resume (so any
        legitimately-nested ``definitions`` meta-keyword inside a property's
        schema is still promoted).
        """
        if isinstance(node, dict):
            normalized = {}
            for key, value in node.items():
                if key in ("properties", "patternProperties") and isinstance(value, dict):
                    # Keys of this dict are user-facing property names, not
                    # meta-keywords. Preserve them verbatim; recurse only into
                    # each property's schema, where ``definitions`` again has
                    # its JSON Schema meaning.
                    normalized[key] = {
                        prop_name: _rewrite_local_refs(prop_schema)
                        for prop_name, prop_schema in value.items()
                    }
                else:
                    out_key = "$defs" if key == "definitions" else key
                    normalized[out_key] = _rewrite_local_refs(value)
            ref = normalized.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/definitions/"):
                normalized["$ref"] = "#/$defs/" + ref[len("#/definitions/"):]
            return normalized
        if isinstance(node, list):
            return [_rewrite_local_refs(item) for item in node]
        return node

    def _strip_nullable_union(node):
        """Collapse JSON Schema nullable unions to provider-safe non-null schemas.

        Delegates to ``tools.schema_sanitizer.strip_nullable_unions`` so MCP
        ingestion, the Anthropic guard, and the global sanitizer all share one
        implementation. Keeps the ``nullable: true`` hint so runtime argument
        coercion can still map a model-emitted ``"null"`` string to Python
        ``None`` for this optional field.
        """
        from tools.schema_sanitizer import strip_nullable_unions

        return strip_nullable_unions(node, keep_nullable_hint=True)

    def _collapse_const_unions(node):
        """Collapse anyOf/oneOf unions of same-typed consts to property enums.

        Delegates to ``tools.schema_sanitizer.collapse_const_unions``. Runs
        AFTER the nullable strip: single-non-null unions are already collapsed
        by then, and unions of several const branches plus a null branch are
        handled here (consts -> enum, null -> ``nullable: true`` hint).
        Ported from block/goose tool_schema_normalize.rs (Apache-2.0).
        """
        from tools.schema_sanitizer import collapse_const_unions

        return collapse_const_unions(node)

    def _repair_object_shape(node):
        """Recursively repair object-shaped nodes: fill type, prune required."""
        if isinstance(node, list):
            return [_repair_object_shape(item) for item in node]
        if not isinstance(node, dict):
            return node

        repaired = {k: _repair_object_shape(v) for k, v in node.items()}

        # Coerce missing / null type when the shape is clearly an object
        # (has properties or required but no type).
        if not repaired.get("type") and (
            "properties" in repaired or "required" in repaired
        ):
            repaired["type"] = "object"

        if repaired.get("type") == "object":
            # Ensure properties exists so required can reference it safely
            if "properties" not in repaired or not isinstance(
                repaired.get("properties"), dict
            ):
                repaired["properties"] = {} if "properties" not in repaired else repaired["properties"]
                if not isinstance(repaired.get("properties"), dict):
                    repaired["properties"] = {}

            # Prune required to only include names that exist in properties
            required = repaired.get("required")
            if isinstance(required, list):
                props = repaired.get("properties") or {}
                valid = [r for r in required if isinstance(r, str) and r in props]
                if len(valid) != len(required):
                    if valid:
                        repaired["required"] = valid
                    else:
                        repaired.pop("required", None)

        return repaired

    normalized = _rewrite_local_refs(schema)
    normalized = _strip_nullable_union(normalized)
    normalized = _collapse_const_unions(normalized)
    normalized = _repair_object_shape(normalized)

    # Ensure top-level is a well-formed object schema
    if not isinstance(normalized, dict):
        return {"type": "object", "properties": {}}
    if normalized.get("type") == "object" and "properties" not in normalized:
        normalized = {**normalized, "properties": {}}

    return normalized


def sanitize_mcp_name_component(value: str) -> str:
    """Return an MCP name component safe for tool and prefix generation.

    Preserves Hermes's historical behavior of converting hyphens to
    underscores, and also replaces any other character outside
    ``[A-Za-z0-9_]`` with ``_`` so generated tool names are compatible with
    provider validation rules.
    """
    return re.sub(r"[^A-Za-z0-9_]", "_", str(value or ""))


# Native MCP tool-name prefix. Hermes uses the ``mcp__<server>__<tool>``
# convention shared by Claude Code, Codex, and OpenCode (anomalyco/opencode
# #33533). The double-underscore delimiter disambiguates the server/tool
# boundary even when either component contains underscores, and matches the
# naming models are trained on. It also aligns native registration with the
# Anthropic-OAuth wire form (``_MCP_TOOL_PREFIX`` in anthropic_adapter.py),
# removing the single->double rewrite that path previously had to perform.
MCP_TOOL_NAME_PREFIX = "mcp__"
_MCP_NAME_DELIM = "__"


def mcp_prefixed_tool_name(server_name: str, tool_name: str) -> str:
    """Build the registry/wire name for an MCP tool.

    Produces ``mcp__<sanitizedServer>__<sanitizedTool>``.
    """
    safe_server = sanitize_mcp_name_component(server_name)
    safe_tool = sanitize_mcp_name_component(tool_name)
    return f"{MCP_TOOL_NAME_PREFIX}{safe_server}{_MCP_NAME_DELIM}{safe_tool}"


def _convert_mcp_schema(server_name: str, mcp_tool) -> dict:
    """Convert an MCP tool listing to the Hermes registry schema format.

    Args:
        server_name: The logical server name for prefixing.
        mcp_tool:    An MCP ``Tool`` object with ``.name``, ``.description``,
                     and ``.input_schema`` (``.inputSchema`` before mcp 2.0).

    Returns:
        A dict suitable for ``registry.register(schema=...)``.
    """
    prefixed_name = mcp_prefixed_tool_name(server_name, mcp_tool.name)
    return {
        "name": prefixed_name,
        "description": strip_unicode_tags(
            mcp_tool.description or f"MCP tool {mcp_tool.name} from {server_name}"
        ),
        "parameters": _normalize_mcp_input_schema(
            mcp_field(mcp_tool, "input_schema", "inputSchema")
        ),
    }



def _build_utility_schemas(server_name: str) -> List[dict]:
    """Build schemas for the MCP utility tools (resources & prompts).

    Returns a list of (schema, handler_factory_name) tuples encoded as dicts
    with keys: schema, handler_key.
    """
    return [
        {
            "schema": {
                "name": mcp_prefixed_tool_name(server_name, "list_resources"),
                "description": f"List available resources from MCP server '{server_name}'",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
            "handler_key": "list_resources",
        },
        {
            "schema": {
                "name": mcp_prefixed_tool_name(server_name, "read_resource"),
                "description": f"Read a resource by URI from MCP server '{server_name}'",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "uri": {
                            "type": "string",
                            "description": "URI of the resource to read",
                        },
                    },
                    "required": ["uri"],
                },
            },
            "handler_key": "read_resource",
        },
        {
            "schema": {
                "name": mcp_prefixed_tool_name(server_name, "list_prompts"),
                "description": f"List available prompts from MCP server '{server_name}'",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
            "handler_key": "list_prompts",
        },
        {
            "schema": {
                "name": mcp_prefixed_tool_name(server_name, "get_prompt"),
                "description": f"Get a prompt by name from MCP server '{server_name}'",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Name of the prompt to retrieve",
                        },
                        "arguments": {
                            "type": "object",
                            "description": "Optional arguments to pass to the prompt",
                            "properties": {},
                            "additionalProperties": True,
                        },
                    },
                    "required": ["name"],
                },
            },
            "handler_key": "get_prompt",
        },
    ]


def _normalize_name_filter(value: Any, label: str) -> set[str]:
    """Normalize include/exclude config to a set of tool-name patterns.

    Entries may be exact tool names or fnmatch-style globs
    (``*_radar_*``, ``get_zones_*``). Matching happens in
    :func:`matches_name_filter`.
    """
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    if isinstance(value, (list, tuple, set)):
        return {str(item) for item in value}
    logger.warning("MCP config %s must be a string or list of strings; ignoring %r", label, value)
    return set()


def matches_name_filter(tool_name: str, patterns: set[str]) -> bool:
    """True if ``tool_name`` matches any entry in ``patterns``.

    Exact names match literally; entries containing fnmatch metacharacters
    (``*``, ``?``, ``[``) match as case-sensitive globs — the same pattern
    semantics as ``approvals.deny``. Exact membership is checked first so
    large literal lists stay O(1).
    """
    if not patterns:
        return False
    if tool_name in patterns:
        return True
    return any(
        fnmatch.fnmatchcase(tool_name, p)
        for p in patterns
        if "*" in p or "?" in p or "[" in p
    )


def _parse_boolish(value: Any, default: bool = True) -> bool:
    """Parse a bool-like config value with safe fallback."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    logger.warning("MCP config expected a boolean-ish value, got %r; using default=%s", value, default)
    return default


def _get_lifecycle_seconds(config: dict, key: str) -> Optional[float]:
    """Return an optional positive lifecycle timeout from top-level/nested config."""
    raw = config.get(key)
    lifecycle = config.get("lifecycle")
    if raw is None and isinstance(lifecycle, dict):
        raw = lifecycle.get(key)
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        logger.warning("MCP config %s must be a number of seconds; ignoring %r", key, raw)
        return None
    if seconds == 0:
        return None
    if seconds < 0:
        logger.warning("MCP config %s must be positive; ignoring %r", key, raw)
        return None
    return seconds


_UTILITY_CAPABILITY_METHODS = {
    "list_resources": "list_resources",
    "read_resource": "read_resource",
    "list_prompts": "list_prompts",
    "get_prompt": "get_prompt",
}

# Maps each utility handler to the MCP capability key that must be non-None
# on the server's ``initialize`` response for the handler to be registered.
# Source of truth: MCP spec — capabilities.resources / capabilities.prompts
# are present on the response only when the server actually implements
# those request families. Without this gate, tools-only servers (e.g.
# Context7 @upstash/context7-mcp, which advertises only ``tools``) had
# all four utility stubs registered and every model call to them came
# back with JSON-RPC ``-32601 Method not found``, which made the model
# conclude the server was broken even when the real tools worked. See
# #18051.
_UTILITY_CAPABILITY_ATTRS = {
    "list_resources": "resources",
    "read_resource": "resources",
    "list_prompts": "prompts",
    "get_prompt": "prompts",
}


def _track_mcp_tool_server(tool_name: str, server_name: str) -> None:
    """Remember the exact raw MCP server that registered *tool_name*."""
    with _lock:
        _mcp_tool_server_names[tool_name] = server_name


def _forget_mcp_tool_server(tool_name: str) -> None:
    """Forget MCP server provenance for a deregistered tool."""
    with _lock:
        _mcp_tool_server_names.pop(tool_name, None)


def _select_utility_schemas(server_name: str, server: MCPServerTask, config: dict) -> List[dict]:
    """Select utility schemas based on config and server capabilities."""
    tools_filter = config.get("tools") or {}
    resources_enabled = _parse_boolish(tools_filter.get("resources"), default=True)
    prompts_enabled = _parse_boolish(tools_filter.get("prompts"), default=True)

    # ``initialize_result.capabilities`` is the source of truth: its sub-objects
    # (``resources``, ``prompts``) are non-None iff the server advertises that
    # request family. ``hasattr(server.session, ...)`` was the old gate but
    # ClientSession always has the four method attributes defined on the class,
    # so it never filtered anything.
    advertised_caps = None
    init_result = getattr(server, "initialize_result", None)
    if init_result is not None:
        advertised_caps = getattr(init_result, "capabilities", None)

    selected: List[dict] = []
    for entry in _build_utility_schemas(server_name):
        handler_key = entry["handler_key"]
        if handler_key in {"list_resources", "read_resource"} and not resources_enabled:
            logger.debug("MCP server '%s': skipping utility '%s' (resources disabled)", server_name, handler_key)
            continue
        if handler_key in {"list_prompts", "get_prompt"} and not prompts_enabled:
            logger.debug("MCP server '%s': skipping utility '%s' (prompts disabled)", server_name, handler_key)
            continue

        # Preferred gate: check the server's advertised capabilities. Skip
        # if the capability is explicitly not advertised.
        if advertised_caps is not None:
            cap_attr = _UTILITY_CAPABILITY_ATTRS[handler_key]
            if getattr(advertised_caps, cap_attr, None) is None:
                logger.debug(
                    "MCP server '%s': skipping utility '%s' "
                    "(server does not advertise '%s' capability)",
                    server_name,
                    handler_key,
                    cap_attr,
                )
                continue
        else:
            # Legacy fallback for test fixtures or older code paths where
            # initialize_result wasn't captured. Preserves the old behavior
            # of registering every stub in that case rather than regressing
            # any server that was working before this fix.
            required_method = _UTILITY_CAPABILITY_METHODS[handler_key]
            if not hasattr(server.session, required_method):
                logger.debug(
                    "MCP server '%s': skipping utility '%s' (session lacks %s)",
                    server_name,
                    handler_key,
                    required_method,
                )
                continue
        selected.append(entry)
    return selected


def _existing_tool_names() -> List[str]:
    """Return tool names for all currently connected servers."""
    names: List[str] = []
    for _sname, server in _servers.items():
        if hasattr(server, "_registered_tool_names"):
            names.extend(server._registered_tool_names)
            continue
        for mcp_tool in server._tools:
            schema = _convert_mcp_schema(server.name, mcp_tool)
            names.append(schema["name"])
    # Lazy servers registered from the schema cache have no MCPServerTask
    # yet — their tools live in the registry only (#56832).
    with _lock:
        lazy_names = [
            n
            for sname, tool_names in _lazy_server_tool_names.items()
            if sname not in _servers
            for n in tool_names
        ]
    names.extend(lazy_names)
    return names


def _register_server_tools(name: str, server: MCPServerTask, config: dict) -> List[str]:
    """Register tools from an already-connected server into the registry.

    Handles include/exclude filtering and utility tools. Toolset resolution
    for ``mcp-{server}`` and raw server-name aliases is derived from the live
    registry, rather than mutating ``toolsets.TOOLSETS`` at runtime.

    Lossy provider-safe name normalization can map distinct raw names to the
    same registry name (for example ``read-file`` and ``read_file``). Such
    collisions fail closed: every ambiguous entry is skipped rather than
    selecting an arbitrary handler.

    Used by both initial discovery and dynamic refresh (list_changed).

    Returns:
        List of registered prefixed tool names.
    """
    from tools.registry import registry

    registered_names: List[str] = []
    toolset_name = f"mcp-{name}"

    # Selective tool loading: honour include/exclude lists from config.
    # Rules (matching issue #690 spec, extended with glob support):
    #   tools.include — whitelist: only matching tool names are registered
    #   tools.exclude — blacklist: all tools EXCEPT matching ones are registered
    #   entries may be exact names or fnmatch globs (e.g. "*_radar_*")
    #   include takes precedence over exclude
    #   include: [] → register nothing (an explicit empty whitelist, as
    #   written by the install checklist's "uncheck everything" path)
    #   Neither set → register all tools (backward-compatible default)
    tools_filter = config.get("tools") or {}
    include_raw = tools_filter.get("include")
    include_set = _normalize_name_filter(
        include_raw, f"mcp_servers.{name}.tools.include"
    )
    include_active = isinstance(include_raw, (str, list, tuple, set))
    exclude_set = _normalize_name_filter(
        tools_filter.get("exclude"), f"mcp_servers.{name}.tools.exclude"
    )

    def _should_register(tool_name: str) -> bool:
        if include_active:
            return matches_name_filter(tool_name, include_set)
        if exclude_set:
            return not matches_name_filter(tool_name, exclude_set)
        return True

    check_fn = _make_check_fn(name)
    candidates: List[dict] = []

    # Trust-tier metadata (security boundary): capture the server's
    # configured trust tier and each tool's readOnlyHint annotation NOW,
    # at discovery, so the call-time gate in _make_tool_handler classifies
    # from data we control rather than re-reading server-supplied state.
    _record_tool_trust_metadata(name, config, server._tools)

    for mcp_tool in server._tools:
        if not _should_register(mcp_tool.name):
            logger.debug(
                "MCP server '%s': skipping tool '%s' (filtered by config)",
                name,
                mcp_tool.name,
            )
            continue

        _scan_mcp_description(name, mcp_tool.name, mcp_tool.description or "")
        schema = _convert_mcp_schema(name, mcp_tool)
        candidates.append(
            {
                "registry_name": schema["name"],
                "origin": f"tool {mcp_tool.name!r}",
                "schema": schema,
                "handler": _make_tool_handler(
                    name, mcp_tool.name, server.tool_timeout
                ),
                "check_fn": check_fn,
            }
        )

    # Generated resource/prompt utility tools share the same namespace as raw
    # MCP tools, so they must participate in the same collision preflight.
    handler_factories = {
        "list_resources": _make_list_resources_handler,
        "read_resource": _make_read_resource_handler,
        "list_prompts": _make_list_prompts_handler,
        "get_prompt": _make_get_prompt_handler,
    }
    for entry in _select_utility_schemas(name, server, config):
        schema = entry["schema"]
        handler_key = entry["handler_key"]
        candidates.append(
            {
                "registry_name": schema["name"],
                "origin": f"generated utility {handler_key!r}",
                "schema": schema,
                "handler": handler_factories[handler_key](
                    name, server.tool_timeout
                ),
                "check_fn": check_fn,
            }
        )

    # Exact duplicate rows from a server are harmless but should not inflate
    # counts. Distinct origins that collapse to one normalized name are unsafe.
    unique_candidates: List[dict] = []
    seen_candidates: set[tuple[str, str]] = set()
    origins_by_name: Dict[str, set[str]] = {}
    for candidate in candidates:
        key = (candidate["registry_name"], candidate["origin"])
        if key in seen_candidates:
            logger.debug(
                "MCP server '%s': duplicate registration candidate %s for '%s'; "
                "keeping one",
                name,
                candidate["origin"],
                candidate["registry_name"],
            )
            continue
        seen_candidates.add(key)
        unique_candidates.append(candidate)
        origins_by_name.setdefault(candidate["registry_name"], set()).add(
            candidate["origin"]
        )

    # A generated resource/prompt utility that normalizes onto a server-native
    # tool's name must not knock that native tool out of the registry: the
    # native tool is the capability the user connected the server for, while the
    # generated utility (read_resource/list_resources/list_prompts/get_prompt)
    # is optional sugar that only matters when the server exposes no such tool
    # of its own (#87112). Resolve that specific collision in favour of the
    # native tool — keep it, drop the shadowed utility — and fall back to the
    # conservative skip-everything only for genuinely ambiguous collisions (two
    # or more native tools normalizing to one name, which we cannot
    # disambiguate). The four utility keys are distinct, so a colliding set
    # holds at most one utility origin.
    ambiguous_names: Dict[str, List[str]] = {}
    shadowed_utilities: set[tuple[str, str]] = set()
    for registry_name, origins in origins_by_name.items():
        if len(origins) <= 1:
            continue
        utility_origins = sorted(
            o for o in origins if o.startswith("generated utility ")
        )
        native_origins = sorted(origins - set(utility_origins))
        if len(native_origins) == 1 and utility_origins:
            for util_origin in utility_origins:
                shadowed_utilities.add((registry_name, util_origin))
            logger.info(
                "MCP server '%s': generated utility %s normalizes onto "
                "server-native %s — keeping the native tool and dropping the "
                "utility (the utility only applies when the server has no such "
                "tool of its own)",
                name,
                ", ".join(utility_origins),
                native_origins[0],
            )
            continue
        ambiguous_names[registry_name] = sorted(origins)

    for registry_name, origins in sorted(ambiguous_names.items()):
        logger.error(
            "MCP server '%s': name normalization collision for '%s' from %s; "
            "skipping every colliding entry instead of choosing an arbitrary "
            "handler",
            name,
            registry_name,
            ", ".join(origins),
        )

    for candidate in unique_candidates:
        registry_name = candidate["registry_name"]
        if registry_name in ambiguous_names:
            continue
        if (registry_name, candidate["origin"]) in shadowed_utilities:
            continue

        existing_toolset = registry.get_toolset_for_tool(registry_name)
        if existing_toolset and existing_toolset != toolset_name:
            if existing_toolset.startswith("mcp-"):
                logger.error(
                    "MCP server '%s': %s normalizes to '%s', already owned by "
                    "MCP toolset '%s' — skipping to preserve the existing owner",
                    name,
                    candidate["origin"],
                    registry_name,
                    existing_toolset,
                )
            else:
                logger.warning(
                    "MCP server '%s': %s (→ '%s') collides with built-in tool "
                    "in toolset '%s' — skipping to preserve built-in",
                    name,
                    candidate["origin"],
                    registry_name,
                    existing_toolset,
                )
            continue

        registry.register(
            name=registry_name,
            toolset=toolset_name,
            schema=candidate["schema"],
            handler=candidate["handler"],
            check_fn=candidate["check_fn"],
            is_async=False,
            description=candidate["schema"]["description"],
        )

        # The pre-check above is advisory only. Multiple servers connect in
        # parallel, so ToolRegistry.register() is the atomic ownership gate.
        if registry.get_toolset_for_tool(registry_name) != toolset_name:
            logger.error(
                "MCP server '%s': registration of %s as '%s' was rejected by "
                "the registry; skipping provenance/count updates",
                name,
                candidate["origin"],
                registry_name,
            )
            continue

        _track_mcp_tool_server(registry_name, name)
        registered_names.append(registry_name)

    if registered_names:
        registry.register_toolset_alias(name, toolset_name)
        # Write-through (#56832): refresh the on-disk schema cache after a
        # live connect so the next startup can lazily register this server
        # without spawning it. Cache failures never break registration.
        try:
            from tools.mcp_schema_cache import config_fingerprint, write_cache_entry

            tools_payload: List[dict] = []
            for mcp_tool in server._tools:
                if not _should_register(mcp_tool.name):
                    continue
                schema_obj = getattr(mcp_tool, "inputSchema", None)
                tools_payload.append({
                    "name": mcp_tool.name,
                    "description": mcp_tool.description or "",
                    "inputSchema": schema_obj if isinstance(schema_obj, dict) else {},
                    # Persist the trust-relevant annotation so the lazy
                    # (cache-registered) path gates identically on next
                    # startup without spawning the server.
                    "annotations": {
                        "readOnlyHint": _annotation_read_only_hint(mcp_tool),
                    },
                })
            utility_payload = [
                {"schema": entry["schema"], "handler_key": entry["handler_key"]}
                for entry in _select_utility_schemas(name, server, config)
            ]
            write_cache_entry(
                name,
                config_fingerprint(config),
                tools=tools_payload,
                utility_tools=utility_payload,
                ttl_ms=(getattr(server, "_list_cache_meta", None) or {}).get("ttl_ms"),
                cache_scope=(getattr(server, "_list_cache_meta", None) or {}).get("cache_scope"),
            )
        except Exception as exc:
            logger.debug("MCP schema cache write failed for '%s': %s", name, exc)

    return registered_names


class _CachedMCPTool:
    """Minimal stand-in for MCP Tool objects loaded from the schema cache."""

    __slots__ = ("name", "description", "inputSchema")

    def __init__(self, name: str, description: str, inputSchema: dict):
        self.name = name
        self.description = description
        self.inputSchema = inputSchema or {}


def _register_from_cache_sync(name: str, config: dict, entry: dict) -> List[str]:
    """Register a server's tools from a cached manifest, no child process.

    Lazy startup (#56832, design by Vansh5632): tools appear in the registry
    immediately; the first real call routes through
    ``_get_connected_server_for_call`` → ``_ensure_lazy_server_connected``.
    """
    from tools.registry import registry
    from tools.mcp_schema_cache import (
        config_fingerprint,
        tools_from_cache_entry,
        utility_tools_from_cache_entry,
    )

    registered_names: List[str] = []
    toolset_name = f"mcp-{name}"
    fingerprint = config_fingerprint(config)
    tool_timeout = _resolve_tool_timeout(config)
    tools_filter = config.get("tools") or {}
    include_raw = tools_filter.get("include")
    include_set = _normalize_name_filter(
        include_raw, f"mcp_servers.{name}.tools.include"
    )
    # include: [] is an explicit empty whitelist (register nothing) — see the
    # live discovery path above for the full filter rules.
    include_active = isinstance(include_raw, (str, list, tuple, set))
    exclude_set = _normalize_name_filter(
        tools_filter.get("exclude"), f"mcp_servers.{name}.tools.exclude"
    )

    def _should_register(tool_name: str) -> bool:
        if include_active:
            return matches_name_filter(tool_name, include_set)
        if exclude_set:
            return not matches_name_filter(tool_name, exclude_set)
        return True

    check_fn = _make_check_fn(name)
    # Trust-tier metadata for the lazy path: the cached manifest carries
    # each tool's readOnlyHint (written by the live discovery path), and
    # trust comes from operator config. Recording it before registration
    # keeps the call-time gate identical whether the server was spawned
    # live or registered from cache. Missing "annotations" in older cache
    # files fails closed to write-capable.
    cached_tool_objs = [
        SimpleNamespace(
            name=raw.get("name"),
            annotations=raw.get("annotations")
            if isinstance(raw.get("annotations"), dict) else None,
        )
        for raw in tools_from_cache_entry(entry)
        if isinstance(raw, dict) and raw.get("name")
    ]
    _record_tool_trust_metadata(name, config, cached_tool_objs)
    for raw in tools_from_cache_entry(entry):
        if not isinstance(raw, dict):
            continue
        raw_name = raw.get("name")
        if not raw_name or not _should_register(raw_name):
            continue
        raw_schema = raw.get("inputSchema")
        mcp_tool = _CachedMCPTool(
            raw_name,
            raw.get("description") or "",
            raw_schema if isinstance(raw_schema, dict) else {},
        )
        # Defense-in-depth: the cache file is user-writable JSON, so run the
        # same injection scan the eager discovery path applies.
        _scan_mcp_description(name, mcp_tool.name, mcp_tool.description or "")
        schema = _convert_mcp_schema(name, mcp_tool)
        registry_name = schema["name"]
        existing_toolset = registry.get_toolset_for_tool(registry_name)
        if existing_toolset and existing_toolset != toolset_name:
            logger.warning(
                "MCP server '%s' (lazy): cached tool '%s' collides with "
                "toolset '%s' — skipping",
                name, registry_name, existing_toolset,
            )
            continue
        registry.register(
            name=registry_name,
            toolset=toolset_name,
            schema=schema,
            handler=_make_tool_handler(name, raw_name, tool_timeout),
            check_fn=check_fn,
            is_async=False,
            description=schema["description"],
        )
        if registry.get_toolset_for_tool(registry_name) != toolset_name:
            continue
        _track_mcp_tool_server(registry_name, name)
        registered_names.append(registry_name)

    handler_factories = {
        "list_resources": _make_list_resources_handler,
        "read_resource": _make_read_resource_handler,
        "list_prompts": _make_list_prompts_handler,
        "get_prompt": _make_get_prompt_handler,
    }
    for raw in utility_tools_from_cache_entry(entry):
        if not isinstance(raw, dict):
            continue
        schema = raw.get("schema")
        handler_key = raw.get("handler_key")
        if not isinstance(schema, dict) or handler_key not in handler_factories:
            continue
        util_name = schema.get("name") or ""
        if not util_name:
            continue
        existing_toolset = registry.get_toolset_for_tool(util_name)
        if existing_toolset and existing_toolset != toolset_name:
            continue
        registry.register(
            name=util_name,
            toolset=toolset_name,
            schema=schema,
            handler=handler_factories[handler_key](name, tool_timeout),
            check_fn=check_fn,
            is_async=False,
            description=schema.get("description") or "",
        )
        if registry.get_toolset_for_tool(util_name) != toolset_name:
            continue
        _track_mcp_tool_server(util_name, name)
        registered_names.append(util_name)

    if registered_names:
        registry.register_toolset_alias(name, toolset_name)
        with _lock:
            _lazy_server_configs[name] = dict(config)
            _lazy_server_fingerprints[name] = fingerprint
            _lazy_server_tool_names[name] = list(registered_names)
        logger.info(
            "MCP server '%s' (lazy): registered %d tool(s) from schema cache",
            name, len(registered_names),
        )
    return registered_names

async def _discover_and_register_server(name: str, config: dict) -> List[str]:
    """Connect to a single MCP server, discover tools, and register them.

    Returns list of registered tool names.
    """
    connect_timeout = config.get("connect_timeout", _DEFAULT_CONNECT_TIMEOUT)
    # List-based claim (not a ``nonlocal`` rebind): the claim callback runs
    # inside ``_connect_server`` while this frame is suspended, and appending
    # keeps type narrowing intact for the module's other ``server`` locals.
    claimed: List[MCPServerTask] = []

    def _claim_server(created: MCPServerTask) -> None:
        claimed.append(created)

    claim_token = _connect_server_claim.set(_claim_server)
    try:
        server = await asyncio.wait_for(
            _connect_server(name, config),
            timeout=connect_timeout,
        )
    except BaseException:
        server = claimed[0] if claimed else None
        task = server._task if server is not None else None
        task_cancelling = (
            task.cancelling()
            if task is not None and hasattr(task, "cancelling")
            else 0
        )
        if (
            server is not None
            and server._error is not None
            and task is not None
            and not task.done()
            and not task_cancelling
        ):
            # Recoverable park: the run task deliberately stays alive to
            # self-probe, so adopt it into the registry for shutdown/revival.
            with _lock:
                _servers[name] = server
        elif server is not None:
            await server.shutdown()
        raise
    finally:
        _connect_server_claim.reset(claim_token)

    with _lock:
        _server_connecting.discard(name)
        _server_connect_errors.pop(name, None)
        _servers[name] = server

    registered_names = _register_server_tools(name, server, config)
    server._registered_tool_names = list(registered_names)

    transport_type = "HTTP" if "url" in config else "stdio"
    logger.info(
        "MCP server '%s' (%s): registered %d tool(s): %s",
        name, transport_type, len(registered_names),
        ", ".join(registered_names),
    )
    return registered_names


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def register_mcp_servers(servers: Dict[str, dict]) -> List[str]:
    """Connect to explicit MCP servers and register their tools.

    Idempotent for already-connected server names. Servers with
    ``enabled: false`` are skipped without disconnecting existing sessions.

    Args:
        servers: Mapping of ``{server_name: server_config}``.

    Returns:
        List of all currently registered MCP tool names.
    """
    if not _ensure_mcp_sdk():
        logger.debug("MCP SDK not available -- skipping explicit MCP registration")
        return []

    servers = _filter_suspicious_mcp_servers(servers)
    if not servers:
        logger.debug("No explicit MCP servers provided")
        return []

    # Only attempt servers that aren't already connected (or currently
    # connecting) and are enabled.  Checking ``_server_connecting`` prevents
    # duplicate subprocess spawns when ``discover_mcp_tools()`` is called
    # from multiple entry-points before the first batch finishes (#58862).
    with _lock:
        connecting = set(_server_connecting)
        new_servers = {
            k: v
            for k, v in servers.items()
            if k not in _servers
            and k not in connecting
            # Servers already lazily registered from the schema cache are
            # not re-registered; they connect on first tool use (#56832).
            and k not in _lazy_server_configs
            and _parse_boolish(v.get("enabled", True), default=True)
            # Skip a server still serving its post-failure backoff. Without
            # this, a server that fails to connect (and is therefore never
            # recorded in ``_servers``) would be re-spawned on every worker
            # session's discovery pass -- the #50394 restart storm. The
            # cooldown is cleared automatically on the next successful
            # connect or by a manual /mcp refresh.
            and not _connect_cooldown_active(k)
        }
        # Cached entries with no live session are parked or mid-reconnect.
        # Their tools are deregistered, so nothing else can reach
        # _signal_reconnect — without this nudge a new session silently
        # waits up to _PARKED_RETRY_INTERVAL for the next self-probe
        # (#50170). Wake them now so their tools come back promptly.
        stale_cached = [
            _servers[k]
            for k in servers
            if k in _servers and getattr(_servers[k], "session", None) is None
        ]
        _server_connecting.update(new_servers)
        for srv_name in new_servers:
            _server_connect_errors.pop(srv_name, None)
        # Track which servers opt-in to parallel tool calls (idempotent).
        for srv_name, srv_cfg in servers.items():
            if _parse_boolish(srv_cfg.get("supports_parallel_tool_calls", False), default=False):
                _parallel_safe_servers.add(srv_name)
            else:
                _parallel_safe_servers.discard(srv_name)

    for srv in stale_cached:
        _signal_reconnect(srv)

    if not new_servers:
        return _existing_tool_names()

    # Lazy startup (#56832): servers gated with ``lazy: true`` whose config
    # fingerprint matches a valid on-disk schema-cache entry register their
    # tools from cache WITHOUT spawning/connecting. A missing or stale cache
    # entry falls back to the normal eager connect below (which write-through
    # refreshes the cache for next time).
    eager_servers: Dict[str, dict] = dict(new_servers)
    lazy_registered = 0
    lazy_server_count = 0
    try:
        from tools.mcp_schema_cache import config_fingerprint, get_cached_entry
    except Exception:  # pragma: no cover - cache module missing
        config_fingerprint = None  # type: ignore[assignment]
        get_cached_entry = None  # type: ignore[assignment]
    if config_fingerprint is not None and get_cached_entry is not None:
        for name, cfg in new_servers.items():
            if not _resolve_server_lazy(name, cfg):
                continue
            entry = get_cached_entry(name, config_fingerprint(cfg))
            if not entry:
                continue
            with _lock:
                _server_connecting.discard(name)
            try:
                names = _register_from_cache_sync(name, cfg, entry)
            except Exception as exc:
                logger.warning(
                    "Failed lazy MCP registration for '%s': %s", name, exc,
                )
                with _lock:
                    _server_connecting.add(name)
                continue
            eager_servers.pop(name, None)
            lazy_registered += len(names)
            lazy_server_count += 1
    new_servers = eager_servers

    if not new_servers:
        if lazy_registered:
            logger.info(
                "MCP: registered %d lazy tool(s) from schema cache "
                "(no processes spawned)",
                lazy_registered,
            )
        return _existing_tool_names()

    # Start the background event loop for MCP connections
    _ensure_mcp_loop()

    async def _discover_one(name: str, cfg: dict) -> List[str]:
        """Connect to a single server and return its registered tool names."""
        return await _discover_and_register_server(name, cfg)

    async def _discover_all():
        server_names = list(new_servers.keys())
        # Connect to all servers in PARALLEL
        results = await asyncio.gather(
            *(_discover_one(name, cfg) for name, cfg in new_servers.items()),
            return_exceptions=True,
        )
        for name, result in zip(server_names, results):
            if isinstance(result, BaseException):
                command = new_servers.get(name, {}).get("command")
                message = _format_connect_error(result)
                with _lock:
                    _server_connecting.discard(name)
                    _server_connect_errors[name] = message
                    # Arm the per-server backoff so the next discovery pass
                    # doesn't immediately re-spawn this failing server
                    # (#50394). Isolated to this server -- healthy servers
                    # in the same batch are unaffected.
                    _record_connect_failure(name)
                logger.warning(
                    "Failed to connect to MCP server '%s'%s: %s",
                    name,
                    f" (command={command})" if command else "",
                    message,
                )
            else:
                with _lock:
                    _server_connecting.discard(name)
                    _server_connect_errors.pop(name, None)
                    _clear_connect_failure(name)

    # Per-server timeouts are handled inside _discover_and_register_server.
    # The outer timeout is generous: 120s total for parallel discovery.
    #
    # Temporarily clear the interrupt flag on the current thread so that MCP
    # discovery is never cancelled by a stale interrupt from a prior agent
    # session (executor threads get reused and may carry old interrupt state).
    from tools.interrupt import is_interrupted as _is_interrupted, set_interrupt as _set_interrupt
    _was_interrupted = _is_interrupted()
    if _was_interrupted:
        _set_interrupt(False)
    try:
        _run_on_mcp_loop(_discover_all, timeout=120)
    except (TimeoutError, InterruptedError) as _e:
        # When the outer timeout fires or the user interrupts,
        # _discover_all's gather may not have finished, leaving
        # entries stranded in _server_connecting.  Those stale
        # entries would block future reconnection attempts (#58862).
        with _lock:
            stale = [n for n in new_servers if n in _server_connecting]
            if stale:
                logger.warning(
                    "MCP discovery %s while %d server(s) were still "
                    "connecting; clearing stale connecting set: %s",
                    "timed out" if isinstance(_e, TimeoutError) else "interrupted",
                    len(stale),
                    ", ".join(stale),
                )
                _server_connecting.difference_update(stale)
                for _sn in stale:
                    _server_connect_errors.setdefault(
                        _sn,
                        f"Connection attempt {'timed out' if isinstance(_e, TimeoutError) else 'interrupted'} during discovery",
                    )
        raise
    finally:
        if _was_interrupted:
            _set_interrupt(True)

    # Log a summary so ACP callers get visibility into what was registered.
    with _lock:
        connected = [
            n
            for n in new_servers
            if n in _servers and n not in _server_connect_errors
        ]
        new_tool_count = sum(
            len(getattr(_servers[n], "_registered_tool_names", []))
            for n in connected
        )
    failed = len(new_servers) - len(connected)
    new_tool_count += lazy_registered
    connected_count = len(connected) + lazy_server_count
    if new_tool_count or failed:
        summary = f"MCP: registered {new_tool_count} tool(s) from {connected_count} server(s)"
        if failed:
            summary += f" ({failed} failed)"
        logger.info(summary)

    return _existing_tool_names()


def discover_mcp_tools() -> List[str]:
    """Entry point: load config, connect to MCP servers, register tools.

    Called from ``model_tools`` after ``discover_builtin_tools()``. Safe to call even when
    the ``mcp`` package is not installed (returns empty list).

    Idempotent for already-connected servers. If some servers failed on a
    previous call, only the missing ones are retried.

    Returns:
        List of all registered MCP tool names.
    """
    servers = _load_mcp_config()
    if not servers:
        logger.debug("No MCP servers configured")
        return []

    # SDK import is deferred to HERE so a config with zero MCP servers (the
    # default) never pays the ~260ms `mcp` import on CLI startup.
    if not _ensure_mcp_sdk():
        logger.debug("MCP SDK not available -- skipping MCP tool discovery")
        return []

    # Cross-process discovery guard (#62771). A lock loser waits for
    # the holder, then performs its own process-local discovery. If locking is
    # unavailable or the bounded wait expires, preserve the previous
    # fail-soft behavior by running discovery unguarded.
    cookie = _try_acquire_mcp_discovery_lock()
    if cookie is None:
        logger.debug(
            "Another process holds MCP discovery lock -- retrying with backoff"
        )
        for _ in range(_MCP_DISCOVERY_LOCK_MAX_RETRIES):
            time.sleep(_MCP_DISCOVERY_LOCK_RETRY_DELAY_S)
            cookie = _try_acquire_mcp_discovery_lock()
            if cookie is not None:
                break

        if cookie is None:
            logger.warning(
                "MCP discovery lock still held after %d retries -- "
                "running discovery unguarded",
                _MCP_DISCOVERY_LOCK_MAX_RETRIES,
            )
        elif cookie is not _LOCK_UNAVAILABLE:
            logger.debug("Retry succeeded -- acquired MCP discovery lock")

    try:
        with _lock:
            connecting = set(_server_connecting)
            new_server_names = [
                name
                for name, cfg in servers.items()
                if name not in _servers
                and name not in connecting
                and _parse_boolish(cfg.get("enabled", True), default=True)
            ]

        tool_names = register_mcp_servers(servers)
        if not new_server_names:
            return tool_names

        with _lock:
            connected_server_names = [
                name
                for name in new_server_names
                if name in _servers and name not in _server_connect_errors
            ]
            new_tool_count = sum(
                len(getattr(_servers[name], "_registered_tool_names", []))
                for name in connected_server_names
            )

        failed_count = len(new_server_names) - len(connected_server_names)
        if new_tool_count or failed_count:
            summary = f"  MCP: {new_tool_count} tool(s) from {len(connected_server_names)} server(s)"
            if failed_count:
                summary += f" ({failed_count} failed)"
            logger.info(summary)

        return tool_names

    finally:
        if cookie not in (None, _LOCK_UNAVAILABLE):
            cookie.release()

def is_mcp_tool_parallel_safe(tool_name: str) -> bool:
    """Check if an MCP tool belongs to a server that supports parallel tool calls.

    MCP tool names follow the pattern ``mcp__{server}__{tool}``, but that
    string shape is ambiguous when server names contain underscores. Use the
    exact server provenance captured at registration time rather than prefix
    matching, then check whether that server's config includes
    ``supports_parallel_tool_calls: true``.

    Returns False for non-MCP tools or tools from servers without the flag.
    """
    if not tool_name.startswith(MCP_TOOL_NAME_PREFIX):
        return False
    with _lock:
        server_name = _mcp_tool_server_names.get(tool_name)
        return bool(server_name and server_name in _parallel_safe_servers)


def get_mcp_status() -> List[dict]:
    """Return status of all configured MCP servers for banner display.

    Returns a list of dicts with keys: name, transport, tools, connected,
    disabled, and status. Includes connected servers, disabled servers,
    in-flight connection attempts, recorded failures, and servers that are
    configured but have not been started in this process yet.
    """
    result: List[dict] = []

    # Get configured servers from config
    configured = _load_mcp_config()
    if not configured:
        return result

    with _lock:
        active_servers = dict(_servers)
        connecting = set(_server_connecting)
        connect_errors = dict(_server_connect_errors)

    for name, cfg in configured.items():
        transport = cfg.get("transport", "http") if "url" in cfg else "stdio"
        enabled = _parse_boolish(cfg.get("enabled", True), default=True)
        server = active_servers.get(name)
        if server and server.session is not None:
            entry = {
                "name": name,
                "transport": transport,
                "tools": len(server._registered_tool_names) if hasattr(server, "_registered_tool_names") else len(server._tools),
                "connected": True,
                "disabled": False,
                "status": "connected",
            }
            if server._sampling:
                entry["sampling"] = dict(server._sampling.metrics)
            result.append(entry)
        elif not enabled:
            # A server with enabled: false is intentionally not connected — it is
            # disabled, not failed. Surface that distinction so consumers (banner,
            # TUI) can render "disabled" rather than an alarming "failed".
            result.append({
                "name": name,
                "transport": transport,
                "tools": 0,
                "connected": False,
                "disabled": True,
                "status": "disabled",
            })
        elif name in connecting:
            result.append({
                "name": name,
                "transport": transport,
                "tools": 0,
                "connected": False,
                "disabled": False,
                "status": "connecting",
            })
        elif name in connect_errors:
            result.append({
                "name": name,
                "transport": transport,
                "tools": 0,
                "connected": False,
                "disabled": False,
                "status": "failed",
                "error": connect_errors[name],
            })
        else:
            result.append({
                "name": name,
                "transport": transport,
                "tools": 0,
                "connected": False,
                "disabled": False,
                "status": "configured",
            })

    return result


def probe_mcp_server_tools() -> Dict[str, List[tuple]]:
    """Temporarily connect to configured MCP servers and list their tools.

    Designed for ``hermes tools`` interactive configuration — connects to each
    enabled server, grabs tool names and descriptions, then disconnects.
    Does NOT register tools in the Hermes registry.

    Returns:
        Dict mapping server name to list of (tool_name, description) tuples.
        Servers that fail to connect are omitted from the result.
    """
    if not _ensure_mcp_sdk():
        return {}

    servers_config = _load_mcp_config()
    if not servers_config:
        return {}

    enabled = {
        k: v for k, v in servers_config.items()
        if _parse_boolish(v.get("enabled", True), default=True)
    }
    if not enabled:
        return {}

    _ensure_mcp_loop()

    result: Dict[str, List[tuple]] = {}
    probed_servers: List[MCPServerTask] = []

    async def _probe_all():
        names = list(enabled.keys())
        coros = []
        for name, cfg in enabled.items():
            ct = cfg.get("connect_timeout", _DEFAULT_CONNECT_TIMEOUT)
            coros.append(asyncio.wait_for(_connect_server(name, cfg), timeout=ct))

        outcomes = await asyncio.gather(*coros, return_exceptions=True)

        for name, outcome in zip(names, outcomes):
            if isinstance(outcome, Exception):
                logger.debug("Probe: failed to connect to '%s': %s", name, outcome)
                continue
            probed_servers.append(outcome)
            tools = []
            for t in outcome._tools:
                desc = getattr(t, "description", "") or ""
                tools.append((t.name, desc))
            result[name] = tools

        # Shut down all probed connections
        await asyncio.gather(
            *(s.shutdown() for s in probed_servers),
            return_exceptions=True,
        )

    try:
        _run_on_mcp_loop(_probe_all, timeout=120)
    except Exception as exc:
        logger.debug("MCP probe failed: %s", exc)
    finally:
        _stop_mcp_loop_if_idle()

    return result


# Serializes in-place mutation of an agent's tool snapshot.  The reload RPC,
# the gateway reload, and the late-binding refresh thread all swap
# ``agent.tools`` / ``agent.valid_tool_names`` after the agent was built; the
# agent's run loop reads those during tool iteration, so a concurrent write
# mid-read could otherwise expose a half-updated list.
_agent_tools_lock = threading.Lock()


def has_registered_mcp_tools() -> bool:
    """True if any MCP server has actually registered tools into the registry.

    Cheap — checks the global MCP-tool→server name map under ``_lock``, no
    registry walk.  Used by the per-turn refresh hook so a session with no MCP
    tools (the common case, and also a connected-but-zero-tool/prompt-only
    server) skips the ``get_tool_definitions`` rebuild entirely.  Checks
    registered TOOLS, not connected servers, so a server that registers no tools
    doesn't keep the hook firing every turn.
    """
    with _lock:
        return bool(_mcp_tool_server_names)


def get_registered_mcp_server_names() -> set:
    """Return the set of MCP server names that have actually registered at
    least one tool into the registry (post-connection, post check_fn/include-
    exclude filtering) -- i.e. the real, availability-filtered signal, not
    just what's present in config.yaml under ``mcp_servers``.

    Used by capability-aware prompt building (e.g. gateway/session.py's
    Slack platform note) to detect an MCP server that provides a given
    platform's capability regardless of what its config key is named.
    """
    with _lock:
        return set(_mcp_tool_server_names.values())



def refresh_agent_mcp_tools(
    agent,
    *,
    enabled_override=None,
    disabled_override=None,
    quiet_mode: bool = True,
    content_aware: bool = False,
) -> set:
    """Re-derive an already-built agent's tool snapshot from the live registry.

    The agent snapshots ``agent.tools`` once at build time and never re-reads
    the registry (see ``run_agent`` / ``agent_init``).  When MCP servers connect
    *after* that snapshot — a slow HTTP/OAuth server that misses the bounded
    startup wait, or a ``/reload-mcp`` — their tools are invisible until the
    snapshot is rebuilt.  This is the single shared rebuild used by every such
    caller (the TUI ``reload.mcp`` RPC, the gateway reload, the late-binding
    refresh thread, and the per-turn between-turns refresh) so they can't drift
    apart again.

    The rebuild respects the agent's own ``enabled_toolsets`` /
    ``disabled_toolsets`` (the same filtering it was built with) and diffs by
    tool **name** (not count — a count compare misses an equal-size add/remove
    swap).

    Crucially it is **additive-preserving**: ``get_tool_definitions`` returns
    only the registry-derived tools, but ``agent_init`` appends two further
    families directly onto ``agent.tools`` *after* that — external
    memory-provider tools (mem0/honcho/…) and context-engine tools
    (``lcm_*``).  A naive ``agent.tools = get_tool_definitions(...)`` would
    silently DELETE those.  So after rebuilding the registry set we re-run the
    same post-build injectors ``agent_init`` used, reconstructing the full
    surface.  The new ``(tools, valid_tool_names)`` pair is published together
    under ``_agent_tools_lock`` so a concurrent reader never sees a
    cross-attribute half-swap.

    Returns the set of newly-added tool names (empty when nothing changed), so
    callers can decide whether to notify the user / re-emit session info.  The
    caller owns the prompt-cache contract: this helper does NOT check turn state,
    because each caller has a different policy (``/reload-mcp`` rebuilds after
    explicit user consent; the late-binding and between-turns paths only rebuild
    at a turn boundary, before that turn's ``tools=`` prefix is assembled).
    """
    from model_tools import get_tool_definitions
    from tools.registry import registry

    # Explicit reloads (/reload-mcp) pass freshly-resolved toolsets so a server
    # the user just ENABLED in config is picked up; the agent's stored selection
    # is then updated to match. The automatic paths (between-turns, late-binding)
    # pass nothing and reuse the agent's build-time selection unchanged.
    if enabled_override is not None or disabled_override is not None:
        enabled = enabled_override if enabled_override is not None else getattr(agent, "enabled_toolsets", None)
        disabled = disabled_override if disabled_override is not None else getattr(agent, "disabled_toolsets", None)
        agent.enabled_toolsets = enabled
        agent.disabled_toolsets = disabled
    else:
        enabled = getattr(agent, "enabled_toolsets", None)
        disabled = getattr(agent, "disabled_toolsets", None)

    # Capture the registry generation this rebuild is derived from BEFORE the
    # (potentially slow) get_tool_definitions call. Used at publish time to
    # reject a stale write: if two callers race (e.g. the late-refresh daemon
    # and the between-turns prologue around turn 1), a slower caller that
    # computed an OLDER set must not clobber a newer set another caller already
    # published. ``registry._generation`` bumps on every (de)register.
    snapshot_generation = registry._generation

    # Registry-derived tools (built-ins + MCP), filtered to the agent's toolsets.
    # Computed OUTSIDE the lock (get_tool_definitions can be slow); the diff and
    # publish below happen together in ONE critical section so two concurrent
    # callers can't torn-publish or compute overlapping ``added`` sets.
    new_defs = list(
        get_tool_definitions(
            enabled_toolsets=enabled,
            disabled_toolsets=disabled,
            quiet_mode=quiet_mode,
        )
        or []
    )
    new_names = {t["function"]["name"] for t in new_defs}

    # Re-append the post-build injected families that get_tool_definitions does
    # NOT reproduce, so a refresh never strips them (memory-provider + context-
    # engine tools). Staged entirely on LOCALS — the live ``agent.tools`` /
    # ``valid_tool_names`` / ``_context_engine_tool_names`` are never touched
    # until the single atomic publish below, so a concurrent reader
    # (``build_api_kwargs``) can't see a partial rebuild or a cross-attribute
    # half-swap. ``staged_engine_names`` are the context-engine routing names
    # this rebuild actually appended (matching agent_init's dedup-aware add).
    staged_engine_names = _reinject_post_build_tools(agent, new_defs, new_names)

    # Single atomic read-diff-publish so the returned ``added`` is consistent
    # with what was actually published, even under concurrent callers, and a
    # stale (older-generation) rebuild can't overwrite a newer published one.
    with _agent_tools_lock:
        # Defensive: the published generation should be an int, but tolerate an
        # agent that never set it (or set a non-int, e.g. a test mock) rather
        # than throwing TypeError on the comparison and silently failing the
        # whole refresh.
        published_gen_raw = getattr(agent, "_tool_snapshot_generation", -1)
        published_gen = published_gen_raw if isinstance(published_gen_raw, int) else -1
        if snapshot_generation < published_gen:
            # A newer snapshot already won; our set is stale — drop it.
            return set()
        current = {
            t["function"]["name"]
            for t in (getattr(agent, "tools", None) or [])
        }
        if new_names == current:
            # Same NAME set. For MCP-reload callers that is "no change" —
            # leave the live snapshot untouched (no churn). Content-aware
            # callers (the compaction boundary) also diff the serialized
            # bytes: dynamic schemas (image_generate capabilities,
            # delegate_task limits, execute_code stubs) change CONTENT
            # under stable names when config changes between compactions.
            content_changed = False
            if content_aware:
                try:
                    _stable = json.dumps(
                        (getattr(agent, "tools", None) or []),
                        sort_keys=True, separators=(",", ":"), default=str,
                    )
                    _new = json.dumps(
                        new_defs, sort_keys=True, separators=(",", ":"),
                        default=str,
                    )
                    content_changed = _stable != _new
                except Exception:  # noqa: BLE001
                    content_changed = False
            if not content_changed:
                # Record the generation so an in-flight older caller can't
                # clobber.
                agent._tool_snapshot_generation = max(published_gen, snapshot_generation)
                return set()
        agent.tools = new_defs
        agent.valid_tool_names = new_names
        # Publish context-engine routing names atomically with the snapshot.
        engine_names = getattr(agent, "_context_engine_tool_names", None)
        if isinstance(engine_names, set):
            engine_names.clear()
            engine_names.update(staged_engine_names)
        agent._tool_snapshot_generation = max(published_gen, snapshot_generation)
        return new_names - current


def _reinject_post_build_tools(agent, tools_list: list, name_set: set) -> set:
    """Append memory-provider and context-engine tools onto staged locals.

    Mirrors the post-``get_tool_definitions`` injection in ``agent_init`` so a
    snapshot rebuild reconstructs the FULL tool surface, not just the
    registry-derived subset. Operates ONLY on the caller's staged ``tools_list``
    / ``name_set`` (never the live agent attributes) so the rebuild stays atomic.
    Idempotent (skips names already present) and fail-soft.

    Returns the set of context-engine routing names actually appended by THIS
    rebuild — matching ``agent_init``'s dedup behavior (a name already provided
    by a registry/plugin tool is NOT claimed for context-engine routing). The
    caller publishes this into ``agent._context_engine_tool_names`` atomically
    with the snapshot.
    """
    def _add(schema: dict) -> bool:
        name = schema.get("name", "")
        if not name or name in name_set:
            return False
        tools_list.append({"type": "function", "function": schema})
        name_set.add(name)
        return True

    # Memory-provider tools (mem0/honcho/byterover/supermemory/…).
    try:
        memory_manager = getattr(agent, "_memory_manager", None)
        get_mem_schemas = getattr(memory_manager, "get_all_tool_schemas", None) if memory_manager else None
        if callable(get_mem_schemas):
            # Honor the same toolset gate inject_memory_provider_tools uses.
            from agent.memory_manager import memory_provider_tools_enabled
            if memory_provider_tools_enabled(
                getattr(agent, "enabled_toolsets", None),
                getattr(agent, "disabled_toolsets", None),
                memory_tool_present="memory" in name_set,
            ):
                for schema in get_mem_schemas():
                    if isinstance(schema, dict):
                        _add(schema)
    except Exception:
        logger.debug("Memory-provider tool re-injection skipped", exc_info=True)

    # Context-engine tools (lcm_grep/lcm_describe/…) — the `context_engine`
    # toolset is intentionally empty, so these only exist via this append.
    # Honor the same enabled_toolsets gate agent_init uses (#5544): without it a
    # restricted-toolset platform (e.g. platform_toolsets: telegram: []) would
    # re-leak lcm_* tools the build deliberately excluded, and pay the local-
    # model latency penalty.
    staged_engine_names: set = set()
    try:
        enabled = getattr(agent, "enabled_toolsets", None)
        context_engine_allowed = enabled is None or "context_engine" in enabled
        compressor = getattr(agent, "context_compressor", None)
        get_schemas = getattr(compressor, "get_tool_schemas", None) if compressor else None
        if context_engine_allowed and callable(get_schemas):
            for schema in get_schemas():
                if not isinstance(schema, dict):
                    continue
                name = schema.get("name", "")
                # Only claim the routing name when WE appended the schema, so a
                # name already owned by a registry/plugin tool keeps its own
                # dispatch (matches agent_init.py's `continue`-before-claim).
                if _add(schema) and name:
                    staged_engine_names.add(name)
    except Exception:
        logger.debug("Context-engine tool re-injection skipped", exc_info=True)

    return staged_engine_names


def shutdown_mcp_servers():
    """Close all MCP server connections and stop the background loop.

    Each server Task is signalled to exit its ``async with`` block so that
    the anyio cancel-scope cleanup happens in the same Task that opened it.
    All servers are shut down in parallel via ``asyncio.gather``.
    """
    with _lock:
        servers_snapshot = list(_servers.values())

    # Fast path: nothing to shut down. The connect-cooldown maps can still
    # be populated here — a server that failed to connect is never recorded
    # in ``_servers`` (that is the very premise of the #50394 cooldown), so
    # "no live servers" is the MOST likely state in which stale backoff
    # entries exist. Clear them so a post-shutdown restart re-attempts every
    # configured server immediately.
    if not servers_snapshot:
        with _lock:
            _server_connect_retry_after.clear()
            _server_connect_failures.clear()
        _stop_mcp_loop()
        return

    async def _shutdown():
        results = await asyncio.gather(
            *(server.shutdown() for server in servers_snapshot),
            return_exceptions=True,
        )
        for server, result in zip(servers_snapshot, results):
            if isinstance(result, Exception):
                logger.debug(
                    "Error closing MCP server '%s': %s", server.name, result,
                )
        with _lock:
            _servers.clear()
            # Drop connect-retry cooldowns too: a full shutdown/restart
            # should re-attempt every server immediately, not honour a
            # stale per-server backoff from before the restart (#50394).
            _server_connect_retry_after.clear()
            _server_connect_failures.clear()

    with _lock:
        loop = _mcp_loop
    if loop is not None and loop.is_running():
        from agent.async_utils import safe_schedule_threadsafe
        future = safe_schedule_threadsafe(
            _shutdown(), loop,
            logger=logger,
            log_message="MCP shutdown: failed to schedule",
        )
        if future is not None:
            try:
                future.result(timeout=15)
            except BaseException as exc:
                logger.debug("Error during MCP shutdown: %s", exc)

    # Unconditional final sweep: whether the async ``_shutdown`` ran,
    # timed out, or was never scheduled (loop already stopped), a full
    # shutdown must leave no stale connect-cooldown state behind — the
    # next start should re-attempt every server immediately (#50394).
    with _lock:
        _server_connect_retry_after.clear()
        _server_connect_failures.clear()

    _stop_mcp_loop()
'''

EXPORTED_NAMES = ('_make_check_fn', '_normalize_mcp_input_schema', 'sanitize_mcp_name_component', 'MCP_TOOL_NAME_PREFIX', '_MCP_NAME_DELIM', 'mcp_prefixed_tool_name', '_convert_mcp_schema', '_build_utility_schemas', '_normalize_name_filter', 'matches_name_filter', '_parse_boolish', '_get_lifecycle_seconds', '_UTILITY_CAPABILITY_METHODS', '_UTILITY_CAPABILITY_ATTRS', '_track_mcp_tool_server', '_forget_mcp_tool_server', '_select_utility_schemas', '_existing_tool_names', '_register_server_tools', '_CachedMCPTool', '_register_from_cache_sync', '_discover_and_register_server', 'register_mcp_servers', 'discover_mcp_tools', 'is_mcp_tool_parallel_safe', 'get_mcp_status', 'probe_mcp_server_tools', '_agent_tools_lock', 'has_registered_mcp_tools', 'get_registered_mcp_server_names', 'refresh_agent_mcp_tools', '_reinject_post_build_tools', 'shutdown_mcp_servers')
SOURCE_PATH = Path(__file__)

def install(namespace: dict[str, object]) -> None:
    filename = str(SOURCE_PATH)
    linecache.cache[filename] = (
        len(_SOURCE), None, _SOURCE.splitlines(True), filename
    )
    exec(compile(_SOURCE, filename, "exec"), namespace, namespace)
