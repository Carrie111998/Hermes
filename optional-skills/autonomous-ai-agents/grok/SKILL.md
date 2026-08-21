---
name: grok
description: "Delegate coding to xAI Grok Build CLI (features, PRs)."
version: 0.3.0
author: Matt Maximo (MattMaximo), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Coding-Agent, Grok, xAI, Code-Review, Refactoring, Automation]
    related_skills: [codex, claude-code, hermes-agent]
---

# Grok Build CLI — Hermes Orchestration Guide

Delegate coding tasks to [Grok Build](https://docs.x.ai/build/overview) (xAI's
autonomous coding agent CLI, the `grok` command) via the Hermes terminal. Grok
can read files, write code, run shell commands, spawn subagents, and manage git
workflows. It runs three ways: an interactive TUI, **headless** (`-p`), and as
an **ACP agent** over JSON-RPC.

This is the third sibling to `codex` and `claude-code`. For direct one-shot
terminal use, headless `-p` is convenient; use a PTY for interactive sessions.
The deterministic Hermes Kanban adapter defaults to stateful ACP and keeps
headless transport only as an explicitly opted-in compatibility path.

## When to use

- Building features
- Refactoring
- PR reviews
- Batch issue fixing
- Any task where you'd otherwise reach for Codex / Claude Code but want Grok

## Prerequisites

- **Install (preferred):** `npm install -g @xai-official/grok`
  - The official installer `curl -fsSL https://x.ai/cli/install.sh | bash` also
    works, but the `x.ai` host is Cloudflare-walled in some environments. The
    npm path avoids that dependency entirely.
- **Auth — SuperGrok / X Premium+ subscription (primary path):**
  - Run `grok login` once → opens a browser for OAuth → token cached in
    `~/.grok/auth.json`. This uses your **SuperGrok or X Premium+** subscription
    (no per-token API billing).
  - Check sign-in state by looking for `~/.grok/auth.json`, or run a cheap
    headless smoke test: `grok --no-auto-update -p "Say ok."`
  - In the TUI, `/logout` signs out and `/login` (or relaunching) signs back in.
- **No git repo required** — unlike Codex, Grok runs fine outside a git
  directory (good for scratch/throwaway tasks).
- **Claude Code / AGENTS.md compatible with zero config** — Grok auto-reads
  `CLAUDE.md`, `.claude/` (skills, agents, MCPs, hooks, rules), and the
  `AGENTS.md` family. Existing project context just works.

> **API-key fallback (not the default for this user):** Grok also supports
> setting the `XAI_API_KEY` environment variable for pay-as-you-go billing
> via `api.x.ai`. Only use
> this if `grok login` / SuperGrok auth is unavailable. The subscription path
> (`grok login`) is the intended setup here.

## Two Orchestration Modes

### Mode 1: Headless (`-p`) — Direct Non-Interactive Use

Runs a one-shot task, prints the result, and exits. No PTY, no interactive
dialogs to navigate. This is the direct-invocation analog of `claude -p` and
`codex exec`; the deterministic Kanban adapter uses ACP by default.

```
terminal(command="grok --no-auto-update -p 'Add a dark mode toggle to settings'", workdir="/path/to/project", timeout=180)
```

Always pass `--no-auto-update` in automation to skip background update checks.

**When to use headless:**
- One-shot coding tasks (fix a bug, add a feature, refactor)
- CI/CD automation and scripting
- Structured output parsing with `--output-format json`
- Any task that doesn't need multi-turn conversation

### Mode 2: Interactive PTY — Multi-Turn TUI Sessions

The TUI is a fullscreen, mouse-interactive app. Drive it with `pty=true`. For
robust monitoring/input use tmux (same pattern as the `claude-code` skill).

```
# Launch in a tmux session for capture-pane monitoring
terminal(command="tmux new-session -d -s grok-work -x 140 -y 40")
terminal(command="tmux send-keys -t grok-work 'cd /path/to/project && grok' Enter")

# Wait for startup, then send a task
terminal(command="sleep 5 && tmux send-keys -t grok-work 'Refactor the auth module to use JWT' Enter")

# Monitor progress
terminal(command="sleep 15 && tmux capture-pane -t grok-work -p -S -50")

# Exit when done
terminal(command="tmux send-keys -t grok-work '/quit' Enter && sleep 1 && tmux kill-session -t grok-work")
```

