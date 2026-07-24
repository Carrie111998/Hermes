# Fleet router builder RED/GREEN receipts

Date: 2026-07-24 ICT

## RED

Command:

```text
uv run --with pytest pytest tests/hermes_cli/fleet -q
```

Receipt at 09:14 ICT:

```text
ModuleNotFoundError: No module named 'hermes_cli.subcommands.fleet'
1 error in 0.35s
```

After adding the production CLI module, the same command exposed a second
collection defect in the recovered WIP:

```text
ImportError: attempted relative import with no known parent package
1 error in 0.37s
```

The import was corrected without adding `__init__.py` to the test tree.

## GREEN

Same command at 09:18 ICT:

```text
........................................................................ [ 79%]
...................                                                      [100%]
91 passed in 6.03s
```

This receipt is from the focused fleet suite and includes the CLI read-only
status assertion added during recovery.

## CLI/config regressions

Command:

```text
uv run --with pytest pytest tests/hermes_cli/test_config_validation.py tests/hermes_cli/test_subparser_routing_fallback.py tests/hermes_cli/test_commands.py -q
```

Receipt at 09:20 ICT:

```text
198 passed in 27.37s
```

The broader batch including `tests/hermes_cli/test_config.py` produced
`3 failed, 378 passed, 1 skipped in 24.96s`. The failures are outside this
feature: one platform-default path expectation conflicts with the current
Windows Hermes home, and two shell-round-trip tests require `sh`, which is not
installed in this PowerShell environment.

The installed `uv run hermes` console script belongs to the separate installed
worktree and was intentionally not modified. The worktree-local entrypoint was
smoke-tested directly:

```text
uv run python -c "import sys; from hermes_cli.main import main; sys.argv=['hermes','fleet','--help']; main()"
```

It exited 0 and listed exactly `status,doctor,plan,run,audit,release`.

After the final CLI refinements, the mandated focused command was rerun:

```text
91 passed in 4.67s
```
