---
name: delegate-to-code-worker
description: "Delegate coding tasks to a code-worker subagent with correct context. Use when the current session needs to implement code changes, fix bugs, write tests, or open PRs."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [delegation, subagent, coding, implementation, workflow]
    related_skills: [subagent-driven-development, delegate-to-knowledge-worker]
---

# Delegate to Code Worker

## When to Use

Use this skill when the current session needs to hand off a concrete coding task to a subagent:

- Implementing a feature from a ticket or spec
- Fixing a bug with a known root cause
- Writing or updating tests
- Opening a pull request with reviewed changes
- Running a build/lint/test cycle in isolation

Do NOT use when:
- The task is ambiguous — resolve ambiguity first, then delegate
- The work is purely research or documentation (use `delegate-to-knowledge-worker` instead)
- The task takes fewer than 2 tool calls — just do it directly

## Invocation

```python
delegate_task(
    goal="<specific coding task from the ticket — what to implement and where>",
    context="""
Project: <absolute repo path, e.g. /Users/you/repos/myproject>
Ticket: <ticket key and one-line description>
Acceptance criteria:
  - <criterion 1>
  - <criterion 2>
Test command: <exact command to verify the work, e.g. python -m pytest tests/ -q>
Relevant files: <list any key files the subagent should read first>
""",
    skills=["delegate-task-guide", "read-write-safety"],
)
```

## Context Discipline

The subagent starts with a blank conversation — everything it needs must be in `goal` and `context`.

**Include in context:**
- Absolute path to the repository root (never assume `/workspace/...`)
- Ticket key + description so the subagent understands what success looks like
- Acceptance criteria as a checklist
- The exact test command so the subagent can self-verify
- Any files it must read before touching anything (especially if they are non-obvious)

**Never include in context:**
- Large file contents — tell the subagent to `read_file` from disk instead
- Entire conversation history or reasoning traces
- Instructions the subagent can discover from the ticket or the codebase itself

## Pitfalls

- **Ambiguous goal:** Delegating "fix the bug" without specifying which bug and what the expected behavior is forces the subagent to guess. Resolve ambiguity before delegating.
- **Missing test command:** Without a test command the subagent cannot verify its own work and will either over-report success or loop indefinitely.
- **Missing repo path:** Subagents must not assume a container-style path like `/workspace/...`. Always provide an absolute path from the actual filesystem.
- **Inlining large files:** Pasting hundreds of lines of source code into `context` wastes context budget and goes stale. Reference by path instead.
- **Re-delegating the whole goal:** If the orchestrator's job is to decompose work, delegate the subtasks — not a single subagent to "do everything". That adds indirection with no value.

## After Delegation

Review the subagent's summary for:
1. All acceptance criteria addressed (not just "done")
2. Test command output confirming passing state
3. Any open issues the subagent flagged for follow-up
