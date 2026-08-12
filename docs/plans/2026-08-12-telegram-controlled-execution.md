# Hermes Telegram controlled-execution reliability

Status: draft implementation, not deployed

## Observed behavior

Telegram jobs using a local `ornith:35b` endpoint can remain in a no-output
state for several minutes because the implicit local stream stale detector is
disabled. The same interaction can then retry a large shell heredoc against a
memory-like path, leaving a partial document and allowing the assistant to
report progress as if the artifact were complete.

## Correction set

1. Keep dangerous-command approval enabled; route ordinary document writes to
   `write_file`/`patch` and verify the result before claiming completion.
2. Give implicit local model calls a finite stale timeout, configurable through
   `agent.local_stale_timeout_seconds`, defaulting to 300 seconds. Explicit
   provider/model stale settings and existing environment overrides continue to
   win.
3. Default gateway and cron sessions to the existing tool-loop hard stop;
   interactive CLI/TUI remains warning-only, and an explicit
   `tool_loop_guardrails.hard_stop_enabled` value overrides the default.
4. Add regression coverage for local non-stream and stream timeout resolution,
   unattended loop behavior, and the tool guidance that distinguishes durable
   memory from documents.

## Evidence boundary

This draft addresses the code paths visible in the screenshots. It does not
prove DGX runtime behavior until the built release is deployed and the Telegram
path is exercised end to end.
