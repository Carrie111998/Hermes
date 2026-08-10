# Subagent personas

A **persona** is a named, reusable configuration for a delegated subagent: a
markdown file with YAML frontmatter that supplies the child's standing system
prompt and, optionally, a narrowed set of toolsets.

```markdown
---
name: scout
description: Read-only reconnaissance inside one repo or directory tree.
toolsets: [file, terminal]
required_toolsets: [file]
reasoning_effort: medium
max_iterations: 30
---
You are a read-only scout. Answer the orchestrator's question with evidence,
without dumping whole files back into its context.

- Never create, edit, or delete files; never change git state.
- Return conclusions, not contents: lead with the direct answer, then evidence
  as `path/file.ext:line` references.
- End with one line on what you did NOT check.
```

Invoke it by name:

```
delegate_task(goal="Where is retry handled in the upload path?", agent="scout")
```

`agent` also works per task, so one batch can fan out different personas:

```
delegate_task(tasks=[
    {"goal": "Where is retry handled in the upload path?", "agent": "scout"},
    {"goal": "Try to break the retry fix that just landed",  "agent": "critic"},
])
```

A per-task `agent` overrides the top-level one. An unknown name fails the whole
call rather than silently spawning an unscoped child.

## Why personas exist

Subagents inherit the parent's toolsets and cannot be given a narrower set by
the model — toolset selection is a capability-scoping decision the model does
not control. That left a gap: there was no way for a *user* to obtain a
genuinely read-only subagent either.

A persona closes that gap without reopening the original problem. The scope is
written by a human, to disk, before any input is seen; the model may only pick
among the personas that already exist. Narrowing is still intersected with the
parent's toolsets, so a persona can only ever **reduce** capability.

> **Scope, precisely:** a persona's `toolsets` is a real capability boundary
> because a human fixed it in advance. It is not a defense against a
> compromised orchestrator choosing *which* persona to invoke. Treat persona
> scoping as blast-radius control, not as a sandbox for untrusted input.

## Where personas live

| Location | Scope | Priority |
| --- | --- | --- |
| `<workdir>/.hermes/agents/` | Current project | 1 (highest) |
| `~/.hermes/agents/` | All your projects | 2 |

Both directories are scanned recursively, so `agents/review/security.md` works.
Identity comes from the `name` field (falling back to the filename), not the
path. When both scopes define the same name, the project copy wins — check
project personas into version control so a repo can pin its own reviewer.

## Frontmatter reference

| Field | Type | Description |
| --- | --- | --- |
| `name` | string | Persona identity. Defaults to the filename stem. Lowercase letters, digits, `-`, `_`. |
| `description` | string | What this persona is for. Helps you (and the model) pick the right one. |
| `toolsets` | list or CSV | Toolsets the child may use. Intersected with the parent's — never widening. Omit to inherit everything the parent has. |
| `required_toolsets` | list or CSV | Subset of `toolsets` the persona cannot work without. If the parent lacks one, the spawn fails loudly instead of producing a crippled child. |
| `reasoning_effort` | string | Effort level for this persona's children (`low`…`max`), overriding `delegation.reasoning_effort`. Breadth recon rarely needs the same tier as deep review. |
| `max_iterations` | int | Turn cap for this persona's children, overriding `delegation.max_iterations`. |

The body below the frontmatter is the child's system prompt. It must not be empty.

### `required_toolsets` — why it matters

Because children intersect with the parent, a persona invoked beneath an
already-narrowed parent silently loses tools it declares. A `coder` persona
spawned under a read-only parent would become a read-only `coder` that fails
for reasons the user cannot see — while the persona file on disk still claims
it can write.

Declaring `required_toolsets` turns that silent contract violation into an
immediate, explanatory error:

```
Persona 'coder' requires toolset(s) file, which the parent agent does not have
enabled. Subagents can only narrow, never widen, the parent's toolsets —
enable them for the parent, or use a persona that does not require them.
```

## Error behavior

An unknown persona name returns the available ones rather than a bare failure,
because an unrecognized-name error is otherwise indistinguishable from a broken
environment and the caller will retry identically:

```
Unknown agent 'scot'; available: critic, implementer, scout. Personas are
markdown files with YAML frontmatter in: /repo/.hermes/agents, /home/u/.hermes/agents.
```

A malformed persona file is skipped with a warning (logged once per process)
rather than disabling delegation — one bad file must not take the feature down.

## Example personas

Copy these into `~/.hermes/agents/` as a starting point. See
`docs/subagent-personas/` in this repo.

- **scout** — read-only recon; returns `path:line` anchors, never file dumps.
- **critic** — adversarial verifier; must produce a concrete failing scenario
  or PASS.
- **implementer** — one bounded task in one repo, with verification required
  before it reports done.