**Tip for headless-but-inline output:** if you want TUI-style output without the
fullscreen alt-screen takeover (e.g. for cleaner logs), add `--no-alt-screen`.
For pure automation, headless `-p` is still cleaner than the TUI.

## Headless Deep Dive

### Common Flags

| Flag | Effect |
|------|--------|
| `-p, --single <PROMPT>` | Send one prompt, run headless, exit |
| `-m, --model <MODEL>` | Choose a model |
| `-s, --session-id <UUID>` | Assign a **NEW** valid UUID to a fresh conversation (must not already exist). Does **not** resume — use `--resume`/`--continue` for that. Only valid with `--resume`/`--continue` when paired with `--fork-session` |
| `-r, --resume [<UUID>]` | Resume an existing session by its UUID (or the most recent if omitted) |
| `-c, --continue` | Continue the most recent session in the current directory |
| `--fork-session` | When resuming, create a new session ID instead of reusing the original |
| `--max-turns <N>` | Cap the maximum number of agent turns |
| `--cwd <PATH>` | Set the working directory |
| `--output-format <FMT>` | `plain` (default), `json`, or `streaming-json` |
| `--always-approve` | Auto-approve all tool executions (the `--full-auto` / `--yolo` equivalent) |
| `--no-alt-screen` | Run inline, no fullscreen TUI takeover |
| `--no-auto-update` | Skip background update checks (use in all automation; hidden from `--help` but still works) |

### Output Formats

- `plain` — human-readable text (default)
- `json` — one JSON object at the end of the run (parse the result cleanly)
- `streaming-json` — newline-delimited JSON events as they arrive

```
# Structured result for parsing
terminal(command="grok --no-auto-update -p 'List all TODO comments in src/' --output-format json", workdir="/project", timeout=120)

# Auto-approve for autonomous building
terminal(command="grok --no-auto-update --always-approve -p 'Refactor the database layer and run the tests'", workdir="/project", timeout=300)
```

### Background Mode (Long Tasks)

```
# Start headless in background
terminal(command="grok --no-auto-update --always-approve -p 'Refactor the auth module'", workdir="/project", background=true, notify_on_complete=true)
# Returns session_id

# Monitor
process(action="poll", session_id="<id>")
process(action="log", session_id="<id>")

# Kill if needed
process(action="kill", session_id="<id>")
```

For an interactive (TUI) background session, use `pty=true` + tmux and monitor
with `tmux capture-pane`, exactly like the `claude-code` / `codex` skills.

### Session Continuation

Sessions are keyed by **UUID**, not by name. `--session-id` assigns a *new* UUID
to a fresh run (it does **not** resume); `--resume` takes an existing session's
UUID (or omit the value to resume the most recent).

```
# Start a session with a self-assigned UUID (must be a valid, unused UUID)
SID=$(uuidgen)
terminal(command="grok --no-auto-update -s $SID -p 'Start refactoring the database layer' --always-approve", workdir="/project", timeout=240)

# Resume that exact session later by its UUID
terminal(command="grok --no-auto-update -r $SID -p 'Now add connection pooling' --always-approve", workdir="/project", timeout=180)

# Or just continue the most recent session in this directory (no UUID needed)
terminal(command="grok --no-auto-update -c -p 'What did you change last time?'", workdir="/project", timeout=60)
```

## Read-Only Audit → Markdown Note Pattern

To have Grok review local artifacts and return a clean markdown note (for
Obsidian or a repo) without mutating anything:

1. Prepare stable input files first with Hermes tools (`read_file`,
   `write_file`). Snapshot only the relevant context into a temp file rather
   than dumping raw paths.
2. Run Grok headless **without** `--always-approve` so it cannot auto-write, and
   demand `markdown only, no preamble`.
3. Save Grok's stdout straight into the destination note with `write_file()`.

```
grok --no-auto-update -p "Read /tmp/current.md and /tmp/inventory.md. Produce markdown only, no preamble. Output a clean note titled 'Cleanup Review'." --output-format plain
```

**Pitfall (same as Claude Code):** for document rewrites, a loose "rewrite this"
prompt may return a change summary instead of the full file. Instead: pipe the
file in, and demand `Return ONLY the full revised markdown document. No intro,
no explanation, no code fences. Start immediately with '# Title'.` Verify the
first lines with `read_file()` before overwriting the destination.

## PR Review Patterns

### Quick Review (Headless)

```
terminal(command="cd /path/to/repo && git diff main...feature-branch | grok --no-auto-update -p 'Review this diff for bugs, security issues, and style problems. Be thorough.'", timeout=120)
```

