---
name: muncho-mac-ops-edge
description: "Use a protected local Mac/browser evidence handoff without exposing credentials to Cloud Hermes."
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [mac, browser, bitrix, local-evidence, handoff]
    related_skills: [browser]
---

# Protected Mac operations edge

Use this edge only when you decide that required evidence is available through
the separately authenticated Mac browser or an explicitly selected local
read-only surface. The edge does not interpret the request and does not choose
whether it is needed; you do.

## Contract

Call `mac_ops_readonly_submit` with:

- one explicit read-only `task_class`;
- a stable idempotency key for the exact contract;
- a complete contract with these headings: `Objective`, `Allowed scope`,
  `Forbidden actions`, `Secrets handling`, `Verification`, `Expected report`.

State the concrete evidence to retrieve and its allowed scope. Forbid writes,
publishing, messaging, approval, configuration, and account changes unless the
user has separately approved a later mutation protocol. Never include tokens,
passwords, cookies, or private keys.

After submission, call `mac_ops_task_read` with the returned issue IID. An open
issue or a queued receipt is not completion. Read the returned evidence, decide
whether it answers the task, and continue through the normal plan. If the edge
reports uncertain dispatch, reconcile the same idempotency key; do not create a
new key to bypass uncertainty.

Keep the interactive model turn available while the local worker runs. Use
`mac_ops_task_read` as a bounded observation, return control to the model after
each read, and decide when another read is useful. Do not replace the structured
read loop with one long foreground shell watcher or a `--wait-closed` command.
Before waiting again, give the user a concise authored update when a meaningful
boundary has changed. Interpret the structured milestone or result; never copy
raw heartbeat lines, worker logs, commands, or credentials into chat. This keeps
steering responsive without turning host output into a synthetic assistant
message.

This first-wave edge is read-only. If the task requires a mutation, explain the
exact blocked mutation and request the applicable owner approval instead of
relabeling it as read-only.
