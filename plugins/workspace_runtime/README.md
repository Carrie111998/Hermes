# workspace_runtime — Hermes plugin

> Automatic Workspace Discovery for Hermes.

## What it does

When Hermes starts a fresh session, this plugin discovers whether the cwd
is inside a Workspace OS workspace rooted by the canonical four-file bootstrap
and surfaces a stable verdict block on the
first user message. The verdict answers nine questions automatically:

1. Did the cwd land inside a Workspace?
2. Where is the root of that Workspace?
3. Where is `GOVERNANCE/`?
4. Where is `CONTEXT/`?
5. Where is the active project (`.project-state/`)?
6. Where is canonical authority (`IDENTITY.md` / `ARCHITECTURE.md`)?
7. What if this is **not** a Workspace?
8. What if the Workspace is **partial** (missing files)?
9. What if **multiple** ancestor roots qualify?

## How it fits

This is a **runtime layer** that lives between Hermes and Workspace OS. It does
NOT redefine Workspace OS. Workspace OS remains the canonical operating system
that lives at `/home/taras/projects/GOVERNANCE/` and `/home/taras/projects/workspace-os/`.

When the verdict is `inside_workspace`:

- The four canonical bootstrap authorities are loaded once at session start.
- A bounded authority cache plus parsed workspace-index summary reaches the API-bound first user turn.
- An unambiguous cwd-bound mission loads only its `source-task.md` and `progress.md`.

When the verdict is `not_a_workspace`:

- The model sees the workspace-discovery block explaining absence.
- Workspace OS is **not applied**.
- Read-only tools continue normally; state-changing actions require explicit operator confirmation.

When the verdict is `partial_workspace` or `multi_workspace`:

- The model sees each missing signal / each candidate root.
- Operator action is required.

## Verdict block format

Every first user message is prefixed with:

```
<workspace-runtime-verdict
  state="..."
  cwd="..."
  duration_ms="..."
  ...
>
  <operator-readable body>
</workspace-runtime-verdict>
```

The block uses stable XML-like tags so the model can pattern-match the
prefix reliably across turns.

## Hooks

| Hook | Purpose |
|---|---|
| `on_session_start` | Run discovery, store verdict, write telemetry. |
| `pre_llm_call` | Augment the first user message of the session with the verdict block. |

The plugin does NOT mutate the system prompt and does NOT touch the
prefix cache. The first-turn user-message augmentation is the lowest-
impact place to surface a verdict block while keeping Hermes' prompt
construction byte-stable across turns.

## Where it does NOT touch

- `~/.hermes/SOUL.md` (unchanged).
- `agent/_cached_system_prompt` (unchanged).
- `~/.hermes/state.db` (NOT read or written).
- The kernel source tree of Workspace OS (`/home/taras/projects/workspace-os/`) is read-only reference; no writes.

## Telemetry

After every session start, the plugin writes a one-line JSON record to
`$HERMES_HOME/workspace_runtime/last_discovery.json` (debug-only, no secrets).
This file is overwritten on each session so it always reflects the latest
discovery state.

## Tests

```
PYTHONPATH=/home/taras/.hermes/hermes-agent/plugins/workspace_runtime \
  pytest /home/taras/.hermes/hermes-agent/plugins/workspace_runtime/tests
```

Current automated coverage includes discovery behavior, hook delivery,
session isolation and idempotency, canonical bootstrap loading, mission
continuation, bounded API context, telemetry, canonical Workspace smoke, and
failure containment.

## Companion documentation

- `workspace-os/docs/BOOTSTRAP-PROCEDURE.md` — kernel-side implementation spec
  referenced when Workspace OS is applied.
- `GOVERNANCE/BOOTSTRAP.md` — canonical 4-file bootstrap recipe.
- `GOVERNANCE/CONTEXT-ROUTING.md` — context routing rules.
- `GOVERNANCE/WORKSPACE-CONSTITUTION.md` — Article V (Identity), Article VII
  (Sprint Pattern), Article XIV (Mission Execution), Article XV (Memory Boundary).

## Mission context

This plugin is the output of mission
`/home/taras/projects/.project-state/workspace-runtime-cold-start-2026-07-25/`.
The mission is logged there with eight iterations documenting the
canonical-signal selection, multi-root tie-break, partial-workspace
tolerance, and not-a-workspace behaviour.
