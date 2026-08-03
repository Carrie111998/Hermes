<!-- stm:v1 -->
## Session Memory

### User Instructions
- …

### Long Horizon Context
- …

### Decisions
- …

### Conclusions
- …

### Active References
- …

<existing_memory>
## Session Memory

### User Instructions
- None captured yet.

### Long Horizon Context
- None captured yet.

### Decisions
- None captured yet.

### Conclusions
- None captured yet.

### Active References
- None captured yet.

</existing_memory>

<conversation_update>
USER:
Investigate the Hermes agent tool registry mechanisms in /home/capeta/Code/hermes-agent/. Answer these questions with file:line citations:

1. In tools/registry.py, find how the tool registry works:
   - How are tools registered? Is it a module-level global, a class instance, or something else?
   - Are tools registered once at startup, or rebuilt per-request/turn?
   - Look at the register() method and any caching/clearing logic.

2. In model_tools.py, find how tool schemas are built and sent to the LLM:
   - Is the 'tools' array (OpenAI tool definitions) rebuilt every turn or cached?
   - Look at functions like get_tool_definitions(), discover_builtin_tools(), build_tool_schemas(), or similar.
   - Is there any cache key or invalidation mechanism for the tool list?

3. Does any reload mechanism (MCP reload, plugin reload, config reload) force the tool registry to rebuild?

4. How does the tool registry interact with toolsets? When you enable/disable a toolset, does the registry update immediately?

For each finding, provide the exact file:line and relevant code snippets. Check tools/registry.py, model_tools.py, toolsets.py, and any related files.

---

ASSISTANT:
I'll investigate the Hermes agent tool registry mechanisms. Let me start by locating the key files.

---

ASSISTANT:
The files are in the current working directory. Let me read them.

---

ASSISTANT:
Now let me examine model_tools.py for how tool schemas are built and cached.

---

ASSISTANT:
Let me read the `get_tool_definitions` function in model_tools.py and its surroundings.

---

ASSISTANT:
Let me read the top of model_tools.py for the discovery setup and check `_is_delegated_child_context`.

---

ASSISTANT:
Now let me find where `get_tool_definitions` is called in run_agent.py and how tools flow to the LLM each turn.

---

ASSISTANT:
Let me find production callers of the invalidation functions and the MCP reload path.

---

ASSISTANT:
Let me read the MCP dynamic tool refresh path in tools/mcp_tool.py.

---

ASSISTANT:
Now let me examine toolsets.py to understand how the registry interacts with toolset definitions.

---

ASSISTANT:
Let me look at the toolset resolution functions in toolsets.py.

---

ASSISTANT:
Now let me check how `hermes tools` enable/disable interacts with the registry (tools_config.py).

---

ASSISTANT:
Let me look at the tools_config.py file directly for the enable/disable flow.

---

ASSISTANT:
The grep was too broad. Let me search specifically in tools_config.py.

---

ASSISTANT:
The grep is searching the whole directory. Let me read tools_config.py directly.

---

ASSISTANT:
Let me check the gateway's `/reload-mcp` handler to see how it rebuilds agents/tools.

---

ASSISTANT:
Let me look at `refresh_agent_mcp_tools` and `_register_server_tools` in mcp_tool.py.

---

ASSISTANT:
Let me check how `agent.tools` is snapshotted in run_agent.py during agent initialization.

---

ASSISTANT:
Let me confirm the per-turn behavior in turn_context.py and check `discover_mcp_tools`.

---

ASSISTANT:
Let me check `shutdown_mcp_servers` and find the between-turns refresh caller in turn_context.py.

---

ASSISTANT:
Let me check if there's any plugin reload mechanism and verify the CLI wrappers.

---

ASSISTANT:
I now have a complete picture of the tool registry mechanisms. Here is my report.

---

# Hermes Agent Tool Registry — Investigation Report

**Note:** The paths below are under `/var/scabbard/@Code/hermes-agent/` (the checkout I have access to; the `/home/capeta/Code/hermes-agent/` path you referenced was not present in my environment).

## 1. How the tool registry works (`tools/registry.py`)

### Registration mechanism: a module-level singleton class instance

- `registry = ToolRegistry()` is a **module-level singleton** at `tools/registry.py:765`.
- `ToolRegistry` (class defined at `tools/registry.py:217`) holds tools in an instance dict `self._tools: Dict[str, ToolEntry]` (`tools/registry.py:221`), guarded by an `RLock` (`tools/registry.py:233`).
- Each tool file calls `registry.register(...)` at **module import time** (`tools/registry.py:3-5`, `365-380`). `discover_builtin_tools()` (`tools/registry.py:67-84`) finds every `tools/*.py` that contains a top-level `registry.register(...)` call (via AST inspection, `tools/registry.py:30-64`) and `importlib.import_module`s it — the import is what fires the registration.

### Registered once at startup, not per-turn

- `discover_builtin_tools()` is called **once** at `model_tools.py:197` (module import of `model_tools`), and plugin discovery (`discover_plugins()`) once at `model_tools.py:213-217`.
- MCP discovery was deliberately **removed from module-level** (`model_tools.py:199-210`); each entry point runs it explicitly at its own startup: `gateway/run.py`, `cli.py`, `tui_gateway/server.py`, `acp_adapter/server.py` (comment at `model_tools.py:206-210`). The CLI wrapper even waits for a one-time background discovery thread before the first snapshot: `cli.py:905-908` → `hermes_cli/mcp_startup.py:123-136`.
- The only post-startup mutation path is MCP **dynamic refresh** (see Q3) — `register()`/`deregister()` are otherwise startup-time only.