### Clone-to-temp Review (safe, no repo mutation)

```
terminal(command="REVIEW=$(mktemp -d) && git clone https://github.com/user/repo.git $REVIEW && cd $REVIEW && gh pr checkout 42 && grok --no-auto-update -p 'Review the changes vs origin/main. Check bugs, security, race conditions, missing tests.'", pty=true, timeout=300)
```

### Post the review

```
terminal(command="gh pr comment 42 --body '<review text>'", workdir="/path/to/repo")
```

## Parallel Issue Fixing with Worktrees

```
# Create worktrees
terminal(command="git worktree add -b fix/issue-78 /tmp/issue-78 main", workdir="~/project")
terminal(command="git worktree add -b fix/issue-99 /tmp/issue-99 main", workdir="~/project")

# Launch Grok headless in each (background)
terminal(command="grok --no-auto-update --always-approve -p 'Fix issue #78: <description>. Commit when done.'", workdir="/tmp/issue-78", background=true, notify_on_complete=true)
terminal(command="grok --no-auto-update --always-approve -p 'Fix issue #99: <description>. Commit when done.'", workdir="/tmp/issue-99", background=true, notify_on_complete=true)

# Monitor
process(action="list")

# After completion: push and open PRs
terminal(command="cd /tmp/issue-78 && git push -u origin fix/issue-78")
terminal(command="gh pr create --repo user/repo --head fix/issue-78 --title 'fix: ...' --body '...'")

# Cleanup
terminal(command="git worktree remove /tmp/issue-78", workdir="~/project")
```

## Useful Subcommands & TUI Commands

| Command | Purpose |
|---------|---------|
| `grok` | Start the interactive TUI |
| `grok -p "query"` | Headless one-shot |
| `grok login` / `grok logout` | Sign in / out (SuperGrok / X Premium+ OAuth) |
| `grok inspect` | Show what Grok discovered in cwd: config sources, instructions, skills, plugins, hooks, MCP servers |
| `grok agent stdio` | Run as an ACP agent over JSON-RPC (for IDE/tool integration) |
| `grok update` | Update the CLI (needs the `x.ai` host; skip in automation) |

TUI slash commands (interactive only): `/model <name>`, `/always-approve`,
`/plan`, `/context`, `/compact`, `/resume`, `/sessions`, `/fork`, `/usage`,
`/quit`. `Shift+Tab` cycles session modes (including Plan mode, which blocks
write tools except the session plan file).

## Config (`~/.grok/config.toml`)

```toml
[cli]
auto_update = false          # skip background update checks persistently

[ui]
permission_mode = "ask"      # or "always-approve" to skip tool prompts by default

[models]
default = "grok-build-0.1"
```

Put global preferences in `~/.grok/config.toml` (not project-scoped
`.grok/config.toml`). `permission_mode` supersedes the legacy `approval_mode` /
`yolo = true` keys.

## Pitfalls & Gotchas

1. **Auth is subscription-gated.** `grok login` requires a SuperGrok or X
   Premium+ subscription. If login fails or there's no `~/.grok/auth.json`,
   confirm the subscription is active before falling back to `XAI_API_KEY`.
2. **Don't conflate Hermes' xAI auth with the `grok` CLI's auth.** Hermes'
   `x_search` runs on its own xAI OAuth; the standalone `grok` CLI has a
   separate token in `~/.grok/auth.json`. A working `x_search` does NOT mean
   `grok` is logged in.
3. **Always pass `--no-auto-update` in automation** — otherwise Grok phones home
   for update checks (and `x.ai`/`storage.googleapis.com` may be unreachable).
4. **Prefer npm install over the curl installer** — `npm install -g
   @xai-official/grok` avoids the Cloudflare-walled `x.ai` host.
5. **`--always-approve` is the autonomous-build switch.** Without it, headless
   runs may stall waiting on tool-approval prompts. Omit it deliberately for
   read-only review/audit work so Grok can't mutate files.
6. **Headless `-p` skips TUI dialogs**; the TUI needs `pty=true` (+ tmux for
   monitoring), just like Claude Code.
7. **Use `--no-alt-screen`** if you run the TUI inline and the fullscreen
   alt-screen takeover garbles captured output.
8. **No git repo needed**, but for PR/commit workflows you still want one — use
   `mktemp -d && git init` for scratch commit tasks.
