---
sidebar_position: 4
title: "Incognito Mode"
description: "Run a temporary Hermes session without Hermes-managed memory or session persistence"
---

# Incognito Mode

Incognito mode runs a Hermes session without using Hermes-managed memory or
session persistence. It is intended for prompts that should not become part of
Hermes's remembered context or session history.

> **Important:** Incognito mode disables Hermes-managed memory and session
> persistence. It does **not** sandbox terminal, browser, filesystem, or
> external services.

## When to use it

Use incognito mode for one-off work that should not be remembered by Hermes,
such as:

- exploring a sensitive question without adding it to persistent memory;
- inspecting temporary or personal material for a single task;
- running a disposable prompt from a script or CI job;
- testing a workflow without creating a resumable Hermes session.

Incognito is a per-invocation mode. It does not change your profile's
configuration or delete existing memory and sessions.

## How to enable it

Use `--incognito` with the regular entry point or with `hermes chat`:

```bash
hermes --incognito
hermes chat --incognito
hermes chat --incognito -q "Summarize this temporary input"
hermes --incognito -z "Return a one-shot answer"
```

The flag is also available to the TUI path when the TUI is launched for the
invocation:

```bash
hermes --incognito --tui
```

## What incognito disables

### Hermes-managed memory

Incognito disables the `memory` toolset for the run. Hermes does not read or
write its built-in memory stores, and it does not commit the turn to a
configured Hermes memory provider.

This means that preferences, facts, or lessons discussed during the run are
not added to Hermes memory for later sessions. Existing memory is left
unchanged.

### Session persistence

Incognito does not create or update the Hermes-managed session record for the
run. In particular, it does not persist the conversation to the profile's
session database or transcript files, and it does not leave an incognito
session available in the session list.

Existing sessions and their stored messages are not modified. Incognito also
does not remove old sessions, memory files, or other data that was already
present before the run.

### Background review

Incognito skips the post-turn background review that can otherwise perform
Hermes-managed memory or skill-related review work. No background review is
started for the incognito session.

### Resume and continue

An incognito session is not resumable. `--resume` and `--continue` are rejected
when used with `--incognito`, rather than silently starting a different kind of
session.

For example:

```bash
hermes --incognito --continue
# Error: incognito sessions cannot be resumed.
```

## What incognito does not disable

Incognito is not a sandbox or a general-purpose privacy boundary. The agent
still has whatever tools and permissions are enabled for the invocation.

In particular:

- `terminal` can still execute commands and read or write files;
- browser tools can still access websites and browser state;
- filesystem access through terminal, `read_file`, `write_file`, or `patch`
  is not generally disabled;
- enabled plugins, MCP servers, and external integrations are not
  automatically made private or ephemeral;
- network requests can still reach external services;
- external services may retain prompts, tool inputs, outputs, logs, or account
  activity according to their own policies;
- operating-system logs, shell history, model-provider logs, proxy logs, and
  other application logs are outside the Hermes-managed session contract.

For example, the terminal may still read or modify files under `~/.hermes` (or
under the active profile's `$HERMES_HOME`) if the agent runs a command that
does so. Incognito prevents Hermes's session and memory lifecycle from writing
there; it does not prevent a tool from accessing that directory.

If the agent is allowed to use a tool, treat that tool as allowed to perform
its normal operation. Incognito does not override tool approval settings,
operating-system permissions, provider retention policies, or external service
behavior.

## Typical patterns

### Temporary interactive investigation

```bash
hermes --incognito
```

Use this when you want a normal interactive session but do not want the turn to
be remembered or appear in Hermes session history. Export or copy any result
you want to keep explicitly; Hermes will not offer the incognito conversation
for resume later.

### Disposable one-shot command

```bash
hermes chat --incognito -q "Review this temporary input and list the risks"
```

This is useful for scripts and short-lived analysis. The answer is returned to
the caller, but the Hermes-managed conversation is not persisted.

### Temporary work with terminal access

```bash
hermes chat --incognito -q "Inspect the current repository and explain the failing test"
```

The agent can still inspect the repository and, if permitted, change files.
Use a separate terminal sandbox or isolated environment when the task must not
be able to affect the host filesystem.

## Known limitations

- Incognito is not retroactive: it does not erase data already stored in
  `state.db`, `sessions/`, memory files, logs, provider caches, or external
  services.
- Incognito is not a guarantee that no bytes are written anywhere on the
  machine. It only covers Hermes-managed memory and session persistence for
  the incognito lifecycle.
- A response, tool output, or generated artifact that you explicitly save
  remains saved. The terminal can write to ordinary files, including files
  inside `$HERMES_HOME`.
- External memory providers and other integrations can have their own storage
  and retention behavior. Incognito prevents Hermes from committing the turn
  through its memory lifecycle; it cannot retract data already sent to an
  external service or control that service's retention policy.
- The mode is per invocation and is not a persistent configuration switch.
  Start a new invocation with `--incognito` whenever you need it.

## Stronger isolation: future work

The current incognito mode deliberately reuses Hermes's existing memory and
session-persistence controls. Stronger isolation would require additional
boundaries that are **not implemented by this feature**:

- launching the run with a temporary `HERMES_HOME` and removing it afterward;
- using an overlay or copy-on-write filesystem for Hermes state and the working
  directory;
- restricting or replacing host-reaching terminal tools and their permitted
  paths;
- applying equivalent isolation to browser profiles, MCP servers, plugins,
  and external services.

These are future-work directions, not behavior provided by `--incognito`. For
stronger containment today, use an isolated terminal backend or another
sandboxing mechanism appropriate for your threat model, and separately review
the retention policies of any external service involved.
