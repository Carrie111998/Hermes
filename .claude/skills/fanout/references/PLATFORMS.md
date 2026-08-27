# Platform adapters — how to spawn parallel workers per tool

Read the section for the tool you're running in. Everything else in
`SKILL.md` is tool-neutral; only the spawn syntax and model names differ.

All schemas below were verified against official docs on **2026-08-14**. Where a
detail was not stated in official docs, it says so — do not fill gaps by
inference.

---

## Capability matrix

| Tool | Parallel subagents | Per-agent model | Spawn mechanism |
|---|---|---|---|
| Grok Build | Yes | Yes | `spawn_subagent` in one turn, or `/workflow graph-run` |
| Claude Code | Yes | Yes | `Agent` tool, N calls in one message |
| Cursor | Yes | Yes | Task tool, N calls in one message |
| Antigravity | Yes | Yes (tier) | `invoke_subagent` |
| Codex CLI | Reads `.codex/agents/` | — | Not verified — see Degradation |
| Copilot / Devin Desktop | Not verified | — | See Degradation |

---

## Grok Build

Skill lives in `~/.grok/skills/graph-engineering/` (and the portable
`fanout` copy under `~/.agents/skills/fanout/`). Subagents: `fanout-worker`,
`fanout-skeptic` (loaded via Claude-compat agents).

**Preferred:** `/workflow graph-run` — coordination is a Rhai script, so
passing results does not re-spend parent context. Source:
`~/Dev/dotfiles/grok/workflows/graph-run.rhai`. User copy:
`~/.grok/workflows/graph-run.rhai`.

Interactive diamond:

```
spawn_subagent({
  description: "<unit name>",
  subagent_type: "fanout-worker",
  prompt: "<full standalone contract for this unit>"
})
```

All `spawn_subagent` calls in **one message** run concurrently. After they
return: completeness gate (findings, not narration) + expected vs actual
count, then one `fanout-skeptic`, then synthesize. Writers take
`isolation: "worktree"`.

See `graph-engineering/references/platforms.md` for args shape.

---

## Claude Code

Skill lives in `~/.claude/skills/fanout/` or `.claude/skills/fanout/`.
Subagent definitions in `.claude/agents/*.md`.

```
Agent({
  description: "<unit name>",
  subagent_type: "claude",
  model: "sonnet",
  prompt: "<full standalone context for this unit>"
})
```

All `Agent` calls in **one message** run concurrently. Tiers: `sonnet` for
workers, `opus` for skeptic and synthesis.

---

## Cursor

Docs: <https://cursor.com/docs/subagents>, <https://cursor.com/docs/skills>

**Skill locations** — Cursor loads skills from `.agents/skills/`,
`.cursor/skills/`, `~/.agents/skills/`, `~/.cursor/skills/`, and for
compatibility also `.claude/skills/` and `.codex/skills/`. A skill is a folder
containing `SKILL.md`.

`SKILL.md` frontmatter: `name` and `description` required; optional `paths`
(globs scoping the skill), `disable-model-invocation` (true = only via
`/fanout`), `metadata`.

**Parallelism.** The docs state it directly:

> "Agent sends multiple Task tool calls in a single message, so subagents run
> simultaneously."

Trigger it in natural language — "research X, Y and Z in parallel" — or let the
agent delegate on its own. Invoke the skill explicitly with `/fanout`.

**Subagent definitions** live in `.cursor/agents/*.md` (also reads
`.claude/agents/` and `.codex/agents/`; `.cursor/` wins on name conflict).
Fields — all optional:

| Field | Default | Notes |
|---|---|---|
| `name` | filename | lowercase + hyphens |
| `description` | — | shown in Task tool hints; drives delegation |
| `model` | `inherit` | `inherit` or a model ID |
| `readonly` | `false` | no file edits, no state-changing shell commands |
| `is_background` | `false` | returns immediately, doesn't block parent |

`model` accepts bracketed parameters: `claude-opus-5[effort=high]`,
`composer-2.5[fast=false]`, `claude-opus-5[effort=high,context=300k]`.

Use `readonly: true` on worker and skeptic agents — it enforces at the tool
layer what the unattended-session gate only asks for in prose.

