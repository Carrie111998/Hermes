# Hermes Telegram controlled-execution reliability

Status: merged and deployed; Telegram end-to-end readiness still unconfirmed

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

## Deployment evidence

- PR #10 merged to `main` as
  `af99f0f1ad52e266fcc2cfbf261e1ee9f71e39c2`.
- CI run `31571694814` completed successfully.
- DGX release snapshot
  `/home/cwliao/.hermes/releases/v2026.8.12-telegram-controlled-af99f0f1ad`
  matches the merge commit.
- `hermes-gateway.service` is active after restart with MainPID `4109761`,
  `ExecMainStatus=0`, and `NRestarts=0`.

## Evidence boundary

The implementation addresses the code paths visible in the screenshots. The
service/release path is verified. Telegram end-to-end message delivery and
polling readiness remain unconfirmed because the observed post-restart logs
showed connection initialization but not a successful polling confirmation.
