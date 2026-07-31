# Gateway Engineering Guide

Root [`AGENTS.md`](../AGENTS.md) still applies. This file owns messaging gateway
and platform-adapter invariants.

## Control-message path

An active session has two sequential guards:

1. `gateway/platforms/base.py` may queue messages in `_pending_messages`;
2. `gateway/run.py` intercepts control commands before
   `running_agent.interrupt()`.

Any approval or control command that must work while the agent is blocked must
bypass both guards and dispatch inline. Do not send it through
`_process_message_background()`, which races session lifecycle.

## Profile and credential isolation

State paths use `get_hermes_home()`. Platform adapters with unique credentials
acquire a scoped token lock during connect/start and release it during
disconnect/stop. Follow an existing adapter such as IRC.

Messaging terminal work uses `terminal.cwd` from `config.yaml`; the gateway
bridges it to child tools. Do not restore removed cwd environment settings as
user-facing configuration.

## Background completion

`terminal(background=true, notify_on_complete=true)` starts a watcher and a new
agent turn on completion. `display.background_process_notifications` controls
whether users receive all updates, only the result, only errors, or nothing.
Preserve the final result/error semantics when changing watcher code.

## Delivery and alternation

Gateway deliveries must preserve assistant/user/tool role alternation. Cron
results stay in their own framed cron session rather than being mirrored into
an unrelated live gateway conversation.

Exercise adapter changes through the real base-adapter and runner path; a test
that covers only one guard is incomplete.
