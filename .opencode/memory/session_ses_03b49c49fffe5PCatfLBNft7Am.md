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
Read the following files in /home/capeta/Code/hermes-agent/ and find the exact insertion points for the tool registry freeze layer:

1. In `run_agent.py`: Find where `agent.tools` is assigned. Search for `self.tools =` or `agent.tools` or tool list construction. Also find how agent tools relate to registry.

2. In `model_tools.py`: Find `_tool_defs_cache` - how is it keyed? What invalidates it? What is `_generation` in the registry and where is it incremented?

3. In `tools/registry.py`: Find `_generation` - where it's incremented, what it's used for. Also find the full registry structure.

4. Search for `refresh_agent_mcp_tools` across the codebase - what does it do? Could we write a `refresh_agent_plugin_tools` analog?

5. In `agent_init.py` (or wherever agent initialization happens): Find the tool setup flow - how tools are assigned to the agent.

For each finding, cite exact file path and line number(s). Return the relevant code excerpts.

---

ASSISTANT:
Both paths exist. Let me verify they're the same content and begin the searches.

---

ASSISTANT:
The two paths are the same repository (same inode). I'll work in `/var/scabbard/@Code/hermes-agent`. The `self.tools =` search returned repo-wide results without `run_agent.py` — let me search that file directly.

---

ASSISTANT:
`run_agent.py` only references `self.tools` at line 2831. The tool assignment must live in `agent/agent_init.py`. Let me examine both.

---

ASSISTANT:
Found the tool setup in `agent/agent_init.py`. Let me read the key sections.

---

ASSISTANT:
Now let me examine `model_tools.py`'s cache mechanism and `tools/registry.py`'s structure in detail.

---

ASSISTANT:
Now let me see the `register`/`deregister` methods and the `ToolEntry` structure in registry.py.

---

ASSISTANT:
Now let me read the top of `registry.py` for the `ToolEntry` structure and the module-level `registry` singleton.

---

ASSISTANT:
Let me read the rest of `registry.py` (lines 580-810) for the module-level singleton and remaining API.

---

ASSISTANT:
Now the key piece — `refresh_agent_mcp_tools` in `tools/mcp_tool.py`.

---

ASSISTANT:
Now let me find `_agent_tools_lock`, the memory-tools injection in `agent_init.py`, and how plugin tools flow into the registry.

---

ASSISTANT:
Let me check how `run_agent.py` consumes `agent.tools`/`valid_tool_names` and the between-turns refresh in `turn_context.py`.

---

ASSISTANT:
Let me quickly check the `_plugin_tool_names` consumer at `plugins.py:2431` and confirm the memory-tools injector signature.

---

ASSISTANT:
Let me verify the early `_tool_snapshot_generation = 0` at `agent_init.py:872` for completeness.

---

ASSISTANT:
I have all the findings. Here is the complete report.

---

# Tool Registry Freeze Layer — Insertion Point Analysis

**Note:** `/home/capeta/Code/hermes-agent/` and `/var/scabbard/@Code/hermes-agent/` are the same repo (identical inode `91580` on `run_agent.py`). All paths below are absolute and line numbers verified against current `main`.

---

## 1. `run_agent.py` — where `agent.tools` is assigned

**Key finding: `run_agent.py` never assigns `self.tools`.** The `AIAgent` object's `tools` attribute is populated in `agent/agent_init.py` (the init helper), not in `run_agent.py`. There is exactly **one** reference in `run_agent.py`:

- `/var/scabbard/@Code/hermes-agent/run_agent.py:2831` — session-log serialization only:
```python
"tools": self.tools or [],
```
- `/var/scabbard/@Code/hermes-agent/run_agent.py:137` — re-exports the schema provider:
```python
get_tool_definitions,  # noqa: F401  # re-exported for tests that mock.patch("run_agent.get_tool_definitions")
```
- `/var/scabbard/@Code/hermes-agent/run_agent.py:6128-6131` — how tools reach the API call:
```python
def _build_api_kwargs(self, api_messages: list) -> dict:
    """Forwarder — see ``agent.chat_completion_helpers.build_api_kwargs``."""
    from agent.chat_completion_helpers import build_api_kwargs
    return build_api_kwargs(self, api_messages)
```

**How agent tools relate to the registry:** `agent.tools` is a **build-time snapshot** — a plain list of OpenAI-format dicts taken once via `get_tool_definitions()` and copied onto the agent. It is NOT live-linked to the registry. The actual read happens in:

- `/var/scabbard/@Code/hermes-agent/agent/chat_completion_helpers.py:984-986`:
```python
def build_api_kwargs(agent, api_messages: list) -> dict:
    """Build the keyword arguments dict for the active API mode."""
    tools_for_api = agent.tools
```
This snapshot-vs-live-registry gap is precisely why `refresh_agent_mcp_tools` (item 4) exists — a late-registered tool is invisible to an already-built agent until the snapshot is rebuilt.

---

## 2. `model_tools.py` — `_tool_defs_cache`

