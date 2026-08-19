---
name: continual-harness
description: "Reversible self-improvement for Hermes: take a /refine, undo it safely if the review fork writes something wrong. Use when the user runs /refine and wants a safety net, or asks how to revert a memory/skill change Hermes made."
version: 1.0.0
author: Hermes Agent + Lunar Port
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [self-improvement, refine, rollback, safety, memory, skills]
    homepage: https://github.com/NousResearch/hermes-agent
    related_skills: [note-taking, software-development/definition-of-done]
---

# Continual Harness — Reversible `/refine`

Hermes learns from experience: `/refine` runs a background review fork that
may write new or updated **memory** entries and **skills**. Those writes are
powerful but, by default, irrevocable. This skill adds a **snapshot-before-
write + atomic-restore** safety net so a `/refine` run can be undone.

## How it works

- When you run `/refine`, Hermes snapshots your **memory dir** and **skills
  dir** *synchronously, before* the background fork starts writing.
- The fork runs as normal (it never touches the live conversation or prompt
  cache).
- When the review reports its summary, it ends with `· undo: /refine undo`.
- `agent/refine_rollback.py` (stdlib-only, fully unit-tested) stores
  snapshots under `HERMES_HOME/review_snapshots/<id>/` with a per-session
  index.

## Undo

```
/refine undo
```

Restores the **most recent** snapshot taken for the current session. It is
per-session and idempotent. Only files are restored (memory `.md` + skill
files); the SQLite cognitive store is intentionally excluded (matches the
file-only v1 scope).

### Data-loss guard

If a snapshot's stored tree is missing files that its manifest promised
(e.g. snapshot storage got corrupted or partially written), restore will
**refuse to wipe** the live directory for that target and reports it instead
of silently emptying your memory/skills. Run the undo hint again only after
investigating `~/.hermes/review_snapshots/<id>/`.

## Configuration

The snapshot is **opt-in per `/refine` call** (no config needed). The harness
is engaged on the user-triggered `/refine` path; automatic post-turn reviews
do not snapshot (they are low-risk nudges, and you can always `/refine undo`
the manual one after reviewing the auto result manually).

## CLI helper (advanced)

`skills/continual-harness/scripts/refine_rollback_cli.py` is a standalone
inspector you can run without the agent:

```
python skills/continual-harness/scripts/refine_rollback_cli.py list
python skills/continual-harness/scripts/refine_rollback_cli.py restore <id>
python skills/continual-harness/scripts/refine_rollback_cli.py delete <id>
```

It imports the same `agent.refine_rollback` module the runtime uses, so
behavior is identical. Use it to audit or clean up old snapshots.

## Notes / limits

- Snapshots are NOT git-backed (Hermes home is not a repo). Cleanup is manual
  via the CLI helper or by deleting `HERMES_HOME/review_snapshots/`.
- A snapshot that captured an *empty* target at snapshot time will restore to
  empty for that target (legitimate undo). A snapshot that *promised* files
  but stored none will **not** wipe a live dir (corruption guard).
- This is a port of prime-agent's "reversible self-improvement" feature,
  folded into Hermes' existing `/refine` machinery — no new system-prompt
  mutation, no prompt-cache or message-role invariant break.
