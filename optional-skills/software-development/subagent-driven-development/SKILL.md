---
name: subagent-driven-development
description: "Execute plans via delegate_task subagents (2-stage review)."
version: 1.1.0
author: Hermes Agent (adapted from obra/superpowers)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [delegation, subagent, implementation, workflow, parallel]
    related_skills: [plan, requesting-code-review, test-driven-development]
---

# Subagent-Driven Development

## Overview

Execute implementation plans by dispatching fresh subagents per task with systematic two-stage review.

**Core principle:** Fresh subagent per task + two-stage review (spec then quality) = high quality, fast iteration.

## When to Use

Use this skill when:
- You have an implementation plan (from the `plan` skill or user requirements)
- Tasks are mostly independent
- Quality and spec compliance are important
- You want automated review between tasks

**vs. manual execution:**
- Fresh context per task (no confusion from accumulated state)
- Automated review process catches issues early
- Consistent quality checks across all tasks
- Subagents can ask questions before starting work

## End-to-End Skeleton

```
read_file(plan)  →  todo([all tasks])            # read the plan ONCE
for each task:
    delegate_task(implementer)                    # full task text in context, TDD
    delegate_task(spec reviewer)   → PASS?        # else fix + re-review
    delegate_task(quality reviewer) → APPROVED?   # else fix + re-review
    todo(task -> completed, merge=True)
delegate_task(final integration reviewer)
pytest tests/ -q  &&  git diff --stat  &&  commit
```

Exact `delegate_task` goals, contexts, and checklists for every box above: `references/orchestration-playbook.md`.

## Routing — load the reference you need

| Intent | Read |
|---|---|
| Run the loop: full implementer / spec-reviewer / quality-reviewer dispatch contexts, final review, verify+commit commands | `references/orchestration-playbook.md` |
| Size the tasks (2-5 min each), too-big vs right-size examples | `references/orchestration-playbook.md` |
| Combine with `plan`, `test-driven-development`, `requesting-code-review`, `systematic-debugging` | `references/orchestration-playbook.md` |
| See a worked 2-task run end to end, including a failed spec review | `references/orchestration-playbook.md` |
| Design or name a validation checkpoint; decide what happens when a check fails and who resumes | `references/gates-taxonomy.md` |
| Subagent asked a question / reviewer found issues / subagent failed the task | `references/gates-taxonomy.md` |
| Run will consume significant context — read-depth rules, four-tier degradation model, silent-degradation warning signs, why fresh-subagent-per-task pays off | `references/context-budget-discipline.md` |

## Red Flags — Never Do These

- Start implementation without a plan
- Skip reviews (spec compliance OR code quality)
- Proceed with unfixed critical/important issues
- Dispatch multiple implementation subagents for tasks that touch the same files
- Make subagent read the plan file (provide full text in context instead)
- Skip scene-setting context (subagent needs to understand where the task fits)
- Ignore subagent questions (answer before letting them proceed)
- Accept "close enough" on spec compliance
- Skip review loops (reviewer found issues → implementer fixes → review again)
- Let implementer self-review replace actual review (both are needed)
- **Start code quality review before spec compliance is PASS** (wrong order)
- Move to next task while either review has open issues

## Remember

```
Fresh subagent per task
Two-stage review every time
Spec compliance FIRST
Code quality SECOND
Never skip reviews
Catch issues early
```

**Quality is not an accident. It's the result of systematic process.**

`references/gates-taxonomy.md` and `references/context-budget-discipline.md` are adapted from gsd-build/get-shit-done (MIT © 2025 Lex Christopherson).
