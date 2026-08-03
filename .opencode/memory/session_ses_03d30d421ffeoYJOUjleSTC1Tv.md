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
Investigate the Hermes agent config reload mechanisms in /home/capeta/Code/hermes-agent/. Answer these questions with file:line citations:

1. In hermes_cli/config.py, find the load_config() function. Look at lines around 236-248 for the caching mechanism based on (path, mtime_ns, size). 
   - Does it automatically re-read config.yaml when the file changes?
   - What does the cache dict look like? What key is used?
   - Is there any notification/hook mechanism for config changes, or purely mtime-based polling?

2. Does the mtime-based auto-invalidation apply to ALL config keys? Specifically:
   - plugins.enabled
   - platform_toolsets
   - Any sections that might be excluded from the cache?

3. If plugins.enabled changes in config.yaml between API calls, does the next call to discover_and_load() pick up the new list? Trace the full path: gateway request -> load_config() -> discover_and_load() -> reads plugins.enabled.

4. Is there a config watch/notify mechanism, or is it purely polling on each access?

For each finding, provide the exact file:line and relevant code snippets. Check DEFAULT_CONFIG, _load_yaml_config, and any config caching logic.

---

ASSISTANT:
Both paths resolve to the same directory (`/var/scabbard/@Code/hermes-agent`). Now let me examine the config file.

---

ASSISTANT:
I found the cache definitions. Now let me read `load_config()` and the `_load_yaml_config` function around lines 7325-7460.

---

ASSISTANT:
Now let me find `_load_config_impl` which is the actual implementation:

---

ASSISTANT:
Now let me examine `discover_and_load()` in plugins.py and check for any watch/notify mechanisms:

---

ASSISTANT:
Interesting — there's an MCP-specific config watcher in cli.py. Let me look at `_get_enabled_plugins()` and the MCP config watcher:

---

ASSISTANT:
Now let me read the `_get_enabled_plugins` function in plugins.py:

---

ASSISTANT:
Now let me trace the gateway path. Let me find all `discover_and_load` call sites and how the gateway handles requests:

---

ASSISTANT:
Let me check where `discover_plugins(force=True)` is called and how the gateway per-request path works:

---

ASSISTANT:
Let me trace the gateway per-request path. First, let me look at gateway/config.py call sites and the message processing flow in gateway/run.py:

---

ASSISTANT:
Key finding — gateway/run.py:2785 says the gateway bypasses `load_config()`. Let me examine that section:

---

ASSISTANT:
Let me look at the gateway's `_load_gateway_config` and the MCP config watcher, plus how toolsets read plugin state per-request:

---

ASSISTANT:
Let me verify the `_check_config_mcp_changes` function and check the tools_config plugin toolset resolution path:

---

ASSISTANT:
Let me check the MCP watcher implementation and how toolsets are resolved from config:

---

ASSISTANT:
Let me check DEFAULT_CONFIG structure for `plugins` and `platform_toolsets`, and how `get_tool_definitions` reads config:

---

ASSISTANT:
Let me look at `get_tool_definitions` in model_tools.py to see how tool definitions are resolved per API call:

---

ASSISTANT:
Let me read `get_tool_definitions` in model_tools.py to understand tool definition caching:

---

ASSISTANT:
Let me check how the gateway resolves toolsets when creating an agent, and the registry `_generation` mechanism:

---

ASSISTANT:
Let me check the plugin enable/disable flow in plugins_cmd.py to see if it forces re-discovery:

---

ASSISTANT:
Let me find the actual `hermes plugins enable` handler to confirm whether it forces re-discovery:

---

ASSISTANT:
Let me verify DEFAULT_CONFIG does not contain `plugins`/`platform_toolsets` and check `_warn_config_parse_failure`:

---

ASSISTANT:
Let me verify the native gateway (non-API-server) agent creation path resolves toolsets per request:

---

ASSISTANT:
I have all the evidence needed. Here is the complete analysis.

---

## Q1: `load_config()` caching mechanism (config.py:236-248)

**Yes, it auto-re-reads when the file changes — purely via `stat()` polling on every access. No notification hook.**

The cache definition (config.py:235-252):

```python
_LAST_EXPANDED_CONFIG_BY_PATH: Dict[str, Any] = {}          # line 235
# (path, mtime_ns, size) -> cached expanded config dict.    # line 236
# load_config() returns a deepcopy of the cached value when the file
# hasn't changed since the last load, skipping yaml.safe_load + ...
# save_config() + migrate_config() write via atomic_yaml_write which
# produces a fresh inode, so stat() sees a new mtime_ns and the next
# load repopulates automatically — no explicit invalidation hook.  # lines 240-242
_LOAD_CONFIG_CACHE: Dict[str, Tuple[int, int, int, int, Dict[str, Any], Dict[str, Optional[str]]]] = {}  # line 248
_RAW_CONFIG_CACHE: Dict[str, Tuple[int, int, Dict[str, Any]]] = {}   # line 252 (same pattern for read_raw_config)
_CONFIG_LOCK = threading.RLock()                            # line 260
```

