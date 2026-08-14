---
name: hermes-agent-skill-authoring
description: "Create or revise SKILL.md files."
version: 2.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [skills, authoring, hermes-agent, conventions, skill-md, context-engineering]
    related_skills: [plan, requesting-code-review]
---

# Authoring Hermes-Agent Skills

## Overview

A skill is a **folder** (not just a markdown file) that builds context for the model. It can include scripts, references, assets, templates, and data the agent discovers and uses. Writing a skill is fundamentally different from writing software or human documentation — you are engineering the model's context, not its runtime.

Two locations:

1. **User-local:** `~/.hermes/skills/<category>/<name>/SKILL.md` — personal. Created via `skill_manage(action='create')`.
2. **In-repo:** `<repo>/skills/<category>/<name>/SKILL.md` — committed, shipped with the package. Use `write_file` + `git add`. `skill_manage(action='create')` does NOT target this tree.

## When to Use

- Creating a new skill (user-local or in-repo)
- Revising an existing skill's description, body, or supporting files
- Reviewing a skill PR from another engineer

**Don't use for:** tasks the model already does well (a skill that restates default behavior adds context cost without value); rapidly-changing references that will drift (use MCP or live fetch instead).

## The Decision Test: Do You Need a Skill?

Before writing anything, apply this test to every proposed skill and every sentence within it:

> **"Would the agent get this wrong — or behave inconsistently — without this instruction?"**

If the answer is no, the skill (or sentence) **cannot afford to exist**. Every skill is a tax: its description costs ~100 tokens in **every session, for every user, always**. Its body costs ~5,000 tokens every time it loads. Skills with fluff degrade not just themselves but every other skill through routing interference.

**You need a skill when:**
- The agent gets it wrong without special context (internal APIs, gotchas, non-obvious conventions)
- You need deterministic consistency across runs (deployment steps, code review standards)
- Your knowledge is durable but not in training data (enterprise workflows, team processes, taste/judgment)

**You don't need a skill when:**
- The model already knows how to do it (e.g., standard git workflows)
- The instructions duplicate what's already in the system prompt or global context
- The reference changes faster than you can maintain it (use live fetch instead)

**Warning:** Research shows that LLM-self-generated skills provide no benefit on average — "models cannot reliably author the procedural knowledge they benefit from consuming." A skill born from real failure cases and human expertise is valuable; a skill an AI wrote by summarizing what it already knows is noise.

## Skill Types (Pick One)

Skills that try to do too much straddle categories and confuse the routing. Know which type you're building:

| Type | What it does | Example |
|---|---|---|
| **Library/API reference** | How to correctly use a library/CLI/SDK, with gotchas | Internal billing library edge cases |
| **Product verification** | How to test/verify code works | Signup flow driver with state assertions |
| **Data fetching** | Connect to data/monitoring stacks | Grafana datasource UIDs, Datadog field refs |
| **Process automation** | Multi-step workflow → one command | Standup post, weekly recap |
| **Code scaffolding** | Generate framework boilerplate | New service with auth/logging pre-wired |
| **Code quality/review** | Enforce standards, review code | Adversarial review, code style enforcement |
| **CI/CD & deployment** | Build, deploy, monitor | PR babysitter, gradual rollout with auto-rollback |
| **Runbooks** | Symptom → investigation → report | Service debugging, log correlator |
| **Infrastructure ops** | Maintenance with guardrails | Orphan cleanup with soak period |

## The Description: Routing Trigger, Not Documentation

The description is the **single most important line** in the skill. It is not a summary of what the skill does — it is **instructions to the model about when to load the skill**.

### How to write it

