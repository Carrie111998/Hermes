# tests/gateway monolithic run — handoff note

Launched 2026-08-11 08:49 local. Expect ~3h (~9896 tests at ~1 test/sec).

- Worktree (isolated, NOT the shared primary checkout):
  `C:\Users\diego\.hermes\agent-src\.claude\worktrees\eager-kepler-ddb335` @ e3bfc2ebc
- Driver: `%LOCALAPPDATA%\Temp\claude\C--Users-diego--hermes-agent-src--claude-worktrees-eager-kepler-ddb335\003d1a6f-a82c-49ce-9131-4c57377e5e3a\scratchpad\run_gw_full2.cmd`
- Log: `gw_full2.log` (in this worktree)
- Sentinel: `gw_full2.done` — written ONLY after pytest exits; contains `EXIT=<code>` + END timestamp.
- pytest PID at launch: 42044 (trampoline parent 53384)

Command actually run (cmd.exe redirection, so tracebacks are NOT reflowed by the
PowerShell formatter):

    python.exe -u -m pytest tests/gateway -p no:cacheprovider -q -rf --tb=short --timeout=900

`CLAUDE_CODE_ENTRYPOINT`, `CLAUDECODE`, `HERMES_DISABLE_MESSAGE_TRIGRAM` cleared by the driver.

## If you are a later session

The process SURVIVES the session that launched it. Do not assume a stopped
background task means a stopped process.

1. `Test-Path gw_full2.done` — if present, the run finished; read it for the exit code.
2. Otherwise check the log's size + mtime and whether a python PID is still alive.
   A growing log = still running.
3. `+++ Timeout +++` at the tail with no summary means pytest_timeout called
   `os._exit(1)` and killed the whole run — raise `--timeout` and restart.
