# Hermes CLI Command Registration Pattern

## How to Add a New Subcommand to `hermes_cli/main.py`

### Step 1: Create command module

Create `hermes_cli/commands/<name>.py` (or `hermes_cli/subcommands/<name>.py`):

```python
"""CLI command for <name>."""

def build_<name>_parser(subparsers, cmd_<name>=None):
    """Register `hermes <name>` subcommands."""
    parser = subparsers.add_parser("<name>", help="...")
    sub = parser.add_subparsers(dest="<name>_command")

    # Subcommands
    list_p = sub.add_parser("list", aliases=["ls"], help="...")
    list_p.add_argument("--json", dest="as_json", action="store_true")

    show_p = sub.add_parser("show", help="...")
    show_p.add_argument("name", help="...")

    if cmd_<name> is not None:
        parser.set_defaults(func=cmd_<name>)
    return parser

def cmd_<name>(args):
    sub = getattr(args, "<name>_command", None)
    if sub == "list": return _cmd_list(args)
    if sub == "show": return _cmd_show(args)
    return 0
```

### Step 2: Register in main.py

In `main()`, find the Plugin CLI section and add BEFORE it:

```python
# =========================================================================
# <name> command
# =========================================================================
from hermes_cli.commands.<name> import build_<name>_parser, cmd_<name>

build_<name>_parser(subparsers, cmd_<name>=cmd_<name>)
```

### Step 3: Add to `_BUILTIN_SUBCOMMANDS`

Find `_BUILTIN_SUBCOMMANDS = frozenset({...})` (around line 11313) and add your command name alphabetically:

```python
_BUILTIN_SUBCOMMANDS = frozenset({
    "acp", "agent", "approvals", ...
})
```

**Why:** This frozenset prevents the CLI from falling through to plugin discovery for built-in commands. Missing an entry costs a one-time import of all plugin modules.

### Step 4: Verify

```bash
cd /mnt/hdd/ares-workspace/hermes-agent
python -m py_compile hermes_cli/commands/<name>.py
python -m py_compile hermes_cli/main.py
python -c "from hermes_cli.commands.<name> import build_<name>_parser, cmd_<name>; print('OK')"
```

## Key Patterns

- **argparse only** — Hermes uses argparse, NOT click
- **`set_defaults(func=handler)`** — connects parser to handler
- **`getattr(args, "xxx_command", None)`** — dispatch subcommands
- **`_BUILTIN_SUBCOMMANDS`** — must be in sync with subparsers.add_parser calls

## Examples in Codebase

- `hermes_cli/subcommands/config.py` — `build_config_parser`
- `hermes_cli/subcommands/plugins.py` — `build_plugins_parser`
- `hermes_cli/commands/agent.py` — `build_agent_parser` (new)