1. **Start with "Use when"** — front-load the trigger within the first 57 chars (that's all the system prompt index shows).
2. **Write in user language** — use the words a frustrated user would type: "babysit PR," "watch CI," "cherry-pick prod fix." Not "monitors pull request status and retries failed checks."
3. **Be dense and terse** — ~100 tokens per skill is the index budget, paid every session.
4. **Include trigger keywords** — if users say "deploy," include "deploy" in the description.
5. **Avoid off-target triggers** — a description that matches too broadly steals routing from other skills. Every new skill risks making every existing skill slightly worse.

### Good vs Bad

- ✅ `Use when debugging Hermes skill discovery failures. Diagnose frontmatter, indexing, loading.`
- ✅ `Use when writing Java tests. Enforces coverage via knowledge graph, generates JUnit5 + Mockito.`
- ❌ `This skill contains detailed guidance for agents working on skill discovery failures.` (documentation tone, trigger buried)
- ❌ `Helps with various development tasks.` (too broad, matches everything)

### Off-target side effects

Adding a skill with a description that overlaps an existing skill's domain causes **routing confusion** — the model loads the wrong skill or loads both, wasting context. Before merging, check: does the new description compete with any existing skill for the same queries?

## Writing the Body

### Skip the obvious

The model already knows how to code and can read your codebase. A skill that restates what the model would do by default adds context without adding value. Focus on information that **pushes the model out of its normal way of thinking**.

### Don't railroad

Write intent, not step-by-step commands. The model handles edge cases better with flexible guidance than with rigid command sequences.

- ✅ `Cherry-pick the commit onto a clean branch. Resolve conflicts preserving intent. If it can't land cleanly, explain why.`
- ❌ `git log # find the commit; git checkout main; git checkout -b <clean-branch>; git cherry-pick <commit>;`

Overly prescriptive instructions are fragile — when something goes wrong (and it will), the model follows the script instead of adapting.

### Build the gotchas section

**Gotchas are the highest-signal content in any skill.** They are the special cases, footguns, and failure modes that the model cannot learn from training data. Examples:

- "The `subscriptions` table is append-only. The row you want is the one with the highest version, not the most recent `created_at`."
- "This field is called `@request_id` in the API gateway and `trace_id` in the billing service. They're the same value."
- "Staging returns 200 even when the webhook didn't process. Check `payment_events` for the real state."

Add a gotcha every time the agent trips up during testing or production. The gotchas section should **grow organically over time** — this is the maintenance flywheel.

### Write completion criteria into steps

Each ordered step should say how the agent knows it's done. "Every modified file accounted for" beats "summarize changes."

## Progressive Disclosure: The 3-Tier Cost Model

A skill is a folder, and the file system is a form of context engineering. Think in three tiers:

| Tier | What loads | Token budget | When you pay |
|---|---|---|---|
| **Index** | `name: description` for every skill | ~100 tokens/skill | Every session, every user, **always** |
| **Load** | Full SKILL.md body | ~5,000 tokens | When the skill is invoked |
| **Runtime** | `references/`, `scripts/`, `assets/`, `templates/` | Unbounded | Only when the agent reads them |

**Rules:**
- **Index tier:** Every word matters. Description must be dense, terse, trigger-focused.
- **Load tier:** Every sentence must change behavior. If it doesn't, it's wasted context that persists until compaction. Multiple skills loading simultaneously multiplies this cost.
- **Runtime tier:** Lowest bar for inclusion. Put unbounded conditional logic, heavy reference docs, and templates here. The agent only pays when it needs them.

### Folder structure

```
<skill-name>/
├── SKILL.md              # Hub: frontmatter + always-needed instructions + gotchas
├── references/           # Heavy docs loaded conditionally ("Read api-errors.md if API returns non-200")
├── scripts/              # Deterministic code the agent would otherwise reinvent every run
├── assets/               # Output templates, schemas the agent copies and fills
├── templates/            # Input templates for scaffolding
└── config.json           # First-run user setup (ask once, store, reuse)
```

For intricate skills with 100+ topics, use **multilevel hierarchy**: group into subject areas (20 areas × 15 topics is easier to route than 300 flat topics). Add quick-reference guides to help the model navigate.

## Frontmatter Requirements

Source of truth: `tools/skill_manager_tool.py::_validate_frontmatter`. Hard requirements:

- Starts with `---` at byte 0 (no leading blank line or BOM).
- Closes with `\n---\n` before the body.
- Parses as a YAML mapping.
- `name` field present (lowercase, hyphens, ≤64 chars).
- `description` field present, ≤ **1024 chars**. First 57 chars are shown in the system prompt skill index; longer text visible via `skills_list()`/`skill_view()`.
- Non-empty body after the closing `---`.

Peer-matched frontmatter:

```yaml
---
name: my-skill-name
description: Use when <trigger>. <one-line behavior>.
version: 2.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [short, descriptive, tags]
    related_skills: [other-skill, another-skill]
---
```

`version`/`author`/`license`/`metadata` are not validator-enforced, but every peer has them.

## Size Limits

- Description: ≤ 1024 chars (enforced).
- Full SKILL.md: ≤ 100,000 chars (~36k tokens). Peer skills sit at **8-14k chars**. Push past 20k → split into `references/*.md`.

## Store Scripts, Don't Reconstruct

Give the agent code to compose, not reconstruct. One of the most powerful tools in a skill is pre-written scripts and helper functions. This lets the model spend its turns on deciding **what to do next** rather than rebuilding boilerplate.

For example, a data-fetching skill might include helper functions that the agent composes into analysis scripts on the fly.

## Skill Memory

Some skills benefit from storing data between runs:
- Append-only log files (e.g., `standups.log` for a standup skill — next run reads history and reports deltas)
- JSON state files
- SQLite for complex state

This gives the skill a form of memory without polluting the model's context.

## Workflow

### Step 0: Write evals first

Before writing the body, define what success looks like:
- **Positive examples:** real queries where the skill should load and help
- **Negative examples:** queries where it should NOT load (prevents off-target routing)
- **Neighbor confusion:** queries near the domain boundary that might route to the wrong skill

At minimum, verify the skill loads when needed and doesn't load when not needed.

### Step 1: Write the description

The hardest line. Get the routing trigger right before writing any body content. Test: does this description correctly route to this skill and not to a neighbor?

### Step 2: Write the body

Skip the obvious. Focus on gotchas, non-obvious conventions, and completion criteria. Don't railroad with rigid command sequences.

### Step 3: Use the hierarchy

Break conditional or heavy content into `references/`, `scripts/`, `assets/`. The SKILL.md is the hub; everything else is a spoke loaded on demand.

### Step 4: Iterate

Run evals. Add gotchas as you discover failure cases. Small word changes in descriptions can have outsized routing impact — test after every description change.

### Step 5: Ship + commit

For in-repo skills: `git add` + `git commit`. For user-local: `skill_manage(action='create')` handles it.

**Note:** The current session's skill loader is cached — new skills won't appear until a fresh session.

## Measuring Skills

At scale, you need data to answer: which skills earn their context tax? Which under-trigger? Which degrade neighbors? This covers usage telemetry (Anthropic's PreToolUse hook pattern), three tiers of eval suites (loading precision/recall, progressive loading, end-to-end task completion), and cross-model testing.

**→ See `references/skill-measurement.md`** for eval suite templates, lifecycle management, and a minimum viable eval quick-start.

## Maintenance: The Gotchas Flywheel

After shipping, the skill enters maintenance mode:

- **Gotchas are append-mostly.** The gotchas section accrues the most value over time. Add a line every time the agent fails in testing or production.
- **Don't change the description without re-running evals.** Description changes affect routing, including spillover effects on other skills. If you're changing the routing trigger, you need eval evidence supporting the change.
- **Skills should get shorter or sharper over time.** When adding a rule, remove the old wording it replaces. Don't layer advice forever.
- **Prune sediment.** Stale lines remain because adding felt safer than deleting. Apply the decision test: if the sentence doesn't change behavior, delete it.

## Quality Checklist (Per Sentence)

Apply to every sentence in the skill:

1. **"Would the agent get this wrong without this?"** — If no, delete it.
2. **Does it change behavior?** — If it's generic advice ("be careful," "use best practices"), delete or replace with a checkable criterion.
3. **Is it in the right tier?** — Always-needed → SKILL.md body. Conditional → `references/`. Deterministic logic → `scripts/`.
4. **Does it railroad?** — Replace rigid command sequences with intent-based instructions.
5. **Is it duplicated?** — Keep each meaning in one source of truth. If it exists elsewhere, reference, don't copy.

## Common Pitfalls

1. **Using `skill_manage(action='create')` for an in-repo skill.** It writes to `~/.hermes/skills/`, not the repo tree. Use `write_file` for in-repo creation.

2. **Leading whitespace before `---`.** The validator checks `content.startswith("---")`; any leading blank line or BOM fails validation.

3. **Description written as documentation, not routing trigger.** "This skill does X" is wrong. "Use when <trigger>" is right. Include the words users actually say.

4. **Description too broad, causing routing interference.** A new skill's description that overlaps existing skills degrades their routing. Check for off-target matches before merging.

5. **Writing no-op prose.** "Be careful," "be thorough," "use best practices" rarely change model behavior. Replace with a checkable completion criterion or delete.

6. **Railroading with rigid command sequences.** Write intent ("cherry-pick onto clean branch"), not scripts (`git checkout main; git checkout -b ...`). Prescriptive sequences are fragile when things go wrong.

7. **Letting skills accumulate sediment.** A skill should get shorter or sharper over time. When adding a rule, remove the old wording it replaces.

8. **Changing the description post-ship without evals.** Description changes affect routing with spillover effects on other skills. Re-run evals first.

9. **Forgetting the author/license/metadata block.** Not validator-enforced, but every peer has it.

10. **Expecting the current session to see the new skill.** The skill loader is cached at session start. Verify in a fresh session.

11. **Self-generating skills without human input.** LLM-generated skills that merely summarize what the model already knows provide no benefit. Real value comes from failure cases, gotchas, and human expertise not in training data.

## Verification Checklist

- [ ] Decision test passed: "Would the agent get this wrong without this skill?" → Yes
- [ ] Description starts with "Use when" and front-loads trigger within 57 chars
- [ ] Description uses user language (what users say when they need this)
- [ ] Description checked for off-target routing overlap with existing skills
- [ ] Frontmatter: `---` at byte 0, `\n---\n` close, name ≤64 chars lowercase+hyphens
- [ ] Body: no no-op prose, no railroaded command sequences
- [ ] Gotchas section present (even if initially empty — it will grow)
- [ ] Heavy/conditional content in `references/`, deterministic logic in `scripts/`
- [ ] Each ordered step has a checkable completion criterion
- [ ] Total file ≤ 100,000 chars (aim for 8-15k)
- [ ] `related_skills` references resolve (in-repo for in-repo skills)
- [ ] Evals written: positive (loads when needed) + negative (doesn't load when not)
- [ ] For in-repo: `git add` + `git commit` completed