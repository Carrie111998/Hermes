"""Registry-query helpers extracted from :mod:`tools.mcp_tool`.

``has_registered_mcp_tools`` and ``get_registered_mcp_server_names`` are
the cheap per-turn registry signal used by the agent refresh hook and by
capability-aware prompt building. Their implementations were extracted
byte-verbatim from ``tools/mcp_tool.py`` (mcp_tool R5 slice, lines
7192-7217 at pin ee4bb75b). The shared registry state they read
(``_lock`` / ``_mcp_tool_server_names``) remains owned by
:mod:`tools.mcp_tool` and is resolved lazily at call time so the moved
functions keep operating on the exact same runtime objects as before.
"""


def has_registered_mcp_tools() -> bool:
    """True if any MCP server has actually registered tools into the registry.

    Cheap — checks the global MCP-tool→server name map under ``_lock``, no
    registry walk.  Used by the per-turn refresh hook so a session with no MCP
    tools (the common case, and also a connected-but-zero-tool/prompt-only
    server) skips the ``get_tool_definitions`` rebuild entirely.  Checks
    registered TOOLS, not connected servers, so a server that registers no tools
    doesn't keep the hook firing every turn.
    """
    # Seam: resolve the shared registry state from the owning module at call
    # time so the extracted functions keep reading the same runtime objects.
    from tools.mcp_tool import _lock, _mcp_tool_server_names
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
    # Seam: resolve the shared registry state from the owning module at call
    # time so the extracted functions keep reading the same runtime objects.
    from tools.mcp_tool import _lock, _mcp_tool_server_names
    with _lock:
        return set(_mcp_tool_server_names.values())
