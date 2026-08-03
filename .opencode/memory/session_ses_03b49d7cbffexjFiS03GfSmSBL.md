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
Read the file /home/capeta/Code/hermes-agent/hermes_cli/plugins.py thoroughly. I need to find:

1. The exact line(s) where plugin modules are imported/executed (spec.loader.exec_module, importlib, import_module, etc.)
2. How modules end up in sys.modules - what naming convention?
3. Any list/dict of loaded plugin module objects accessible from PluginManager
4. The `_discovered` attribute - where it's set to True
5. The `_plugins` dict - what's stored in it
6. The `discover_and_load` method - especially if there's a `force=True` path that clears caches
7. The `register()` method and where tool handlers are stored
8. Any use of importlib.reload anywhere in the file

For each finding, cite the exact file path and line number(s). Return the relevant code excerpts.

---

ASSISTANT:
The file is 2,466 lines. Let me read it thoroughly in chunks.

---

ASSISTANT:
I've read the entire file. Let me verify a couple of specific patterns with targeted searches to make sure I haven't missed anything.

---

ASSISTANT:
I've read the entire file (2,466 lines) and verified patterns with targeted searches. Note: `/home/capeta/Code/hermes-agent/hermes_cli/plugins.py` and `/var/scabbard/@Code/hermes-agent/hermes_cli/plugins.py` are the same file (`readlink` resolves both to `/var/scabbard/@Code/hermes-agent/hermes_cli/plugins.py`), so all citations below use the canonical path.

# Findings: `hermes_cli/plugins.py`

## 1. Where plugin modules are imported/executed

There is no `import_module`/`__import__` for plugins — loading is done via the `importlib.util` spec machinery. The core execution happens in `_load_directory_module()`:

**`/var/scabbard/@Code/hermes-agent/hermes_cli/plugins.py:1852-1868`**
```python
1852:        key = manifest.key or manifest.name
1853:        slug = key.replace("/", "__").replace("-", "_")
1854:        module_name = f"{_NS_PARENT}.{slug}"
1855:        spec = importlib.util.spec_from_file_location(
1856:            module_name,
1857:            init_file,
1858:            submodule_search_locations=[str(plugin_dir)],
1859:        )
1860:        if spec is None or spec.loader is None:
1861:            raise ImportError(f"Cannot create module spec for {init_file}")
1862:
1863:        module = importlib.util.module_from_spec(spec)
1864:        module.__package__ = module_name
1865:        module.__path__ = [str(plugin_dir)]  # type: ignore[attr-defined]
1866:        sys.modules[module_name] = module
1867:        spec.loader.exec_module(module)
1868:        return module
```

The **single `exec_module` call** is at **line 1867**. Entry-point (pip) plugins load via `ep.load()` instead:

**`/var/scabbard/@Code/hermes-agent/hermes_cli/plugins.py:1870-1886`**
```python
1880:        for ep in group_eps:
1881:            if ep.name == manifest.name:
1882:                return ep.load()
```

The orchestration entry point is `_load_plugin()` (lines 1748-1830), which dispatches to `_load_directory_module` (line 1765) for `user`/`project`/`bundled` sources and `_load_entrypoint_module` (line 1767) for `entrypoint`, then calls `register()`:

**`/var/scabbard/@Code/hermes-agent/hermes_cli/plugins.py:1764-1769`**
```python
1764:            if manifest.source in {"user", "project", "bundled"}:
1765:                module = self._load_directory_module(manifest)
1766:            else:
1767:                module = self._load_entrypoint_module(manifest)
1768:
1769:            loaded.module = module
```

## 2. sys.modules naming convention

The namespace parent is the module constant `_NS_PARENT`:

**`/var/scabbard/@Code/hermes-agent/hermes_cli/plugins.py:219`**
```python
219: _NS_PARENT = "hermes_plugins"
```

The namespace package is created if absent, then each plugin is registered under `hermes_plugins.<slug>` where the slug is the manifest key with `/` → `__` and `-` → `_`:

**`/var/scabbard/@Code/hermes-agent/hermes_cli/plugins.py:1846-1854`**
```python
1846:        if _NS_PARENT not in sys.modules:
1847:            ns_pkg = types.ModuleType(_NS_PARENT)
1848:            ns_pkg.__path__ = []  # type: ignore[attr-defined]
1849:            ns_pkg.__package__ = _NS_PARENT
1850:            sys.modules[_NS_PARENT] = ns_pkg
1851:
1852:        key = manifest.key or manifest.name
1853:        slug = key.replace("/", "__").replace("-", "_")
1854:        module_name = f"{_NS_PARENT}.{slug}"
```

