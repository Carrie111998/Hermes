# Task 7 repository-baseline waiver evidence

Date: 2026-07-18 (America/New_York)

Starting HEAD: `acef3b0b70dbf7b1aca827e1ef8ef5856c1e2902`

Feature comparison range: `ff3e132..2b90de64a`
Host: Windows, PowerShell, CPython 3.12 from the repository's locked uv environment

This is a bounded non-regression waiver, not a claim that the complete
repository suite is green. It records the exact limits and failures observed
outside the cumulative Session Bridge feature diff.

## Locked environment and collection

The test environment was synchronized without updating the lock:

```powershell
uv sync --locked --extra dev --extra all
```

Result: exit 0 in 20.4 seconds; the existing lock resolved 234 packages.

An initial whole-repository collection exposed one stale test import:

```powershell
uv run --locked --extra dev --extra all python -m pytest --collect-only -qq
```

Result before correction: exit 1 in 284.6 seconds, solely because
`tests/gateway/platforms/test_telegram_network.py` imported the deleted module
`gateway.platforms.telegram_network`. The implementation had moved to
`plugins.platforms.telegram.telegram_network`.

The stale import was corrected test-first:

1. Targeted pre-change run: exit 1 during collection with
   `ModuleNotFoundError: gateway.platforms.telegram_network`.
2. Only the import path in the test was changed.
3. Targeted post-change run: `2 passed` in 3.46 seconds (exit 0; 10.2 seconds
   command wall time).
4. Fresh whole-repository collection using the exact command above: exit 0 in
   89.4 seconds. Collection therefore succeeds under locked `dev+all` extras.

Warnings during collection were existing FastAPI deprecations, unknown pytest
marks, a non-collectable helper class, and denied writes to `.pytest_cache`;
none caused collection failure after the stale import correction.

## Bounded four-worker diagnostic

The repository runner discovered 2,041 files using its default policy (which
excludes its documented `integration`, `e2e`, and `docker` directories). To
avoid another ambiguous interrupted run, the runner's own deterministic sorted
discovery order was bounded to exactly its first 229 files and passed back via
the supported `--files` option:

```powershell
$files = uv run --locked --extra dev --extra all python -c \
  "from pathlib import Path; import scripts.run_tests_parallel as r; root=Path.cwd(); print(':'.join(str(p.relative_to(root)) for p in r._discover_files([root/'tests'])[:229]))"
uv run --locked --extra dev --extra all python scripts/run_tests_parallel.py \
  -j 4 --files $files -q
```

Exact boundary:

- selected files: 229 of 2,041 default-discovered files;
- first: `tests/acp/test_approval_isolation.py`;
- last: `tests/agent/test_tool_result_classification.py`;
- first deliberately unrun file: `tests/agent/test_trace_upload.py`;
- worker count: exactly 4;
- command wall time: 963.6 seconds;
- completed-file count: exactly 229;
- files with actual test failures: 17;
- actual failed tests: 43;
- files with no test summary (timeout before completion): 2;
- aggregate pass/skip totals for the 229-file run are unavailable because the
  tool transport truncated the runner's aggregate summary. They are not
  inferred or presented as a green full-suite result.

A bounded follow-up over only the 17 failing files, used to classify the
failure signatures, reported `43 failed, 429 passed, 3 skipped` in 169.21
seconds. Those follow-up pass/skip counts apply only to those 17 files and are
not totals for the 229-file diagnostic.

## Exact failure inventory

| File | Failed tests | Observed failure signature |
|---|---:|---|
| `tests/acp/test_edit_approval.py` | 1 | Windows newline translation made six logical characters seven bytes. |
| `tests/acp/test_ping_suppression.py` | 1 | Windows Proactor registered an invalid closed handle (`WinError 6`). |
| `tests/acp_adapter/test_acp_images.py` | 3 | Windows `file:///C:/...` URIs were incorrectly resolved as `/mnt/c/...`. |
| `tests/agent/lsp/test_workspace.py` | 1 | Tilde expansion used Windows profile semantics instead of the test's patched POSIX `HOME`. |
| `tests/agent/test_codex_app_server_persist.py` | 1 | Windows held the SQLite state file open (`WinError 32`) during cleanup. |
| `tests/agent/test_compression_concurrent_fork.py` | 1 | Timing-sensitive lease/lock refresh assertion failed under the loaded Windows run. |
| `tests/agent/test_context_references.py` | 1 | Sensitive-home path expectation differed under Windows path normalization. |
| `tests/agent/test_credential_pool.py` | 2 | Concurrent disk merge/removal persistence expectations failed on the Windows filesystem path. |
| `tests/agent/test_file_safety_sandbox_mirror.py` | 5 | Sandbox-mirror shape matching expected POSIX separators/paths. |
| `tests/agent/test_image_routing.py` | 7 | Image-reference extraction expectations used POSIX absolute/home paths and separators. |
| `tests/agent/test_proxy_and_url_validation.py` | 3 | Malformed proxy-port validation differed for the Windows environment variables. |
| `tests/agent/test_save_url_image.py` | 1 | Hermes-home/cache path resolution differed on Windows. |
| `tests/agent/test_shell_hooks.py` | 7 | Hook subprocess, executable, matcher, and payload tests rely on POSIX shell/path behavior. |
| `tests/agent/test_shell_hooks_consent.py` | 2 | Script mtime lookup returned `None` for Windows/tilde paths. |
| `tests/agent/test_skill_utils.py` | 2 | One separator mismatch plus unavailable unprivileged Windows symlink creation; the fallback also lacked a `pytest` import. |
| `tests/agent/test_skill_commands.py` | 3 | Supporting-file separators and inline `echo`/`pwd` expansion assumed POSIX shell behavior. |
| `tests/agent/test_subdirectory_hints.py` | 2 | Terminal command path extraction did not recognize embedded Windows paths. |

The runner also classified these two files as "no tests ran" because their
per-file processes exceeded the runner's 300-second cap before emitting a
pytest summary:

| File | Failed-test count | Observed boundary |
|---|---:|---|
| `tests/agent/test_auxiliary_main_first.py` | 0 reported | Timed out while local-server type detection performed synchronous HTTP probing. |
| `tests/agent/test_model_metadata.py` | 0 reported | Timed out before collection/execution summary during local model metadata/server probing. |

The 19 paths above are the complete non-zero/no-summary inventory from the
bounded 229-file run. No `tests/session_bridge` file or Session Bridge source
path failed.

## Cumulative-diff and cleanliness proof

Each of the 19 paths was checked against the cumulative feature range with:

```powershell
git diff --name-only ff3e132..2b90de64a -- <all-19-paths>
```

Result: no output. The cumulative range contains 43 changed paths, and none of
the waived failure/no-summary paths is one of them.

Before writing this evidence, `git status --short` contained only the
authorized stale-import correction in
`tests/gateway/platforms/test_telegram_network.py`. The duplicate diagnostic
runner and its descendants were stopped as one owned process tree; no unrelated
runner, service, source file, test, or configuration was changed. The first
229-file run itself left the worktree clean.

## Waiver conclusion

The repository-wide execution baseline is not green: this bounded diagnostic
observed 43 failures in 17 files and two per-file timeouts. The evidence supports
only a Session Bridge non-regression waiver because every observed path is
outside `ff3e132..2b90de64a`, the locked environment now collects successfully,
and no Session Bridge path appeared in the failure inventory.
