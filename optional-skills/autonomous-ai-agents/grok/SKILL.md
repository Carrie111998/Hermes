---
name: grok
description: "Delegate coding to xAI Grok Build CLI (features, PRs). Headless automation: require stopReason EndTurn (exit 0 is not success), set SSL_CERT_FILE to avoid macOS hangs, prefer SuperGrok OAuth over XAI_API_KEY billing, use streaming-json with process watch_patterns for mid-run supervision."
version: 0.2.0
author: Matt Maximo (MattMaximo), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Coding-Agent, Grok, xAI, Code-Review, Refactoring, Automation, Headless, stopReason, SSL]
    related_skills: [codex, claude-code, hermes-agent]
---

# Grok Build CLI — Hermes Orchestration Guide

Delegate coding tasks to [Grok Build](https://docs.x.ai/build/overview) (xAI's
autonomous coding agent CLI, the `grok` command) via the Hermes terminal. Grok
can read files, write code, run shell commands, spawn subagents, and manage git
workflows. It runs three ways: an interactive TUI, **headless** (`-p`), and as
an **ACP agent** over JSON-RPC.

This is the third sibling to `codex` and `claude-code`. The orchestration
pattern is nearly identical — **prefer headless `-p` for one-shots**, use a PTY
for interactive sessions.

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

> **API-key fallback (not the default):** Grok also supports setting the
> `XAI_API_KEY` environment variable (`xai-…`) for pay-as-you-go billing via
> `api.x.ai`. Only use this if `grok login` / SuperGrok auth is unavailable.
> The subscription path (`grok login`) is the intended setup for most users.
>
> **Billing surface check (verified on Grok CLI):** run `grok models`.
> - `You are logged in with grok.com.` → SuperGrok / subscription OAuth path.
> - `You are using XAI_API_KEY.` → API-key / metered path.
>
> When **both** `~/.grok/auth.json` (OAuth) and `XAI_API_KEY` are present, the
> CLI reports the API-key path. A leftover `XAI_API_KEY` in the Hermes process
> environment can silently move spend off the subscription allotment onto
> metered API billing. For subscription automation: **unset `XAI_API_KEY`**
> (and do not inject OAuth access tokens into `XAI_API_KEY` — those are JWTs,
> not `xai-…` API keys).

## Two Orchestration Modes

### Mode 1: Headless (`-p`) — Non-Interactive (PREFERRED)

Runs a one-shot task, prints the result, and exits. No PTY, no interactive
dialogs to navigate. This is the cleanest integration path — the analog of
`claude -p` and `codex exec`.

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
- `streaming-json` — newline-delimited JSON events as they arrive (best with
  background + `process` / `watch_patterns`)

```
# Structured result for parsing
terminal(command="grok --no-auto-update -p 'List all TODO comments in src/' --output-format json", workdir="/project", timeout=120)

# Auto-approve for autonomous building
terminal(command="grok --no-auto-update --always-approve -p 'Refactor the database layer and run the tests'", workdir="/project", timeout=300)
```

### Success criteria — exit 0 is not enough

Headless Grok can exit **0** on failed or cancelled runs. Process status alone
must not be treated as task success.

When automating with `--output-format json` (preferred for Hermes):

1. Require non-empty stdout.
2. Parse the JSON object (or the last JSON line if the stream mixed noise).
3. Accept **only** when `stopReason == "EndTurn"`.
4. Treat `stopReason` of `Cancelled`, missing fields, empty stdout, or invalid
   JSON as **failure** — re-exit non-zero in any wrapper, and do not mark the
   Kanban/task done.
5. Independently inspect `git diff` / run the packet's verify commands. A
   narrative "done" in the model text is not evidence.

Example acceptance snippet (run after the CLI returns):

```bash
# stdout saved to result.json; fail closed unless EndTurn
python3 - <<'PY'
import json, sys
raw = open("result.json").read().strip()
if not raw:
    sys.exit("empty grok stdout")
try:
    data = json.loads(raw)
except json.JSONDecodeError:
    data = None
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    if data is None:
        sys.exit("invalid grok JSON")
if data.get("stopReason") != "EndTurn":
    sys.exit(f"grok stopReason={data.get('stopReason')!r} (want EndTurn)")
print("ok")
PY
```

Without JSON mode, you cannot reliably distinguish Cancelled-from-permissions
from a completed edit run — prefer `--output-format json` whenever Hermes owns
the result.

### macOS headless hang — set SSL_CERT_FILE

On macOS, bare headless `grok` can hang indefinitely after model-cache load
with empty stdout. The process may sit in TLS/certificate initialization
(Security framework / keychain) with no useful error. Hermes already hardens
similar cases elsewhere in the gateway/agent SSL paths; for Grok automation
set a static CA bundle **before** launch:

```bash
export SSL_CERT_FILE=/etc/ssl/cert.pem
export CURL_CA_BUNDLE="$SSL_CERT_FILE"
export REQUESTS_CA_BUNDLE="$SSL_CERT_FILE"
```

If `/etc/ssl/cert.pem` is missing, a Python `certifi` bundle works as a
fallback. Symptom of the bug: long wall time, empty stdout, eventual parent
timeout — not an auth error message. Fix env and relaunch; do not keep waiting.

### Preflight (cheap, before a long coding run)

Run these in order; fail closed on the first miss:

