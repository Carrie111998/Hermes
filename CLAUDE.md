# hermes-agent

macOS 27 (Tahoe) beta injects `MallocStackLogging` (and
`MallocStackLoggingNoCompact`) into GUI-session process environments, and
the beta enables "lite mode" malloc stack logging on mere presence of the
variable (value ignored, even `"no"`). Every spawned subprocess then
emits two `comm(pid) MallocStackLogging: …` lines to stderr — which
Hermes merges into stdout. Without intervention those lines contaminate:

- Terminal-tool captures (cat/sha256sum/wc output)
- `read_file` line-numbered output (the `N|` gutter prefix)
- The `sha256` post-write verification in `file_operations` (noise
  appended after the payload → false `Post-write verification failed`)
- Anything else that ingests subprocess stdout

## Defense layers

Three layers, each closes a different leak path. **All three are required.**

1. **Streaming strip inside `_wait_for_process._drain()`**
   (`tools/environments/base.py`, `_MslStreamStripper`) — line-buffered
   stripper that runs in all three drain branches (POSIX select, Windows
   blocking, iterable fallback). MSL never enters the bounded collector,
   so the 40/60 head/tail budget stays intact for real output. Fast path
   skips the regex when `"MallocStackLogging"` is absent in `carry +
   chunk`. **Primary defense; must remain the first to touch the bytes.**

2. **`strip_malloc_stack_logging` post-capture**
   (`tools/environments/base.py`, end of `BaseEnvironment.execute()`) —
   safety net for any path that bypasses `_drain()` (custom subprocess
   adapters, file-ops `cat`, code-exec RPC). Idempotent and cheap.

3. **Hook subprocess strip** (`agent/shell_hooks.py`, after
   `proc.communicate()` in `_spawn`) — hook scripts bypass
   `BaseEnvironment.execute()` entirely; their stdout/stderr flows back
   to the model unfiltered unless stripped at this boundary.

## When adding new subprocess or capture code

- Anything that calls `subprocess.Popen`/`subprocess.run` and captures
  stdout/stderr must go through `BaseEnvironment.execute()` OR strip MSL
  itself via `tools.environments.base.strip_malloc_stack_logging`.
- Anything that bypasses the bounded collector (unbounded capture,
  custom adapter, async stream) needs to run its own line-buffered
  `_MslStreamStripper` to keep MSL from evicting real output.
- Do NOT add a fourth post-strip in `execute()` thinking "more is
  better" — the existing Layer 2 is already redundant with Layer 1;
  adding another just costs regex time.

## When adding new tests for MSL

`tests/tools/test_base_environment.py::TestStripMallocStackLogging` is
the regex-matrix gate; `::TestMslStreamStripper` covers carry/flush/fast
path; `::TestMslDoesNotEvictRealTail` is the direct regression for the
"every tool call failed" symptom (real payload + 30 KB MSL flood → both
sentinels survive). Keep all three.