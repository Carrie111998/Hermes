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

## Live subscription correction RED/GREEN

The uncommitted correction WIP was RED on inspection because it called the
unsupported `agy auth status --json`, passed the human model label instead of
the exact `gemini-3.1-pro-high` ID, and exposed incomplete effort orderings.
Its successful qualifications also described `overage_disabled` too broadly
instead of identifying it as fail-closed subscription-route policy evidence.

The Claude live receipt was rechecked at 09:47 ICT and retained only these
non-sensitive fields:

```text
{"loggedIn":true,"authMethod":"claude.ai","apiProvider":"firstParty"}
```

Email and organization fields were neither printed nor persisted. The current
worktree shell did not resolve `agy`, so the supplied live `agy --version`,
`agy models`, and help receipts were incorporated through deterministic
qualification tests without attempting the nonexistent auth command or an
inference.

GREEN at 09:49 ICT:

```text
uv run --with pytest pytest tests/hermes_cli/fleet -q
101 passed in 7.49s
```

This includes exact Antigravity argv coverage, exact model-list qualification,
second-highest effort selection for all four live profiles, API environment
scrubbing, and fail-closed policy checks.

The 198-test regression subset was rerun at 09:51 ICT:

```text
uv run --with pytest pytest tests/hermes_cli/test_config_validation.py tests/hermes_cli/test_subparser_routing_fallback.py tests/hermes_cli/test_commands.py -q
198 passed in 13.25s
```