**Definition** — `/var/scabbard/@Code/hermes-agent/model_tools.py:270-285`:
```python
_tool_defs_cache: Dict[tuple, List[Dict[str, Any]]] = {}

# Hard cap on memoized get_tool_definitions() results. ... 8 comfortably covers
_TOOL_DEFS_CACHE_MAX = 8


def _clear_tool_defs_cache() -> None:
    """Drop memoized get_tool_definitions() results. Called when dynamic
    schema dependencies change (e.g. discord capability cache reset,
    execute_code sandbox reconfigured)."""
    _tool_defs_cache.clear()
```

**How it's keyed** — `/var/scabbard/@Code/hermes-agent/model_tools.py:328-336` (inside `get_tool_definitions`, only when `quiet_mode=True`):
```python
cache_key = (
    frozenset(enabled_toolsets) if enabled_toolsets is not None else None,
    frozenset(disabled_toolsets) if disabled_toolsets else None,
    registry._generation,                     # ← bumps on register/deregister/alias
    cfg_fp,                                   # ← (mtime_ns, size) of config.yaml
    bool(os.environ.get("HERMES_KANBAN_TASK")),
    bool(skip_tool_search_assembly),
    _is_delegated_child_context(),
)
```
- The `cfg_fp` fingerprint is recomputed **on every quiet-mode call** (lines 322-327: `cfg_path.stat()` → `(st_mtime_ns, st_size)`), so user config edits bust the cache via the same signal.
- Cache hit path: lines 337-345 (returns `list(cached)` shallow copy, updates `_last_resolved_tool_names`).
- Miss path: lines 347-363 — computes via `_compute_tool_definitions(...)`, then LRU-evicts oldest entry (`_tool_defs_cache.pop(next(iter(_tool_defs_cache)))` at line 360-361) and stores. Line 351-356 comment notes the shallow-copy is required so downstream `run_agent` appends (memory/LCM tools) don't poison the cache (#17335).
- `_clear_tool_defs_cache()` (281-285) is the explicit escape hatch — **never called from production code**, only tests.

**What invalidates it:** (a) `registry._generation` bump (any register/deregister/alias), (b) config.yaml mtime/size change, (c) `HERMES_KANBAN_TASK` env change, (d) explicit `_clear_tool_defs_cache()`.

---

## 3. `tools/registry.py` — `_generation` and full registry structure

**`_generation` declaration** — `/var/scabbard/@Code/hermes-agent/tools/registry.py:234-239`:
```python
# Monotonically-increasing generation counter. Bumped on every
# mutation (register / deregister / register_toolset_alias / MCP
# refresh). External callers (e.g. get_tool_definitions) can memoize
# against it: a cache entry keyed on the generation is valid for as
# long as the generation hasn't changed.
self._generation: int = 0
```

**Where it's incremented** (all inside `with self._lock:`):
| Line | Method | Context |
|---|---|---|
| `registry.py:300` | `register_toolset_alias()` | end of method |
| `registry.py:457` | `register()` | end of method, after `self._tools[name] = ToolEntry(...)` |
| `registry.py:523` | `deregister()` | end of method, after cleanup |

**Full registry structure** — `ToolRegistry.__init__` at `registry.py:220-239`:
```python
self._tools: Dict[str, ToolEntry] = {}
self._plugin_override_policy: Dict[str, bool] = {}
self._toolset_checks: Dict[str, Callable] = {}
self._toolset_aliases: Dict[str, str] = {}
self._lock = threading.RLock()
self._generation: int = 0
```

**`ToolEntry` dataclass** (slots) — `registry.py:87-116`, fields: `name, toolset, schema, handler, check_fn, requires_env, is_async, description, emoji, max_result_size_chars, dynamic_schema_overrides` (the last is a zero-arg callable applied at `get_definitions()` time).

**Module-level singleton** — `registry.py:765`:
```python
# Module-level singleton
registry = ToolRegistry()
```

**Other relevant machinery:**
- `_snapshot_state()` / `_snapshot_entries()` — `registry.py:241-248`, thread-safe snapshots under the RLock (readers get stable views while MCP refresh mutates).
- `discover_builtin_tools()` — `registry.py:67-84`, auto-imports `tools/*.py` modules containing a top-level `registry.register(...)` (excludes `registry.py`, `mcp_tool.py`).
- `register()` — `registry.py:365-457`; the override/shadow-rejection gate (plugin toolsets cannot clobber built-ins without `override=True` + operator opt-in).
- `deregister()` — `registry.py:459-524`; ownership-gated (plugins can only remove their own tools, except `mcp-*` toolsets).
- `get_definitions()` — `registry.py:530-577`; applies the 30 s `check_fn` TTL cache + `dynamic_schema_overrides` per tool.
- `check_fn` TTL cache — `registry.py:143-215` (`_CHECK_FN_TTL_SECONDS = 30.0`, `_CHECK_FN_FAILURE_GRACE_SECONDS = 60.0`).
- Query helpers — `registry.py:650-761` (`get_all_tool_names`, `get_schema`, `get_toolset_for_tool`, `get_tool_to_toolset_map`, `is_toolset_available`, `check_toolset_requirements`, `get_available_toolsets`, `get_toolset_requirements`, `check_tool_availability`).

---

## 4. `refresh_agent_mcp_tools` — definition, callers, and the plugin analog
