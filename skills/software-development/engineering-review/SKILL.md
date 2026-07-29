---
name: engineering-review
description: "Use when performing a comprehensive, evidence-based engineering audit of the Hermes Agent project across architecture, source, runtime, prompts, profiles, skills, memory, AI-Vault, gateway, scheduler, MCP, Docker, and documentation."
version: 1.1.1
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [engineering-review, audit, architecture, hermes-agent, quality, governance, evidence]
    related_skills: [hermes-agent, hermes-agent-skill-authoring, requesting-code-review, systematic-debugging, codebase-inspection]
---

# Engineering Review

## Overview

Engineering Review is the repeatable audit capability for Hermes Agent itself.
It produces a structured, evidence-based engineering health report that can be
compared across releases, incidents, and major refactors.

This skill is intentionally modular. It is not a long prompt. The skill loads a
small orchestration layer here, then pulls focused components from
`references/`, `templates/`, and `scripts/` as needed.

**Core principle:** No finding without evidence. If evidence is incomplete, say
`Unknown — Manual Review Required` instead of speculating.

## When to Use

Use this skill when the user asks to:

- Review, audit, assess, or score Hermes Agent engineering health
- Prepare a release-quality engineering review report
- Compare engineering health between versions or checkpoints
- Audit Hermes runtime, gateway, scheduler, MCP, skills, memory, profiles, or AI-Vault integration
- Produce a long-term technical debt, risk, and roadmap assessment for Hermes

Do not use this for:

- Reviewing a small PR or local diff only — use `requesting-code-review`
- Debugging one failing test or incident — use `systematic-debugging`
- Creating a new skill — use `hermes-agent-skill-authoring`

## Component Map

| Component | File | Responsibility |
|---|---|---|
| Skill metadata | `SKILL.md` frontmatter | Discovery, versioning, and relationship metadata |
| System role | `references/system-role.md` | Reviewer stance, non-negotiables, and anti-hallucination rules |
| Review contract | `references/review-contract.md` | Finding schema, scoring model, evidence standard, and severity taxonomy |
| Execution workflow | `references/execution-workflow.md` | Ordered review process from scope detection to final report |
| Engineering checklist | `references/engineering-checklist.md` | Category-specific audit checklist for Hermes subsystems |
| Tool permissions | `references/tool-permissions.md` | Safe read-only defaults and approval rules for mutating actions |
| Output template | `templates/engineering-review-report.md` | Stable report structure for release-over-release comparison |
| Evidence collector | `scripts/collect_hermes_review_evidence.py` | Read-only repository evidence snapshot in JSON |

## Default Execution Flow

1. **Load components**
   - Read `references/system-role.md`
   - Read `references/review-contract.md`
   - Read `references/execution-workflow.md`
   - Read `references/engineering-checklist.md`
   - Read `references/tool-permissions.md`
   - Read `templates/engineering-review-report.md`

2. **Identify the Hermes repository**
   - Prefer the current working directory if it is a Hermes Agent checkout.
   - Otherwise inspect likely paths such as `~/.hermes/hermes-agent`.
   - Record the repo path, branch, revision, dirty status, and review timestamp.

3. **Collect evidence**
   - Run the evidence collector when available:
     ```bash
     python3 skills/software-development/engineering-review/scripts/collect_hermes_review_evidence.py --repo .
     ```
   - Keep the default review read-only by consuming stdout. Add
     `--output .hermes/reviews/evidence.json` only when the user asks for a
     saved artifact.
   - If the repo-local script is unavailable, use the installed skill copy shown by `skill_view(name="engineering-review")`.
   - Add targeted source reads, git status, tests, docs checks, and runtime checks as needed.
   - Never read or print secrets. Inspect presence/shape of config files, not secret values.

4. **Audit by category**
   - Architecture
   - Source code
   - Configuration
   - Runtime
   - Prompt system
   - Profiles
   - Skills
   - Memory
   - AI-Vault / knowledge pipeline
   - Gateway
   - Scheduler / cron
   - MCP
   - Docker / deployment
   - Documentation

5. **Score and classify**
   - Apply the scoring model in `references/review-contract.md`.
   - Each finding must include severity, evidence, root cause, impact, and recommendation.
   - Unknowns are valid findings only when clearly marked as manual-review gaps.

6. **Generate the report**
   - Use `templates/engineering-review-report.md`.
   - Include executive summary, health score, category scores, debt assessment, risk matrix, positives, top priorities, immediate actions, medium-term roadmap, and long-term roadmap.

7. **Verify the report**
   - Ensure every claim links to command output, file path + line range, test result, or explicit `Unknown — Manual Review Required`.
   - Ensure no finding is based only on intuition.
   - Ensure recommendations are actionable and scoped.

## Evidence Rules

Use `references/review-contract.md` as the source of truth for evidence,
scoring, severity, and unknown handling. `SKILL.md` intentionally keeps only the
orchestration summary so the review contract does not drift across files.

## Versioning Policy

- Increment **patch** version for wording, checklist clarifications, and template improvements.
- Increment **minor** version when adding a new review category, evidence source, or report section.
- Increment **major** version if the finding schema, scoring scale, or report compatibility changes.
- Keep old report fields stable so historical reviews remain comparable.

## Common Pitfalls

1. **Turning the skill into a monolithic prompt.** Add focused reference or template files instead.
2. **Inventing evidence.** Unknown is better than false certainty.
3. **Reading secrets.** Review configuration structure and redaction posture, not credential values.
4. **Scoring without category evidence.** A score with no evidence is speculation.
5. **Mixing improvement work into the audit.** The review may recommend changes, but should not refactor unless the user separately authorizes implementation.
6. **Ignoring dirty worktrees.** A dirty repo changes audit repeatability; report it clearly.

## Verification Checklist

- [ ] All modular components were loaded or explicitly marked unavailable
- [ ] Evidence snapshot was collected or the collection blocker was recorded
- [ ] Repo path, branch/revision, dirty status, and timestamp are in the report
- [ ] Every finding has severity, evidence, root cause, impact, and recommendation
- [ ] Insufficient evidence is marked `Unknown — Manual Review Required`
- [ ] Category scores and health score explain their evidence basis
- [ ] Roadmaps are separated into immediate, medium-term, and long-term actions
- [ ] The final report is suitable for comparing against future releases