**Cache structure:** The dict is keyed by `str(config_path)` (the profile-scoped path string — comment at config.py:7411-7413 says "keyed on `str(config_path)` so profile switches don't collide"). The value is a 6-tuple `(user_mtime_ns, user_size, managed_mtime_ns, managed_size, merged_value, env_ref_snapshot)`.

**How invalidation works** — `_load_config_impl()` (config.py:7562):

```python
st = config_path.stat()
user_sig: Optional[Tuple[int, int]] = (st.st_mtime_ns, st.st_size)   # 7569-7570
...
managed_sig = (mst.st_mtime_ns, mst.st_size) if mst else (0, 0)      # 7582-7583
cache_sig = (user_sig[0], user_sig[1], managed_sig[0], managed_sig[1])  # 7590-7595
cached = _LOAD_CONFIG_CACHE.get(path_key)
if cached is not None and cache_sig is not None and cached[:4] == cache_sig:
    env_snapshot = cached[5] ...
    if all(os.environ.get(k) == v for k, v in env_snapshot.items()):
        return copy.deepcopy(cached[4]) if want_deepcopy else cached[4]  # 7601-7610
```

On miss it does `copy.deepcopy(DEFAULT_CONFIG)` → `fast_safe_load(f)` → `_deep_merge` → normalize → `_expand_env_vars` → managed overlay → store `_LOAD_CONFIG_CACHE[path_key] = (*cache_sig, cached_copy, env_snapshot)` (config.py:7612-7693).

**Is it polling?** Yes. Every `load_config()` call does a fresh `config_path.stat()` (line 7569). There is no OS-level watch. Two *extra* invalidation signals beyond the user file's mtime/size are folded in:
1. The **managed-scope** config file signature (config.py:7574-7585) — editing `/etc/hermes/config.yaml` also busts the cache.
2. An **env-ref snapshot** (config.py:7608-7609, populated at 7690-7692) — if a `${VAR}` the config was expanded against changed value in `os.environ` (late `.env` load, in-process rotation — issue #58514), the cache is treated as stale even though the file is byte-identical.

`read_raw_config()` (config.py:7317-7352) uses the identical pattern with `_RAW_CONFIG_CACHE`, keyed on `str(config_path)`, value `(mtime_ns, size, parsed)` (lines 7332-7351). Even the parse-failure warn-once set is `(str(config_path), st.st_mtime_ns, st.st_size)`-keyed (config.py:39, 126).

---

## Q2: Does mtime auto-invalidation apply to ALL config keys?

**Yes — the cache is whole-file; there is no per-key exclusion.** The cached object is the single fully-merged dict (`DEFAULT_CONFIG` + user YAML + managed overlay). Any edit to *any* key changes the file's mtime/size and forces a full re-read + re-merge on the next call. That includes `plugins.enabled` and `platform_toolsets` — neither is excluded.

One nuance: `plugins` and `platform_toolsets` are **not** in `DEFAULT_CONFIG` at all. They're listed in `_EXTRA_KNOWN_ROOT_KEYS` with the comment "intentionally absent from DEFAULT_CONFIG" (config.py:5745-5747), so they arrive only via the user YAML through the same `_deep_merge` (config.py:7626) and are cached as part of the same dict. There is no separate cache for them, and no section is carved out of `_LOAD_CONFIG_CACHE`.

**Caveat — the cache is whole-file, but downstream derived caches can hold stale state:**
- The tool-schema memo in `model_tools.py` (`_tool_defs_cache`, line 270) is keyed on `(frozenset(enabled_toolsets), frozenset(disabled_toolsets), registry._generation, cfg_fp, ...)` — and `cfg_fp` is recomputed as `(cfg_path.stat().st_mtime_ns, st_size)` on every quiet-mode call (model_tools.py:320-336). So config edits *do* bust this cache via the same mtime signal, and `registry._generation` bumps on register/deregister (tools/registry.py:300, 457, 523).
- The PluginManager's `_discovered` flag (plugins.py:1286) is the one genuinely sticky piece — see Q3.

---

## Q3: Does a `plugins.enabled` change between API calls reach `discover_and_load()`?

**No — not in a long-lived process, unless `force=True` is passed.** The config *data* refreshes; the plugin *sweep* does not re-run. Full trace:

**1. Discovery only happens once per process.** `discover_and_load()` (plugins.py:1279) has a re-entrancy guard:

```python
def discover_and_load(self, force: bool = False) -> None:
    if self._discovered and not force:      # plugins.py:1286
        return
```

The gateway calls it exactly once, at startup, without force — gateway/run.py:8046-8048:

```python
from hermes_cli.plugins import discover_plugins
discover_plugins()
```

All other calls are also force-less and idempotent (the module wrapper `discover_plugins()` at plugins.py:2040-2046; `_ensure_plugins_discovered()` at plugins.py:2330-2337; the "discovery timing pitfall" sites in model_tools.py:214-215, tools_config.py:234, 249, etc.). The only production `force=True` caller in the codebase is the dashboard basic-auth setup (hermes_cli/main.py:13881); the rest are tests.
