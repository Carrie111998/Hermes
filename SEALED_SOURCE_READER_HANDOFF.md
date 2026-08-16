# Sealed Source Reader and Preclaim Contract — Handoff

Baseline candidate: `79b2ea22795ef76969ea15c1bab9b9837adc44a6`
Branch: `wt/t_91654f11`
PR: https://github.com/NousResearch/hermes-agent/pull/86164

## Implemented

- `kanban_read_source` remains task-scoped, attachment-root confined, digest/size pinned, and cursor authenticated.
- A source-bearing or repository-bound task now resolves one effective worker runtime contract before claim and carries it into the run receipt.
- The receipt is bound to task ID, run ID, required capabilities, sealed source IDs, checked repository SHA/workspace, runtime backend, claim lock, and claim expiry.
- Runtime source reads reject an altered backend, expired or changed claim, changed capability/source snapshot, missing/mismatched repository HEAD, or altered receipt binding.
- Non-local workers are denied before task transition or `task_runs` insertion whenever a source manifest or required repository SHA needs host-path materialization. The durable code is `remote_materialization_unsupported` with the selected backend.
- The effective CLI toolset allowlist is captured alongside requested top-level toolsets. Config validation warns when an explicit platform allowlist shadows requested toolsets.

## Verification

Executed from the implementation worktree with an explicit temporary Kanban DB, board name, attachments root, and workspaces root:

```text
.venv/bin/python -m py_compile hermes_cli/kanban_db.py hermes_cli/config.py tests/hermes_cli/test_kanban_db.py
.venv/bin/python -m pytest -o 'addopts=' -q tests/hermes_cli/test_kanban_db.py tests/hermes_cli/test_config_validation.py
47 passed, 1 skipped in 1.54s
```

The live `~/.hermes/kanban.db` SHA-256 was unchanged across that test execution. `git diff --check` is also clean.

## Reviewer focus

1. Validate the strict receipt fields against the AC-1–AC-13 contract, particularly repository `HEAD` checks and the environment backend pin.
2. Verify the remote materialization boundary: remote source/repository tasks must deny without a run, while local task-scoped source reads continue to work.
3. Confirm shadow warnings are advisory only and do not re-enable `file` or `terminal` for a platform allowlist that omits them.

No deployment, merge, incident retry, live-board mutation, or profile/config rollout was performed.