So a flat plugin `plugins/disk-cleanup/` loads as `hermes_plugins.disk_cleanup`, and a category plugin `plugins/image_gen/openai/` loads as `hermes_plugins.image_gen__openai` (preventing collision with a hypothetical `tts/openai`). The module is inserted into `sys.modules` at **line 1866** (`sys.modules[module_name] = module`) — before `exec_module`.

## 3. Loaded module objects accessible from PluginManager

The `LoadedPlugin` dataclass holds the module reference:

**`/var/scabbard/@Code/hermes-agent/hermes_cli/plugins.py:317-332`**
```python
317: @dataclass
318: class LoadedPlugin:
319:     """Runtime state for a single loaded plugin."""
320:
321:     manifest: PluginManifest
322:     module: Optional[types.ModuleType] = None
323:     tools_registered: List[str] = field(default_factory=list)
324:     hooks_registered: List[str] = field(default_factory=list)
325:     middleware_registered: List[str] = field(default_factory=list)
326:     commands_registered: List[str] = field(default_factory=list)
327:     enabled: bool = False
328:     error: Optional[str] = None
329:     # True for a bundled platform plugin recorded as a deferred (not-yet-
330:     # imported) loader. ...
332:     deferred: bool = False
```

There is **no separate list of module objects** — the module objects are reachable via `manager._plugins[<key>].module` (set at line 1769). The manager dict itself is declared in `__init__`:

**`/var/scabbard/@Code/hermes-agent/hermes_cli/plugins.py:1252`**
```python
1252:         self._plugins: Dict[str, LoadedPlugin] = {}
```

The module objects also live in `sys.modules` under the `hermes_plugins.*` names (line 1866).

## 4. The `_discovered` attribute

Declared at **line 1260**:
```python
1260:         self._discovered: bool = False
```

Set to `True` in three places:
- **Line 1290** — `HERMES_SAFE_MODE` short-circuit path:
```python
1288:        if env_var_enabled("HERMES_SAFE_MODE"):
1289:            logger.info("HERMES_SAFE_MODE=1 — plugin discovery skipped")
1290:            self._discovered = True
1291:            return
```
- **Line 1310** — the up-front re-entrancy guard (a plugin's `register()` can transitively trigger discovery again):
```python
1310:        self._discovered = True
1311:        try:
1312:            self._discover_and_load_inner()
1313:        except BaseException:
1314:            self._discovered = False
1315:            raise
```
- Reset to `False` at **line 1314** if the sweep raises (so a failed scan isn't cached as "discovered with an empty registry").

Checked at **line 1286**:
```python
1286:        if self._discovered and not force:
1287:            return
```

## 5. The `_plugins` dict

Defined at **line 1252** (above). It maps `lookup_key = manifest.key or manifest.name` → `LoadedPlugin`. Populated at these sites:

| Line | Context |
|---|---|
| 1393 | Disabled via config (`enabled=False`, `error="disabled via config"`) |
| 1405 | `kind == "exclusive"` (memory providers — recorded for introspection, not loaded) |
| 1420 | `kind == "model-provider"` (recorded; loaded by `providers/` discovery) |
| 1464 | Not enabled (`error="not enabled in config ..."`) |
| 1724 | Deferred bundled platform plugin placeholder (`deferred=True`) |
| 1830 | Fully loaded plugin in `_load_plugin` |

The final storage line:
```python
1830:        self._plugins[manifest.key or manifest.name] = loaded
```

The `lookup_key` computation is at line 1386 (`lookup_key = manifest.key or manifest.name`) and line 1384 (`winners[manifest.key or manifest.name] = manifest`), with key derivation in `_parse_manifest` at line 1581: `key = f"{prefix}/{plugin_dir.name}" if prefix else name`.

## 6. `discover_and_load(force=True)` — clears caches

**`/var/scabbard/@Code/hermes-agent/hermes_cli/plugins.py:1279-1315`** (full method):
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
1304:        # Set the flag up front as a re-entrancy guard ...
1310:        self._discovered = True
1311:        try:
1312:            self._discover_and_load_inner()
1313:        except BaseException:
1314:            self._discovered = False
