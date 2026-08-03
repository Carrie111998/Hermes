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
Investigate the Hermes agent plugin reload mechanisms in /home/capeta/Code/hermes-agent/. Answer these specific questions with file:line citations:

1. In hermes_cli/plugins.py, find the PluginManager class. Look at:
   - discover_and_load method (around line 1279) - how is it idempotent? How does force=True work?
   - When loading a plugin (around line 1784), does it cache the module in memory (e.g. importlib)? Does force=True actually re-import a changed __init__.py, or does Python's import cache (sys.modules) prevent it?
   - _ensure_plugins_discovered function (around line 2330) - how does force parameter work?

2. Search for all call sites where discover_and_load(force=True) is called. What CLI commands, API endpoints, or slash commands trigger a force reload?

3. Search for all call sites where _ensure_plugins_discovered(force=True) is called. Is it ever called with force=True by the running gateway (gateway/run.py), or only from setup/CLI?

4. Search for "/reload-mcp" in cli.py around line 6329 - how does it work? Does it reload plugins too?

For each finding, provide the exact file:line and the relevant code snippet. Be thorough - check imports, decorators, and all call sites.

---

ASSISTANT:
I'll investigate the plugin reload mechanisms in the Hermes agent codebase. Let me start with the key files and locations.

---

ASSISTANT:
Both paths exist. Let me read the key sections of plugins.py.

---

ASSISTANT:
Now let me read the `_load_plugin` method (around line 1784) to understand module caching and imports.

---

ASSISTANT:
Now let me check for sys.modules manipulation and the `_NS_PARENT` constant, plus find all call sites.

---

ASSISTANT:
Let me narrow the searches to `force=True` specifically and check line 2046 in plugins.py.

---

ASSISTANT:
Let me search more broadly for force reload call sites, including `discover_plugins(force=True)` and any positional/variable forms.

---

ASSISTANT:
Let me get the full list of `discover_plugins` call sites with context.

---

ASSISTANT:
Let me read the context around the `discover_plugins(force=True)` call in main.py, and search for force=True in gateway and cli.py.

---

ASSISTANT:
Let me examine the gateway call sites and the `/reload-mcp` command.

---

ASSISTANT:
Let me read the `/reload-mcp` implementation in cli.py at both locations.

---

ASSISTANT:
Let me find the `_confirm_and_reload_mcp` and `_reload_mcp` implementations.

---

ASSISTANT:
Let me check the remaining `discover_plugins()` call sites and search for any plugin-reload commands in the gateway, TUI, and web server.

---

ASSISTANT:
The rtk tee is truncating output. Let me use plain rg directly.

---

ASSISTANT:
Let me examine the four production `_ensure_plugins_discovered(force=True)` call sites and the gateway/TUI reload paths.

---

ASSISTANT:
Let me check the gateway and TUI reload paths to see if they touch plugin discovery with force.

---

ASSISTANT:
Let me check the TUI gateway reload path and confirm the `/reload-mcp` command definition.

---

ASSISTANT:
Let me look at the CommandDef definition for reload-mcp and check whether the `_handle_reload_mcp_command` in gateway references plugins at all.

---

ASSISTANT:
Let me find `_handle_reload_mcp_command` definition in the gateway.

---

ASSISTANT:
Let me verify the remaining call sites don't pass force, and check for any plugin reload web/API endpoints.

---

ASSISTANT:
Let me check the context of the remaining call sites and search for any plugin-reload web endpoints.

---

ASSISTANT:
Let me check the `hermes plugins` CLI commands and web server for any force reload triggers.

---

ASSISTANT:
Let me look at plugins_cmd.py line 2018 (the `--force` flag path) and check the `hermes plugins` subcommand registry.

---

ASSISTANT:
Let me check whether `cmd_install` triggers a force plugin reload after install.

---

ASSISTANT:
Let me check the remaining call sites and search the web server for plugin-related API endpoints.

---

ASSISTANT:
Let me check the remaining call sites and verify the web_server plugin context.

---

ASSISTANT:
Let me check web_server.py:1091 context and confirm `refresh_agent_mcp_tools` doesn't touch plugins.

---

ASSISTANT:
Let me confirm security_audit's `_discover_plugins` is a local helper and check for any `plugins reload` subcommand.

---

ASSISTANT:
Let me do a final verification — check for plugin reload endpoints in web_server.py and any `/plugins` slash command with force.

---

ASSISTANT:
Let me do final checks — the gateway's HookRegistry at run.py:8106, and any plugin command handlers in cli.py.

---

ASSISTANT:
Let me verify gateway/hooks.py's `discover_and_load` is a different class (HookRegistry, not PluginManager).

---

