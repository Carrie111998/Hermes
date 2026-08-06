# Configuration Reference

Full detail on `config.yaml` sections, `.env` policy, the three config loaders,
and working-directory resolution.

> Extracted from `AGENTS.md`. Load this file when working in this area.

---

### config.yaml options:
1. Add documented/defaulted keys to `DEFAULT_CONFIG` in
   `hermes_cli/config.py`. It is the primary known-root/default source, not a
   universal schema: `_EXTRA_KNOWN_ROOT_KEYS` and `read_user_config_raw()` cover
   intentionally absent, dynamic, or presence-sensitive roots.
2. Bump `_config_version` (check the current value at the top of `DEFAULT_CONFIG`)
   ONLY if you need to actively migrate/transform existing user config
   (renaming keys, changing structure). Adding a new key to an existing
   section is handled automatically by the deep-merge and does NOT require
   a version bump.

### Top-level `config.yaml` sections (non-exhaustive):

`model`, `agent`, `terminal`, `compression`, `display`, `stt`, `tts`,
`memory`, `security`, `delegation`, `smart_model_routing`, `checkpoints`,
`auxiliary`, `curator`, `skills`, `gateway`, `logging`, `cron`, `profiles`,
`plugins`, `honcho`.

`auxiliary` holds per-task overrides for side-LLM work (curator, vision,
embedding, title generation, session_search, etc.) — each task can pin
its own provider/model/base_url/max_tokens/reasoning_effort. See
`agent/auxiliary_client.py::_resolve_auto` for resolution order.

`curator` holds the background skill-maintenance config —
`enabled`, `interval_hours`, `min_idle_hours`, `stale_after_days`,
`archive_after_days`, `backup` (nested).

### .env variables

New credentials (API keys, tokens, passwords) are declared in
`OPTIONAL_ENV_VARS`; do not add new env-only behavioral configuration:
1. Add to `OPTIONAL_ENV_VARS` in `hermes_cli/config.py` with metadata:
```python
"NEW_API_KEY": {
    "description": "What it's for",
    "prompt": "Display name",
    "url": "https://...",
    "password": True,
    "category": "tool",  # provider, tool, messaging, setting
},
```

Non-secret settings (timeouts, thresholds, feature flags, paths, display
preferences) canonically belong in `config.yaml`, not `.env`. Legacy/internal,
platform, and plugin behavioral env variables still exist (for example
`HERMES_BACKGROUND_NOTIFICATIONS` and `HERMES_CRON_TIMEOUT`); preserve needed
compatibility, but bridge new user-facing behavior from config rather than
presenting runtime env as secret-only.

### Config loaders (three paths — know which one you're in):

| Loader | Used by | Location |
|--------|---------|----------|
| `load_cli_config()` | CLI mode | `cli.py` — merges CLI-specific defaults + user YAML |
| `load_config()` | `hermes tools`, `hermes setup`, most CLI subcommands | `hermes_cli/config.py` — merges `DEFAULT_CONFIG` + user YAML |
| Direct YAML load | Gateway runtime | `gateway/run.py` + `gateway/config.py` — reads user YAML raw |

If a new key appears in one surface but not another, trace the actual loader.
Check `DEFAULT_CONFIG` and the raw/presence-sensitive paths rather than assuming
one merged schema governs every runtime.

### Working directory:
- **CLI** — uses the process's current directory (`os.getcwd()`).
- **Messaging** — uses `terminal.cwd` from `config.yaml`. The gateway bridges this
  to the `TERMINAL_CWD` env var for child tools. **`MESSAGING_CWD` has been
  removed** — the config loader prints a deprecation warning if it's set in
  `.env`. Same for `TERMINAL_CWD` in `.env`; the canonical setting is
  `terminal.cwd` in `config.yaml`.

### Background process notifications

`display.background_process_notifications` controls gateway messages for
tracked background commands:

- `all` — running-output updates and the final message (default)
- `result` — only the final completion message
- `error` — only a non-zero-exit final message
- `off` — no watcher messages

`HERMES_BACKGROUND_NOTIFICATIONS` remains an internal/backward-compatible
override; user-facing configuration belongs in `config.yaml`.