9. **Clean up tmux sessions** with `tmux kill-session -t <name>` when done.

## Rules for Hermes Agents

1. **Use ACP for the deterministic Kanban adapter.** For direct, unsupervised
   one-shot terminal tasks, headless `-p` provides structured output via
   `--output-format json`; it is not the adapter default.
2. **Always set `workdir`** (or `--cwd`) so Grok targets the right project.
3. **Pass `--no-auto-update`** in every automated invocation.
4. **Use `--always-approve` only when Grok should write autonomously**; omit it
   for read-only reviews and audits.
5. **Background long tasks** with `background=true, notify_on_complete=true` and
   monitor via the `process` tool.
6. **Use tmux for multi-turn interactive work** and monitor with
   `tmux capture-pane -t <session> -p -S -50`.
7. **Verify auth before relying on it** — check `~/.grok/auth.json` or run a
   cheap `grok -p "Say ok."` smoke test; don't assume Hermes' xAI auth carries
   over.
8. **Report results to the user** — summarize what Grok changed and what's left.

## Deterministic Hermes Kanban Worker (Opt-In)

For a Kanban card that must reach a terminal state even when Grok returns prose
instead of calling a terminal tool, use the bundled adapter. The adapter, not
Grok, owns `claim`, `request-review`, `complete`, and `block`:

```bash
python optional-skills/autonomous-ai-agents/grok/scripts/hermes_kanban_worker.py \
  <task-id> --board <board-slug> --workspace /path/to/isolated/worktree \
  --acceptance-command '{"label":"tests","argv":["scripts/run_tests.sh"],"timeout":900}' \
  --acceptance-command '{"label":"ruff","argv":["ruff","check","."],"timeout":120}'
```

Assign the card to `worker-grok-cli` before invoking the adapter. It verifies
the assignee before claiming so the Kanban run profile cannot accidentally be
recorded as `foreman-long` or another launcher profile.

The adapter starts one fresh `grok agent stdio` process and one ACP session per
card. Substantive work, terminal reporting, and at most one report correction
all run as prompts in that same process and session. The report prompt passes
the strict worker schema through Grok's ACP `_meta.outputSchema` extension and
reads the validated object from `PromptResponse._meta.structuredOutput`; the
ACP path never launches a separate headless resume process. Install Hermes with
its `acp` extra (currently pinned to `agent-client-protocol==0.9.0`) before
using this transport. The legacy one-shot route remains available explicitly
as `--transport headless`; it is a compatibility fallback, not the default.

The ACP session receives the full Kanban card plus a Hermes project context
pack generated by the same coding-context detector used for Sol and Luna.
The adapter passes the pinned SDK's 50 MiB bounded stdio reader limit to
`spawn_agent_process`; without that explicit transport option the lower-level
helper falls back to asyncio's 64 KiB line limit and valid large JSON-RPC
updates fail with `LimitOverrunError`. Frames beyond the bounded limit become a
structured capability failure rather than a generic PID crash.
Implementation runs are cancelled if no workspace content changes within
`--no-progress-timeout` (300 seconds by default), so a broad exploration loop
cannot consume the entire card timeout without editing. ACP stop reasons other
than `end_turn` are nonterminal failures and cannot be promoted into completed
reports or enter the report phase. Work, report, and correction timeouts send
`session/cancel` before the bounded process cleanup ladder runs.

Acceptance is an adapter-owned, fail-closed contract. Every repeated
`--acceptance-command` value must be a JSON object containing a short `label`,
an `argv` array, and a bounded `timeout`; shell operators are rejected. The
adapter runs all required commands independently in an isolated Git copy,
requires zero exits, unchanged HEAD, and no verification-copy/source mutation,
and never substitutes generic project-context suggestions for this contract.
When the first post-work ACP acceptance probe has residual command failures,
the adapter sends exactly one bounded continuation through the same process and
session, then probes once more before reporting or blocking.

Optional failover is also structured. Repeat `--provider-route` with JSON such
as `{"name":"backup","endpoint_env":"GROK_BACKUP_URL","key_env":"GROK_BACKUP_KEY"}`.
Descriptors name environment variables only; credential values are never CLI
arguments or metadata. An alternate route is attempted only for a sanitized
retryable provider failure and only while the workspace still matches its
pre-attempt snapshot. Partial work suppresses replay and is preserved for a
precise block.

