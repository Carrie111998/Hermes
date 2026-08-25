# Security Model — unlazy / gate-check

`gate-check.mjs` executes shell commands taken from `GATES.md` ledgers. A ledger is
therefore code: anyone who can edit a ledger you run can execute commands as you.
This document states the boundaries the tooling enforces and the ones it does not.

## Treat every CHECK as untrusted code

- Before running an inherited or repository-provided ledger, parse it without
  executing anything: `gate-check.mjs --status GATES.md`. Read every `CHECK:`
  command and every script it calls.
- Approve only commands you wrote yourself or fully understand. In a repository
  you do not trust, do not run `--approve` at all — review first, or rewrite the
  gates in your own words.

## What approvals bind

Approvals live outside the repository (default `~/.unlazy/approved/`) so a
repository cannot pre-approve its own commands. Each approval is keyed to the
exact combination of:

- ledger identity and gate id
- `CHECK:` command text and `EXPECT:` expectation
- resolved working directory and resolved shell
- timeout, output and regex limits
- platform and the full inherited `PATH`

Changing any bound input invalidates the approval; the command will not run
again until re-approved. This prevents a ledger edit (or a `PATH` / shell swap)
from silently changing what an approved command does.

## What is NOT a security boundary

- **Scopes and leases** (`--claim`, `--release`, `OWNS:` paths) coordinate
  concurrent writers. They are cooperative bookkeeping, not filesystem
  isolation or sandboxing. A misbehaving process can ignore them.
- **The checker itself does not sandbox commands.** An approved `CHECK:` runs
  with your full user privileges. Approval is the safety mechanism, not
  containment.
- **`EXPECT:` matching proves only the declared oracle.** It cannot verify that
  the English gate title describes what the command measures.

## Hook installation cautions

`install-hooks.mjs` writes Claude Code settings that reference absolute Node and
hook-script paths:

- Default (project-local) install writes machine-specific values into
  `.claude/settings.local.json`. Keep `.claude/settings.local.json`, `.unlazy/`,
  and `.unlazy-hook-state.json` in the project's ignore rules so they are never
  committed or shared.
- Shared installation embeds absolute paths and is usually not portable across
  machines; prefer project-local unless you control every machine involved.
- Never install the Stop hook without the user's explicit consent, and remove it
  with `--uninstall` when the pipeline is done.

## Practical checklist for an untrusted repository

1. `--status` only; never `--approve` on first contact.
2. Read each `CHECK:` and every script it invokes (follow the file, not the name).
3. Prefer re-authoring gates over inheriting them.
4. Verify `CWD:` and shell resolution printed by the checker match what you expect.
5. Remember `~/.unlazy/approved/` persists across sessions — clean up approvals
   for ledgers you no longer trust.