> **Rules ≠ skills.** Cursor rules are `.cursor/rules/*.mdc` with
> `description` / `globs` / `alwaysApply`. A plain `.md` in that directory is
> **ignored**. Don't put this skill there.

---

## Google Antigravity

Docs: <https://antigravity.google/docs/subagents>,
<https://antigravity.google/docs/skills>,
<https://antigravity.google/docs/rules-workflows>

**Skill locations** — `<workspace-root>/.agents/skills/<folder>/SKILL.md`
(workspace) or `~/.gemini/config/skills/<folder>/SKILL.md` (global).
`.agent/skills` (singular) still works for backward compatibility. Frontmatter:
`name` + `description`.

**Parallelism.** The parent calls the `invoke_subagent` tool. Verbatim from the
docs:

> "A parent agent can invoke multiple subagents concurrently."

Subagents start with a clean context — they do not inherit the parent's
conversation history. Workspace options: `inherit`, `branch` (isolated git
worktree), or `share`. Monitor via the subagent panel or `Alt+J` in the CLI.

**Subagent definitions** live in `.agents/agents/<name>.md` (or
`.agents/agents/<name>/agent.md`); globally `~/.gemini/config/agents/<name>.md`.

| Field | Type | Default | Notes |
|---|---|---|---|
| `name` | string | required | unique identifier |
| `description` | string | required | planner uses this to decide delegation |
| `tools` | string[] | `[]` | e.g. `view_file`, `grep_search`, `run_command` |
| `mainAgent` | boolean | `true` | selectable as primary agent in chat |
| `subagent` | boolean | `true` | invocable via `invoke_subagent` |
| `model` | string | `inherit` | `inherit`, `flash`, or `pro` |
| `commandExecutionPolicy` | string | `sandbox` | `off`, `auto`, `eager`, `sandbox` |
| `mcpServers` | object[] | `[]` | per-subagent MCP servers |
| `skills` | string[] | `[]` | e.g. `skills/fanout` |

Tier mapping: `flash` for workers, `pro` for skeptic and synthesis.

> **Known issue (per the docs):** a misspelled or unmapped name in `tools` can
> make the subagent **hang** during execution. Double-check exact tool names.
> Omitting `tools` entirely is safer than guessing.

**Rules are not this.** Antigravity rules live in `.agents/rules/` (workspace)
and `~/.gemini/GEMINI.md` (global), capped at **12,000 characters**. A rule is
plain Markdown — activation (Manual / Always On / Model Decision / Glob) is set
in the Customizations UI, **not** in frontmatter. Do not author `trigger:` or
`globs:` keys in a rule file; they are undocumented.

Workflows are separate again: Markdown, invoked as `/workflow-name`, same
12,000-character cap, and they can call other workflows.

---

## Degradation — tools without parallel subagents

If your tool has **no** parallel-subagent primitive, do not run the phases
serially and still call it a fan-out; the wall-clock benefit is the reason the
pattern exists. Pick one:

1. **Human-parallel.** Open N agent sessions/tabs yourself, paste one unit
   prompt into each, then paste the combined output back into one session for
   Phases 3–4. Preserves the real benefit; costs manual coordination.
2. **Serial with separation preserved.** Run units one at a time, but keep the
   skeptic as its own pass over the combined output. Loses the wall-clock win,
   keeps the quality win — which is the half that catches errors.
3. **Don't.** If serial cost exceeds the value, say so and do the task directly.

Say explicitly which mode you used. A serial run reported as a fan-out
misrepresents how independently the findings were produced.

---

## Verification notes

- Cursor and Antigravity both describe Agent Skills as an **open standard**
  (agentskills.io). `SKILL.md` is portable across all three tools unchanged.
- Cursor reading `.claude/skills/` and `.claude/agents/` is documented
  compatibility, not a coincidence — one authored tree can serve three tools.
- **Not verified:** whether Codex CLI, GitHub Copilot, or Devin Desktop
  (formerly Windsurf) expose a parallel-subagent primitive. Absence of evidence,
  not evidence of absence — check before assuming. Treat them as Degradation
  cases until confirmed.
- **Not verified:** AGENTS.md vs GEMINI.md precedence in Antigravity when both
  exist. Sources conflict; no official statement found. Don't build logic that
  depends on one silently overriding the other.