A valid completed implementation report requests first-class review from
`worker-luna` by default; a worker-declared block deterministically blocks the
card. The handoff proves ownership with the current Hermes run id and carries
the same adapter-owned verification and execution evidence in review metadata.
The review dispatcher then force-loads `sdlc-review`: Luna either approves with
`kanban_complete`, returns actionable rework with `kanban_request_changes`, or
blocks only for a genuine external dependency. A changes request routes the
same card back to `worker-grok-cli` without touching block-recurrence counts;
the external Grok lane must invoke the adapter again for that ready card.

Pass `--reviewer <profile>` to select another reviewer. Passing an explicitly
empty value preserves the older direct-`complete` behavior, but that
unsupervised compatibility path is not recommended for a formal worker.
The adapter selects a Hermes profile name; it does not override that profile's
model/provider or fallback policy. Operators who require Luna-only review must
audit `worker-luna` credentials and remove or replace any configured fallback
provider instead of assuming the profile name proves the model used.
`working` is not a legal terminal schema value. A blocked report requires a
non-empty reason, concrete evidence, and an individual test/check record;
verification commands joined by `&&` or `||` are rejected because their result
attribution is ambiguous. The model-facing schema omits `capability`:
completed reports use a null `block_kind`, while blocked reports may use only
`dependency`, `needs_input`,
or `transient`. Every report and correction prompt also states the coupled
rules explicitly: completed requires `block_reason=""` and `block_kind=null`;
blocked requires a non-empty reason and one of the three non-capability kinds.
Completed reports may list only checks that actually ran, with `passed` or
`failed` outcomes; optional or out-of-scope checks that were not run are
omitted, while an unavailable required check becomes a `dependency` block and
must never be reported as a fake pass.
Grok CLI 1.0.4 did not return `structuredOutput` when these coupled rules were
encoded as a top-level conditional `anyOf`, so the prompt plus the Python
validator remain authoritative instead of deploying that incompatible schema.
Provider, authentication, ACP SDK, and local executable failures are classified
by the adapter itself. Timeouts, non-zero CLI exits, or two malformed reports
produce a structured transient block instead of a crashed PID or silent retry.
The second malformed report records only safe status/kind, test outcome counts,
and block-reason length/SHA-256 evidence; test commands/details and
model-authored blocker text are not retained. Grok and Hermes subprocesses
receive `HERMES_PROFILE=worker-grok-cli`; that fixes command and comment
authorship, while the pre-claim assignee check fixes the recorded Kanban run
profile.

The adapter also injects a scoped worker rule through Grok's `--rules` option.
It explicitly authorizes edits and tests in the claimed workspace while
delegating GitHub Issue/PR lifecycle, branch ownership, commits, pushes, and
Kanban terminal actions to the Foreman. This prevents repository instructions
that describe a full human/agent PR lifecycle from turning an authorized
implementation worker into a read-only reviewer. The rule does **not** disable
repository scope, security, quality, or testing requirements. Prefer a real
branch worktree when the repository requires one, but leave the worker's diff
uncommitted for the Foreman to inspect and integrate.

Security-review cards use `--task-mode review`. Their single ACP session is
created with `GROK_SANDBOX=read-only`, and work/report/correction remain inside
that process and session. Grok CLI 1.0.4 pins sandbox policy at session
creation. Grok's Linux write-deny sandbox also refuses a symlinked
`GROK_HOME`; the adapter resolves that path in the child environment without
changing the user's symlink, auth state, or config. The adapter snapshots file
contents before and after every phase,
rejects any review mutation, requires a pinned-SHA verdict with evidence, and
never synthesizes PASS when the coordinator or provider fails. It never
commits, pushes, opens a PR, or merges.

The adapter passes `RIGHTCODE_GROK_API_KEY` only to child processes. If that
name is absent but `RIGHTCODE_API_KEY` is already present, it maps the value in
memory for the child without changing shell profiles, `.env` files, or Grok
configuration. It never prints the key.

Grok remains an external pull lane and is deliberately **not** registered as a
Hermes profile or silently launched through `_default_spawn`. Invoke it from a
bounded external supervisor (or manually) only in an isolated workspace where
`--always-approve` is appropriate. Luna review itself is a native Hermes lane:
with the default `kanban.review_dispatch: true`, the Gateway dispatcher claims
the review card and adds `sdlc-review` automatically. If `--workspace` is
passed, the adapter requires it to resolve to the workspace returned by
`claim`; it never lets Grok write in a different directory. The structured
report and adapter verification are review inputs, not proof that Grok's claims
are true.