ASSISTANT:
Let me do final comprehensive searches for any plugin reload API endpoints in web/TUI/desktop surfaces.

---

ASSISTANT:
Let me confirm the function containing main.py:13881 and double-check the TUI RPC and gateway MCP reload paths don't call plugins.

---

ASSISTANT:
The rtk output truncation is interfering. Let me use the Grep tool directly.

---

ASSISTANT:
The rtk output is being truncated oddly. Let me use a direct approach with awk to find the enclosing function.

---

ASSISTANT:
The rtk wrapper is truncating. Let me use the Grep tool directly on the file path.

---

ASSISTANT:
The grep tool is searching the directory despite the file path. Let me use bash to find the enclosing function directly.

---

ASSISTANT:
The grep tool seems to be searching broadly. Let me do a final verification on the web_server reload endpoints and the TUI RPC registration.

---

ASSISTANT:
There's a `plugins.manage` RPC in the TUI gateway. Let me examine it.

---

ASSISTANT:
Let me check `dashboard_set_agent_plugin_enabled` to see if toggle triggers a force reload.
</conversation_update>

</existing_memory>

<conversation_update>
ASSISTANT:
I now have a complete picture. Here is my thorough investigation report.

---

# Hermes Agent Plugin Reload Mechanisms — Investigation Report

All paths relative to `/var/scabbard/@Code/hermes-agent/` (same tree as `/home/capeta/Code/hermes-agent/`).

## 1. `PluginManager` in `hermes_cli/plugins.py`

### 1a. `discover_and_load()` — idempotency & `force=True`

**`hermes_cli/plugins.py:1279-1315`**

```python
1279:    def discover_and_load(self, force: bool = False) -> None:
1280:        """Scan all plugin sources and load each plugin found.
1281:
1282:        When ``force`` is true, clear cached discovery state first so config
1283:        changes or newly-added bundled backends become visible in long-lived
1284:        sessions without requiring a full agent restart.
1285:        """
1286:        if self._discovered and not force:
1287:            return
1288:        if env_var_enabled("HERMES_SAFE_MODE"):
1289:            logger.info("HERMES_SAFE_MODE=1 — plugin discovery skipped")
1290:            self._discovered = True
1291:            return
1292:        if force:
1293:            self._plugins.clear()
1294:            self._hooks.clear()
1295:            self._middleware.clear()
1296:            self._plugin_tool_names.clear()
1297:            self._plugin_platform_names.clear()
1298:            self._cli_commands.clear()
1299:            self._plugin_commands.clear()
1300:            self._plugin_skills.clear()
1301:            self._aux_tasks.clear()
1302:            self._slack_action_handlers.clear()
1303:            self._context_engine = None
1304:        # Set the flag up front as a re-entrancy guard (a plugin's register()
1305:        # can transitively trigger discovery again), but reset it if the sweep
1306:        # raises so a failed scan is NOT cached as "discovered with an empty
1307:        # registry" ...
1310:        self._discovered = True
1311:        try:
1312:            self._discover_and_load_inner()
1313:        except BaseException:
1314:            self._discovered = False
1315:            raise
```

**Idempotency mechanism:** The `_discovered` flag (init at line 1260, `self._discovered: bool = False`). If discovery already ran and `force` is falsy, line 1286 returns immediately. It is set `True` *before* the sweep (line 1310) as a re-entrancy guard (a plugin's `register()` can trigger discovery again transitively), and reset to `False` on exception (line 1314) so a failed scan isn't cached as "discovered with empty registry."

**`force=True` behavior:** Lines 1292–1303 wipe all of the manager's in-memory state dicts (`_plugins`, `_hooks`, `_middleware`, `_plugin_tool_names`, `_plugin_platform_names`, `_cli_commands`, `_plugin_commands`, `_plugin_skills`, `_aux_tasks`, `_slack_action_handlers`, and `_context_engine`) before re-running `_discover_and_load_inner()`. Note: it does **not** clear `sys.modules` (see 1b).

### 1b. Module loading — does `force=True` re-import a changed `__init__.py`?

**`hermes_cli/plugins.py:1748-1768`** (`_load_plugin`) delegates to `_load_directory_module` (directory/user/project/bundled) or `_load_entrypoint_module` (pip).

**Directory plugins — `_load_directory_module`, `hermes_cli/plugins.py:1832-1868`:**

```python
1832:    def _load_directory_module(self, manifest: PluginManifest) -> types.ModuleType:
1833:        """Import a directory-based plugin as ``hermes_plugins.<slug>``. ..."""
1840:        plugin_dir = Path(manifest.path)
1841:        init_file = plugin_dir / "__init__.py"
1842:        if not init_file.exists():
