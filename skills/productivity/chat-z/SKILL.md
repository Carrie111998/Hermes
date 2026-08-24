---
name: chat-z
description: Send prompts through running Hermes Desktop sessions.
version: 1.0.0
author: xxbwx888-cell (GitHub), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Desktop, Sessions, Messaging, Delegation, Productivity]
    category: productivity
    related_skills: [session-librarian]
---

# Chat-Z Skill

Send a prompt through the running Hermes Desktop app to an existing Desktop
session, or create a new Desktop session in a chosen project directory. This
is a fire-and-confirm handoff: it confirms Desktop accepted the prompt but
does not wait for the receiving agent to finish.

## When to Use

- The user asks to send or hand off work to another Hermes Desktop session.
- A workflow needs to create a visible Desktop session in a specific project.
- The receiving session must show the same native running state as a prompt
  entered manually in Desktop.
- The caller should continue immediately after Desktop accepts the prompt.

Do not use this for subagent delegation that must return a result to the
current turn. Use the normal delegation capability for that workflow.

## Prerequisites

- Hermes Desktop is already running and has finished loading.
- The CLI and Desktop are using the same active Hermes profile.
- New sessions require an existing project directory.
- The caller is running as the same OS user as Desktop. Chat-Z is a local
  same-user transport, not a remote authenticated messaging API.

## How to Run

Use `terminal` to invoke `hermes chat-z`. Choose exactly one target:

```text
hermes chat-z --session-id "<stored-session-id>" -q "<prompt>"
hermes chat-z -c "<exact-desktop-title>" -q "<prompt>"
hermes chat-z --new --cwd "<project-directory>" --title "<fixed-title>" -q "<prompt>"
```

For long, multiline, or shell-sensitive prompts, place the prompt in a UTF-8
file and use `--query-file <path>` instead of `-q`.

## Quick Reference

| Option | Purpose |
|---|---|
| `--session-id ID` | Target an existing session by durable stored ID. |
| `-c TITLE` | Target an existing session by its exact Desktop title. |
| `--new` | Create a new Desktop session. |
| `--cwd DIR` | Bind a new session to an existing project directory. |
| `--title TITLE` | Give a new session a stable title. |
| `-q TEXT` | Send prompt text directly. |
| `--query-file PATH` | Read the prompt from a UTF-8 file. |
| `--timeout SECONDS` | Limit the wait for Desktop acceptance, up to 300 seconds. |
| `-Q` | Print nothing after successful acceptance. |

## Procedure

1. Determine whether the target already exists or a new session is required.
2. For an existing session, prefer `--session-id` when an ID is known. Use
   `-c` only with the complete, exact Desktop title.
3. For a new session, resolve the intended existing project directory and
   provide both `--cwd` and `--title`. Do not rely on the first prompt to
   become the title.
4. Send a self-contained prompt. The receiver does not automatically inherit
   the current session's context, files, decisions, or authorization.
5. Inspect the command result:
   - `Accepted by Hermes Desktop` means an existing session accepted it.
   - `Created by Hermes Desktop` means Desktop created and accepted a new
     session. Preserve the printed session ID for future sends.
6. Return control immediately after acceptance unless the user separately
   requested a workflow that collects the receiver's result.

## Pitfalls

- Do not substitute `hermes chat -c`. That path can create or run a CLI
  session without going through Desktop's native submit path.
- Acceptance is not completion. Never report that the receiving agent
  finished merely because Chat-Z returned exit code 0.
- Titles are exact-match targets and can be ambiguous or change. Prefer the
  stored session ID for durable automation.
- `--new` without `--title` lets Desktop derive a title from the prompt, which
  makes later title-based sends fragile.
- A timeout occurs before target lookup. Check that Desktop is running,
  loaded, includes the Chat-Z bridge, and uses the same profile; do not assume
  the target session's source field caused the timeout.
- Do not repeatedly retry an uncertain delivery. One retry after verifying
  Desktop readiness is enough unless the user asks otherwise.

## Verification

Treat the handoff as successful only when the command exits with code 0 and
prints an accepted or created receipt, or exits silently with code 0 under
`-Q`. For a new session, retain the returned session ID and use it for the
next message. If Desktop rejects the request or no receipt arrives, report the
error without claiming delivery.
