---
name: plan
description: "Plan mode: write an actionable markdown plan to .hermes/plans/, no execution. Bite-sized tasks, exact paths, complete code."
version: 2.0.0
author: Hermes Agent (writing-craft adapted from obra/superpowers)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [planning, plan-mode, implementation, workflow, design, documentation]
    related_skills: [subagent-driven-development, test-driven-development, requesting-code-review]
---

# Plan Mode

Use this skill when the user wants a plan instead of execution.

## Core behavior

For this turn, you are planning only.

- Do not implement code.
- Do not edit project files except the plan markdown file.
- Do not run mutating terminal commands, commit, push, or perform external actions.
- You may inspect the repo or other context with read-only commands/tools when needed.
- Your deliverable is a markdown plan saved inside the active workspace under `.hermes/plans/`.

## Reference Map

| To do this | Read |
|---|---|
| Copy the required plan header and the per-task format (objective / files / TDD steps / commit) | `references/plan-document-template.md` |
| Follow the 6-step authoring process, including codebase exploration calls and the pre-delivery review checklist | `references/writing-process.md` |
| See DRY / YAGNI / TDD / commit principles worked out, and the four common plan mistakes | `references/principles-and-common-mistakes.md` |

## Output requirements

Write a markdown plan that is concrete and actionable.

Include, when relevant:
- Goal
- Current context / assumptions
- Proposed approach
- Step-by-step plan
- Files likely to change
- Tests / validation
- Risks, tradeoffs, and open questions

If the task is code-related, include exact file paths, likely test targets, and verification steps.

## Save location

Save the plan with `write_file` under:
- `.hermes/plans/YYYY-MM-DD_HHMMSS-<slug>.md`

Treat that as relative to the active working directory / backend workspace. Hermes file tools are backend-aware, so using this relative path keeps the plan with the workspace on local, docker, ssh, modal, and daytona backends.

If the runtime provides a specific target path, use that exact path.
If not, create a sensible timestamped filename yourself under `.hermes/plans/`.

## Interaction style

- If the request is clear enough, write the plan directly.
- If no explicit instruction accompanies `/plan`, infer the task from the current conversation context.
- If it is genuinely underspecified, ask a brief clarifying question instead of guessing.
- After saving the plan, reply briefly with what you planned and the saved path.

---

# Writing the Plan Well

The rest of this skill is the craft of authoring a *good* implementation plan — the content that goes inside the markdown file above.

## Overview

Write comprehensive implementation plans assuming the implementer has zero context for the codebase and questionable taste. Document everything they need: which files to touch, complete code, testing commands, docs to check, how to verify. Give them bite-sized tasks. DRY. YAGNI. TDD. Frequent commits.

Assume the implementer is a skilled developer but knows almost nothing about the toolset or problem domain. Assume they don't know good test design very well.

**Core principle:** A good plan makes implementation obvious. If someone has to guess, the plan is incomplete.

## When a Full Implementation Plan Helps

**Always use before:**
- Implementing multi-step features
- Breaking down complex requirements
- Delegating to subagents via subagent-driven-development

**Don't skip when:**
- Feature seems simple (assumptions cause bugs)
- You plan to implement it yourself (future you needs guidance)
- Working alone (documentation matters)

## End-to-End Skeleton

```
1. Understand the requirements; ask if genuinely underspecified.
2. Explore the codebase read-only; find the closest existing pattern.
3. Decide architecture, file layout, test strategy.
4. Write tasks in order: setup → core (TDD each) → edge cases → integration → cleanup.
5. Fill in exact paths, complete copy-pasteable code, exact commands + expected output.
6. Run the review checklist, then save to .hermes/plans/YYYY-MM-DD_HHMMSS-<slug>.md.
7. Report the path and offer the subagent-driven-development handoff.
```

## Bite-Sized Task Granularity

**Each task = 2-5 minutes of focused work.**

Every step is one action:
- "Write the failing test" — step
- "Run it to make sure it fails" — step
- "Implement the minimal code to make the test pass" — step
- "Run the tests and make sure they pass" — step
- "Commit" — step

Sizing examples (too big vs right size): `references/plan-document-template.md`.

## Non-Negotiables for the Plan Content

- **Exact file paths.** Not "the config file" — `src/config/settings.py`.
- **Complete, copy-pasteable code.** Not "add validation" — the actual function body.
- **Exact commands with expected output.** Not "test it works" — the command and the pass count.
- **Every code-producing task carries the full TDD cycle** (failing test → verify fail → minimal
  code → verify pass). See `test-driven-development`.
- **A commit at the end of every task.**
- **DRY and YAGNI applied.** No speculative flexibility for requirements nobody asked for.
- **No task larger than 2-5 minutes.** If it doesn't fit, split it.

Review the plan against `references/writing-process.md` Step 6 before you deliver it.

## Execution Handoff

After saving the plan, offer the execution approach:

**"Plan complete and saved. Ready to execute using subagent-driven-development — I'll dispatch a fresh subagent per task with two-stage review (spec compliance then code quality). Shall I proceed?"**

When executing, use the `subagent-driven-development` skill:
- Fresh `delegate_task` per task with full context
- Spec compliance review after each task
- Code quality review after spec passes
- Proceed only when both reviews approve

## Remember

```
Bite-sized tasks (2-5 min each)
Exact file paths
Complete code (copy-pasteable)
Exact commands with expected output
Verification steps
DRY, YAGNI, TDD
Frequent commits
```

**A good plan makes implementation obvious.**
