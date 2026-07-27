# Rebranding and Migration Guide

## Identity

The independent project is Charterforge. The accepted decision and collision
check are in
[Decision 0001](decisions/0001-charterforge-identity.md).

## State migration

Stop all processes before moving state.

```bash
test ! -e "$HOME/.charterforge"
cp -a "$HOME/.hermes" "$HOME/.charterforge"
CHARTERFORGE_HOME="$HOME/.charterforge" uv run charterforge doctor
```

The copy-first procedure preserves rollback. Do not delete `~/.hermes` until
the new command has passed integrity checks and real external state has been
verified. On Windows, copy `%LOCALAPPDATA%\hermes` to
`%LOCALAPPDATA%\charterforge`.

`CHARTERFORGE_HOME` takes precedence over the legacy `HERMES_HOME`. Other
canonical variables use `CHARTERFORGE_`; the canonical entry point maps them
to inherited internal readers during the transition.

## Automation migration

Replace:

| Legacy | Canonical |
|---|---|
| `hermes` | `charterforge` |
| `hermes-agent` | `charterforge-agent` |
| `HERMES_*` | `CHARTERFORGE_*` |
| `~/.hermes` | `~/.charterforge` |
| image/container `hermes-agent` / `hermes` | `charterforge` |
| `hermes-kanban-dispatcher.service` | `charterforge-kanban-dispatcher.service` |

The Python `hermes_cli` namespace remains an internal compatibility layer.
New extensions should import public Charterforge modules when they exist and
must not claim official Hermes affiliation.

## Repository migration

The configured independent remote currently retains the historical
`hermes-agent` repository slug. The product and package are still
Charterforge. Renaming the hosted repository is an external administrative
action and was not performed by these code changes.

Never push Charterforge-specific commits to the `upstream` remote.

