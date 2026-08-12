# Task 1 Report — Public explicit source policy

## RED

Command:

```text
scripts/run_tests.sh tests/hermes_cli/test_kanban_cli.py -k 'source_policy' -q; scripts/run_tests.sh tests/tools/test_kanban_tools.py -k 'create_source_policy or invalid_source_policy' -q
```

Expected failure demonstrated: CLI rejected `--source-policy` as an unrecognized argument (4 failing tests); tool handler ignored policy values and accepted an invalid value (3 failing tests).

## GREEN

Commands and results:

- `scripts/run_tests.sh tests/hermes_cli/test_kanban_cli.py -k 'source_policy' -q` — 4 passed.
- `scripts/run_tests.sh tests/tools/test_kanban_tools.py -k 'create_source_policy or invalid_source_policy' -q` — 4 passed.
- `scripts/run_tests.sh tests/hermes_cli/test_kanban_cli.py -q` — 37 passed.
- `scripts/run_tests.sh tests/tools/test_kanban_tools.py -k 'create and source' -q` — 4 passed.
- `git diff --check` — passed.

## Changed paths

- `hermes_cli/kanban.py`
- `tools/kanban_tools.py`
- `tests/hermes_cli/test_kanban_cli.py`
- `tests/tools/test_kanban_tools.py`
- `.superpowers/sdd/2026-08-12-default-board-source-flow/task-1-report.md`

## Commit

`7d92ccd1adf2292dec7c57b4be3d36bfb0b6dc51` — `fix(kanban): expose default source policy`

## Concerns

None. No Product-v2 schema or `hermes_cli/kanban_db.py` changes were made. The full repository test suite was not run; the brief-required affected suites and diff check passed.