### `register()` and the invalidation/caching logic

- `register()` (`tools/registry.py:365-457`) is lock-protected and rejects shadowing: registering a name that already exists in a *different* toolset is refused unless `override=True` (explicit plugin opt-in, gated by `_plugin_override_policy`, `tools/registry.py:402-417`) or both are MCP toolsets (`tools/registry.py:393-401`).
- **Generation counter:** every mutation (`register` at `tools/registry.py:457`, `deregister` at `tools/registry.py:523`, `register_toolset_alias` at `tools/registry.py:300`) does `self._generation += 1`. This is the primary cache-invalidation signal consumed by `model_tools.get_tool_definitions` (see Q2).
- **`check_fn` TTL cache:** availability probes (`check_fn`) are cached ~30 s (`_CHECK_FN_TTL_SECONDS = 30.0`, `tools/registry.py:143`) via `_check_fn_cached()` (`tools/registry.py:154-206`), with a 60 s "last-good" grace window for transient failures (`tools/registry.py:147, 184-196`). `invalidate_check_fn_cache()` exists at `tools/registry.py:209-214` but is **never called from production code** — only tests.
- `deregister()` (`tools/registry.py:459-524`) removes a tool, cleans up the toolset check/alias if it was the last tool, and applies the same plugin-ownership gate (MCP toolsets exempt, `tools/registry.py:479-509`).

## 2. How tool schemas are built/sent to the LLM (`model_tools.py`)

### The `tools` array IS cached (memoized), not rebuilt every turn

- `get_tool_definitions()` (`model_tools.py:288-364`) is the main schema provider. It has a **module-level memoization dict** `_tool_defs_cache` (`model_tools.py:270`), hard-capped at 8 entries (`_TOOL_DEFS_CACHE_MAX = 8`, `model_tools.py:278`).
- The cache is only active when `quiet_mode=True` (the gateway/AIAgent path; `model_tools.py:320`). On a hit it returns a shallow copy (`model_tools.py:337-345`); on a miss it computes via `_compute_tool_definitions()` (`model_tools.py:367-581`) and stores a copy (`model_tools.py:349-363`).

### Cache key and invalidation

The key (`model_tools.py:328-336`) captures:
```python
cache_key = (
    frozenset(enabled_toolsets) if enabled_toolsets is not None else None,
    frozenset(disabled_toolsets) if disabled_toolsets else None,
    registry._generation,          # ← bumps on register/deregister/alias
    cfg_fp,                        # ← config.yaml mtime+size (line 322-325)
    bool(os.environ.get("HERMES_KANBAN_TASK")),
    bool(skip_tool_search_assembly),
    _is_delegated_child_context(),
)
```
Invalidation is therefore **transparent**: any `register()`/`deregister()`/`register_toolset_alias()` bumps `registry._generation` and invalidates all entries; any edit to `config.yaml` changes the `cfg_fp` fingerprint (`model_tools.py:321-325`). There's also an explicit escape hatch `_clear_tool_defs_cache()` (`model_tools.py:281-285`), likewise **never called from production code** (only tests).

### Per-turn reality: the agent snapshots tools once

- The agent's tool list is a **one-time snapshot** at build time: `agent.tools = _ra().get_tool_definitions(...)` in `agent/agent_init.py:1386-1390` (with `valid_tool_names` at `1393-1395`).
- Every turn, the loop reuses that frozen snapshot — `tools=agent.tools or None` in `agent/turn_context.py:649, 728, 892`, `agent/conversation_loop.py:4259, 5818`, `agent/conversation_compression.py:2297`.
- So: `get_tool_definitions()` is cached at the module level, *and* each agent holds an immutable per-session copy. The tools array is **not** rebuilt per turn for an existing agent.

## 3. Reload mechanisms that force a registry rebuild

### MCP reload — yes, explicitly

- **Dynamic refresh via `notifications/tools/list_changed`:** `MCPServerTask._refresh_tools()` (`tools/mcp_tool.py:2074-2140`) — triggered by the notification handler at `tools/mcp_tool.py:2043-2059` — deregisters stale tools (`registry.deregister`, `tools/mcp_tool.py:2111-2113`) and re-registers the fresh list via `_register_server_tools()` (`tools/mcp_tool.py:2117-2119`, defined at `5466-5573`). Each call bumps `_generation`.
- **`/reload-mcp` command:** `_execute_mcp_reload()` in `gateway/run.py:16470-16575` does `shutdown_mcp_servers()` (`tools/mcp_tool.py:6182`) then `discover_mcp_tools()` (`tools/mcp_tool.py:5741`), then calls `refresh_agent_mcp_tools(_agent, ...)` on every cached agent (`gateway/run.py:16518-16545`).
- **`refresh_agent_mcp_tools()`** (`tools/mcp_tool.py:5991-6110`) is the single shared rebuild for already-built agents: it re-runs `get_tool_definitions()` against the live registry (`tools/mcp_tool.py:6060-6067`), re-injects memory/context-engine tools (`tools/mcp_tool.py:6078`), and atomically republishes `agent.tools`/`valid_tool_names` under `_agent_tools_lock` (`tools/mcp_tool.py:6083-6109`), using a `_tool_snapshot_generation` compare to reject stale writes (`tools/mcp_tool.py:6090-6092`).