1. **Binary:** `command -v grok` (or your pinned path) is executable.
2. **TLS (macOS automation):** `SSL_CERT_FILE` points at a readable CA file.
3. **Auth path:** `test -f ~/.grok/auth.json` for subscription OAuth, **or**
   `XAI_API_KEY` starts with `xai-` for intentional API-key billing — not both
   unless you mean to force the API path.
4. **Billing surface:** `grok models` prints the path you intend
   (`grok.com` vs `XAI_API_KEY`).
5. **Cheap call:**
   `grok --no-auto-update -p 'Reply with exactly: PONG' --output-format plain`
   with a short timeout. Expect `PONG` quickly; hang ⇒ TLS/env; auth error ⇒
   login/key; success ⇒ proceed to the real packet.

### Background Mode (Long Tasks)

Use Hermes `terminal` + `process` — do not invent a parallel job registry.

```
# Start headless in background (JSON for honest completion checks)
terminal(
  command="grok --no-auto-update --always-approve --output-format json -p 'Refactor the auth module'",
  workdir="/project",
  background=true,
  notify_on_complete=true
)
# Returns session_id

# Monitor
process(action="poll", session_id="<id>")
process(action="log", session_id="<id>")

# Kill if needed
process(action="kill", session_id="<id>")
```

For mid-run supervision, pair **`streaming-json`** with `watch_patterns` so
matches land on real event boundaries instead of scraped prose:

```
terminal(
  command="grok --no-auto-update --always-approve --output-format streaming-json -p 'Implement the ticket'",
  workdir="/project",
  background=true,
  watch_patterns=["EndTurn", "stopReason"]
)
```

Notes:

- Prefer rare, high-signal patterns. `watch_patterns` is rate-limited (about one
  notification per 15s) and auto-promotes to completion-only after repeated
  noisy windows — do not watch generic words like `error` on a long build.
- On completion, still parse the final JSON / last event for
  `stopReason == "EndTurn"`; a process exit of 0 is insufficient.
- For an interactive (TUI) background session, use `pty=true` + tmux and monitor
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
| `grok models` | List models **and** show which auth/billing path is active |
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
3. **Don't conflate SuperGrok OAuth with `XAI_API_KEY` billing.** OAuth lives
   in `~/.grok/auth.json` and uses the subscription/plan allotment. `XAI_API_KEY`
   (`xai-…`) is metered API billing. Check with `grok models` (see Prerequisites).
   When both are present, the CLI selects the API-key path — unset `XAI_API_KEY`
   to stay on subscription. Never copy an OAuth access token into
   `XAI_API_KEY`.
4. **Exit code 0 is not success.** Cancelled / empty / non-`EndTurn` JSON runs
   can still exit 0. Require `stopReason == "EndTurn"` from `--output-format json`
   (see Success criteria).
5. **macOS headless hang without `SSL_CERT_FILE`.** Empty stdout + multi-minute
   stall after cache load → set `SSL_CERT_FILE=/etc/ssl/cert.pem` (see above).
6. **Always pass `--no-auto-update` in automation** — otherwise Grok phones home
   for update checks (and `x.ai`/`storage.googleapis.com` may be unreachable).
7. **Prefer npm install over the curl installer** — `npm install -g
   @xai-official/grok` avoids the Cloudflare-walled `x.ai` host.
8. **`--always-approve` is the autonomous-build switch.** Without it, headless
   runs may stall waiting on tool-approval prompts (often surfacing as
   `stopReason: Cancelled` with exit 0). Omit it deliberately for read-only
   review/audit work so Grok can't mutate files.
9. **Headless `-p` skips TUI dialogs**; the TUI needs `pty=true` (+ tmux for
   monitoring), just like Claude Code.
10. **Use `--no-alt-screen`** if you run the TUI inline and the fullscreen
    alt-screen takeover garbles captured output.
11. **No git repo needed**, but for PR/commit workflows you still want one — use
    `mktemp -d && git init` for scratch commit tasks.
12. **Clean up tmux sessions** with `tmux kill-session -t <name>` when done.
13. **Imported project context can dominate cost.** Grok auto-reads `CLAUDE.md`,
    `.claude/`, and related instruction trees. For bounded automation, prefer a
    clean worktree / explicit `--cwd` and disable unneeded integrations when the
    CLI supports it — then verify with `grok inspect`.

## Rules for Hermes Agents

1. **Prefer headless `-p`** for single tasks — cleanest integration, structured
   output via `--output-format json`.
2. **Always set `workdir`** (or `--cwd`) so Grok targets the right project.
3. **Pass `--no-auto-update`** in every automated invocation.
4. **On macOS automation, export `SSL_CERT_FILE=/etc/ssl/cert.pem`** (and the
   usual CA bundle aliases) before launch.
5. **Use `--always-approve` only when Grok should write autonomously**; omit it
   for read-only reviews and audits.
6. **Treat completion as `stopReason == "EndTurn"` + independent diff/tests**,
   never process exit code alone.
7. **Background long tasks** with `background=true`, `notify_on_complete=true`
   (and `streaming-json` + sparse `watch_patterns` when mid-run signals help);
   monitor via the `process` tool — do not rebuild a job control plane.
8. **Use tmux for multi-turn interactive work** and monitor with
   `tmux capture-pane -t <session> -p -S -50`.
9. **Verify auth and billing surface before relying on them** — preflight
   (binary, cert, `grok models`, cheap PONG); don't assume Hermes' xAI auth
   carries over; unset `XAI_API_KEY` when you intend SuperGrok OAuth.
10. **Report results to the user** — summarize what Grok changed, the
    stopReason, and what's left.
