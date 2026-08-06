# System log

Append-only daily log of agent and automation activity in this repo.

## File naming

- One file per UTC day: `YYYY-MM-DD.md`
- Create the file on first entry; never rewrite prior days

## Entry format

```markdown
## YYYY-MM-DDTHH:MM:SSZ — Short title (agent/tool)

- **Agent/tool:** Cursor | Claude Code | GitHub Actions | manual
- **Repos:** repo-a, repo-b
- **Done:** bullet summary of work completed
- **Commits/PRs:** abc123, #42 (optional)
- **Follow-up:** open items (optional)
```

## Rules

1. **Append only** — do not edit or delete prior entries except to redact secrets
2. **No secrets** — redact tokens, API keys, passwords, and credential file paths
3. **UTC timestamps** — use ISO-8601 with `Z` suffix
4. **One session, one entry** — merge related work from the same session into a single entry

## When to log

Log after any non-trivial session: feature work, bug fixes, CI changes, refactors, rollouts, or multi-file edits.

Skip for typo-only or comment-only changes.

## Canonical spec

Account-wide standards live in [dizhaky/.github `docs/system-log/README.md`](https://github.com/dizhaky/.github/blob/main/docs/system-log/README.md) and Obsidian `Projects/Tech/Agent Documentation/STANDARDS`.
