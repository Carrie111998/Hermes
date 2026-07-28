# Kanban worker safety

## Problem

Assigned kanban tasks can start with an empty body, external coding workers can share one checkout, and the local `kanban-executor` skill permits destructive shortcuts. This produced overlapping T1/T5 edits and a green PR obtained by deleting tests.

## Scope

Protect future coding tasks and safely restart the current T1/T5 work. Keep the existing CPU quota and `max_in_progress: 3`. Do not add containers or a new orchestration subsystem.

## Design

1. Reject an assigned, non-triage `kanban_create` request when `body` is blank. The dispatcher must also refuse legacy assigned tasks with blank bodies so existing database rows cannot bypass the creation guard.
2. For an existing PC repository, `kanban-executor` must give each task a unique git worktree derived from `HERMES_KANBAN_TASK`; workers must never edit a shared checkout.
3. Remove instructions that bypass destructive-command guards. Forbid `reset --hard` on an existing checkout, broad `git add -A`, and deleting or weakening tests merely to make a run green. Out-of-scope failures are reported and block completion.
4. Stop T1/T5, preserve T1's uncommitted diff and untracked files, then recreate both with complete bodies and clean per-task worktrees. Leave T2/PR #12 untouched. Keep PR #11 open until its replacement is verified; closing or merging PRs is a separate final action.

## Verification

- A focused test proves blank assigned tasks are rejected while explicit triage remains allowed.
- A focused dispatcher test proves legacy blank tasks are not spawned.
- A skill test checks the required per-task worktree and safety rules.
- On the live host, T1/T5 restart with non-empty bodies, distinct worktrees, no shared checkout, at most three workers, and the existing 80% CPU quota.
