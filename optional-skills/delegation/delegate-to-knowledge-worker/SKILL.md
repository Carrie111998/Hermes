---
name: delegate-to-knowledge-worker
description: "Delegate research and documentation tasks to a knowledge-worker subagent with correct context. Use when the current session needs information gathered, summarized, or documented."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [delegation, subagent, research, documentation, workflow]
    related_skills: [delegate-to-code-worker, subagent-driven-development]
---

# Delegate to Knowledge Worker

## When to Use

Use this skill when the current session needs to hand off an information-gathering or documentation task to a subagent:

- Researching a library, API, or service to inform a design decision
- Summarizing a codebase section or architecture
- Drafting or updating documentation, changelogs, or runbooks
- Auditing existing code for patterns, issues, or coverage gaps
- Comparing approaches and producing a recommendation

Do NOT use when:
- The task requires writing or modifying code (use `delegate-to-code-worker` instead)
- The answer can be found in one or two tool calls — just do it directly
- The question is ambiguous — clarify first, then delegate

## Invocation

```python
delegate_task(
    goal="<specific research or documentation task — what to find out or produce>",
    context="""
Question to answer: <the concrete question the subagent must answer>
Scope: <which files, directories, URLs, or systems to look at>
Output format: <what to produce — e.g. bullet list, comparison table, draft doc section>
Constraints: <anything to avoid or prefer — e.g. "do not modify any files">
Relevant starting points: <key files, entry points, or URLs to read first>
""",
    skills=["delegate-task-guide"],
)
```

## Context Discipline

The subagent starts with a blank conversation — everything it needs must be in `goal` and `context`.

**Include in context:**
- The concrete question or deliverable (not "explore" — what exactly to produce)
- Scope boundaries so the subagent does not wander into unrelated areas
- The desired output format so the summary is directly usable
- Starting points: key files, relevant URLs, or module names to read first

**Never include in context:**
- Large file contents — tell the subagent to `read_file` from disk instead
- Open-ended exploration mandates without a clear deliverable
- Instructions that contradict the `profile="knowledge-worker"` — this profile is read-only by convention; if writes are needed, use `delegate-to-code-worker`

## Output Expectations

Knowledge-worker subagents should produce their findings as their final response — the summary that comes back to the parent is the deliverable. Structure the goal so the subagent knows exactly what form that summary should take:

- A comparison table of options A, B, C with trade-offs
- A list of all files that import `foo.bar` with their call sites
- A draft changelog entry for these commits
- A risk assessment for migrating from X to Y

Structured output is easier to act on than free-form prose.

## Pitfalls

- **Vague goal:** "Research logging" produces a vague summary. "List all log calls in `agent/` that use level ERROR or above, grouped by file" produces an actionable table.
- **No output format:** Without specifying format, the subagent writes prose when you need a table, or a table when you need bullet points.
- **Scope creep:** Without scope boundaries the subagent reads everything and returns an overwhelming summary. Constrain by directory, module, or question.
- **Expecting file changes:** Knowledge workers read and summarize — they do not write files. If the research should result in a documentation commit, delegate a separate code-worker step after getting the research summary.

## After Delegation

Use the subagent's summary to:
1. Make the decision or answer the question it was researching
2. Feed its output as `context` into a follow-up `delegate-to-code-worker` call if implementation is next
3. Flag any gaps or contradictions the subagent surfaced for follow-up research
