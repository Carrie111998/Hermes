---
name: thread-scope
description: "Ground progress answers in this scope's own tracked work."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [scope, session, progress, discord, gateway]
    related_skills: []
---

# Thread Scope Skill

Hermes conversations that share a repository, a profile, or even a Discord
channel are not automatically the same piece of work. This skill governs
when to check or update the current conversation's **scope** — the durable
record of what this specific thread/session owns — so a progress question
gets answered from artifacts this conversation actually produced, never
from activity that merely happens to be nearby (another thread, another
tmux session, another branch in the same repo).

## When to Use

- The user asks a progress/status question: "where are we on this", "is
  this done", "what's left", "what have you done so far".
- You are about to start non-trivial work spanning more than one turn (a
  bug fix, a feature, a multi-step task) and there is no scope yet for this
  conversation.
- You manually created an artifact `hermes scope`'s auto-registration
  cannot see — a git branch, a worktree, or a PR (see Prerequisites).

Do NOT create a scope for every message — only when the user is opening a
real piece of work you might later be asked to report progress on.

## Prerequisites

- `hermes scope` is a CLI subcommand (see `terminal`), not a model tool —
  there is no dedicated tool call for it.
- Auto-registration already links tmux/terminal sessions, delegations, and
  cron jobs you create through Hermes's own paths to the active scope — you
  do not need to link those manually.
- Branches, worktrees, and PRs are **never** auto-linked (Hermes has no
  durable registry for them). Link them yourself with `hermes scope link`
  whenever you create one as part of this scope's work.

## How to Run

Check whether this conversation already has a scope:

```bash
hermes scope status
```

If it reports "scope unknown" and you're starting real work, create one:

```bash
hermes scope create --goal "<one-line description of what this conversation is for>"
```

Link a manually created artifact:

```bash
hermes scope link branches "fix/ssl-cert-bug"
hermes scope link worktrees "/path/to/worktree"
hermes scope link prs "https://github.com/org/repo/pull/123"
```

Record something you're blocked on (kept separate from verified progress):

```bash
hermes scope dependency "waiting on infra team to rotate the cert"
```

Close it out when the work is done:

```bash
hermes scope complete   # or: hermes scope archive
```

## Quick Reference

| Situation | Command |
|---|---|
| "What's the status of this?" | `hermes scope status` |
| Starting a new multi-turn task | `hermes scope create --goal "..."` |
| Just created a branch/worktree/PR | `hermes scope link <category> <value>` |
| Blocked on something external | `hermes scope dependency "<description>"` |
| Work is done | `hermes scope complete` |
| Full unredacted detail (debugging) | `hermes scope audit` |

## Procedure

1. Before answering a progress/status question, run `hermes scope status`.
   Its `owned artifacts` section is this conversation's verified progress —
   report from there, not from `session_search` results or from other
   tmux sessions/branches you happen to notice in the repository.
2. `session_search` results are conversation history, not proof of live
   state or of what this conversation owns — never cite a hit from another
   thread/session as if it were this conversation's own work, even when
   it looks directly relevant. `session_search` already scopes discovery
   to this thread by default on platforms with threads; only pass
   `include_unscoped=true` when the user explicitly asks you to search
   other conversations.
3. If `hermes scope status` reports "scope unknown," say so plainly rather
   than substituting a guess built from whatever else is active in the
   repo or profile — that guess is exactly the failure this skill exists
   to prevent.
4. When you create a branch, worktree, or PR as part of this scope's work,
   link it immediately with `hermes scope link` — don't wait until a
   status question forces you to reconstruct what happened.
5. An external dependency (waiting on another team, a pending review, an
   infra change) is not progress — record it with `hermes scope
   dependency` and keep it out of your progress claims.

## Pitfalls

- Don't create a new scope for every turn — reuse the existing one for the
  life of the conversation/thread; `hermes scope create` is idempotent by
  identity, so calling it again on an existing scope is harmless but
  usually unnecessary.
- Don't pass `--account-id`/`--guild-scope-id` to `hermes scope create`
  unless you will always address this scope by its explicit `--scope-id`
  afterward — the live session context does not carry those fields, so a
  scope created with either set cannot be found again by a bare `hermes
  scope status` or by auto-registration.
- Don't treat a `hermes scope status` artifact count as proof something is
  still running — the counts include a best-effort liveness check for
  tmux sessions, delegations, and cron jobs, but branches/worktrees/PRs
  have no liveness signal at all; verify those against the actual source
  (`git branch`, the PR page) before reporting them as current.

## Verification

```bash
hermes scope status
```

If it prints a goal, lifecycle, and owned-artifact counts, the scope is
tracked correctly. "scope unknown" with no `--scope-id` means either no
scope has been created for this conversation yet, or you're on a platform
(bare CLI, some DMs) with no thread identity to key one on.
