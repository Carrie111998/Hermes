# CLI Engineering Guide

Root [`AGENTS.md`](../AGENTS.md) still applies. This file owns classic CLI,
configuration, setup, profile, and skin rules under `hermes_cli/` and `cli.py`.

## Command registry

`hermes_cli/commands.py::COMMAND_REGISTRY` is the single command catalog.
CLI dispatch, gateway recognition/help, Telegram menus, Slack routing,
autocomplete, and CLI help derive from it.

Add a command by:

1. adding one `CommandDef`;
2. handling its canonical name in `HermesCLI.process_command()`;
3. adding a gateway handler only when the command is gateway-capable.

Aliases belong only in the existing `CommandDef`. Persistent settings use the
existing configuration writers.

## Configuration ownership

- Behavioral settings belong in `config.yaml`.
- `.env` is for credentials: API keys, tokens, and passwords.
- Add defaults to `hermes_cli/config.py::DEFAULT_CONFIG`.
- Bump `_config_version` only for a migration or structural transformation;
  deep-merged additive keys do not need a bump.
- Credential metadata belongs in `OPTIONAL_ENV_VARS`.

There are three loaders. Verify every consumer you change:

| Loader | Consumers |
|---|---|
| `cli.py::load_cli_config()` | interactive CLI |
| `hermes_cli/config.py::load_config()` | setup, tools, most subcommands |
| direct YAML via `gateway/config.py` | gateway runtime |

CLI work uses the process cwd. Messaging work uses `terminal.cwd`; deprecated
cwd environment variables are not configuration surfaces.

## Profile-safe paths

Use `get_hermes_home()` for state and `display_hermes_home()` in messages.
Never hardcode `~/.hermes`. Profile management roots remain HOME-anchored by
design.

## Skin system

`hermes_cli/skin_engine.py` owns data-driven skins. Shared behavior belongs in
`SkinConfig` and the built-in data map; user skins are YAML under the active
Hermes home. Missing values inherit from the default skin.

Do not add code paths for a data-only theme. `/skin` and `display.skin` are the
activation surfaces.

## Terminal UI pitfalls

- New interactive menus use `hermes_cli/curses_ui.py`; do not add
  `simple_term_menu` call sites.
- Under `prompt_toolkit.patch_stdout`, do not emit ANSI erase-to-EOL
  (`\033[K`); overwrite with carriage return plus space padding.
- Keep output encoding and configuration reads tolerant of UTF-8 BOM and
  legacy `.env` encodings where those files can be edited by Windows tools.
- Probe external commands with `shutil.which()` and provide a platform-native
  fallback rather than assuming POSIX utilities exist.
