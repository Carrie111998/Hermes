---
name: hermes-agent
description: "Configure, extend, or contribute to Hermes Agent."
version: 2.3.0
author: Hermes Agent + Teknium
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [hermes, setup, configuration, multi-agent, spawning, cli, gateway, development]
    homepage: https://github.com/NousResearch/hermes-agent
    related_skills: [claude-code, codex, opencode]
---

# Hermes Agent

Hermes Agent is an open-source AI agent framework by Nous Research that runs in your terminal, a native desktop app, messaging platforms, and IDEs. It's in the same category as Claude Code (Anthropic), Codex (OpenAI), and OpenClaw — autonomous coding and task-execution agents that use tool calling to interact with your system. Hermes works with any LLM provider (OpenRouter, Anthropic, OpenAI, Google, DeepSeek, xAI, local models, and 20+ others) and runs on Linux, macOS, Windows, and WSL.

What makes Hermes different:

- **Self-improving through skills** — Hermes learns from experience by saving reusable procedures as skills. When it solves a complex problem, discovers a workflow, or gets corrected, it can persist that knowledge as a skill document that loads into future sessions. Skills accumulate over time, making the agent better at your specific tasks and environment.
- **Persistent memory across sessions** — remembers who you are, your preferences, environment details, and lessons learned. Pluggable memory backends (built-in, Honcho, Mem0, and more) let you choose how memory works.
- **Multi-platform gateway** — the same agent runs on Telegram, Discord, Slack, WhatsApp, iMessage, Signal, Matrix, Teams, Email, and a dozen more platforms with full tool access, not just chat.
- **Many surfaces** — the same agent core drives the CLI, the Ink TUI, a native Electron desktop app, a web dashboard, and an ACP server for IDEs (VS Code / Zed / JetBrains).
- **Provider-agnostic** — swap models and providers mid-workflow without changing anything else. Credential pools rotate across multiple API keys automatically.
- **Profiles** — run multiple independent Hermes instances with isolated configs, sessions, skills, and memory.
- **Extensible** — plugins, MCP servers, custom tools, webhook triggers, cron scheduling, and the full Python ecosystem.

People use Hermes for software development, research, system administration, data analysis, content creation, home automation, and anything else that benefits from an AI agent with persistent context and full system access.

**This skill helps you work with Hermes Agent effectively** — setting it up, configuring features, spawning additional agent instances, troubleshooting issues, finding the right commands and settings, and understanding how the system works when you need to extend or contribute to it.

**Docs:** https://hermes-agent.nousresearch.com/docs/

## Scope & Verification

This skill is a concise operating guide, not the complete source of truth for every Hermes feature. If a Hermes feature, command, or setting is not mentioned here, do not treat that absence as evidence that it does not exist. Check the live repository and official docs before giving a negative answer.

Good verification targets:

- CLI commands: `hermes --help`, `hermes <command> --help`, and `hermes_cli/main.py`
- User documentation: https://hermes-agent.nousresearch.com/docs/
- Source tree: https://github.com/NousResearch/hermes-agent

## Quick Start

```bash
# Install (shell installer — sets up uv, Python, the venv, and the launcher)
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash

# Or via PyPI (ships the TUI bundle + shell launcher)
pip install hermes-agent       # or: uv pip install hermes-agent

# Interactive chat (default surface; set display.interface: tui to launch the Ink TUI instead)
hermes

# Single query
hermes chat -q "What is the capital of France?"

# Setup wizard  /  pick model+provider  /  health check
hermes setup
hermes model
hermes doctor

# Other surfaces
hermes desktop                 # launch the native desktop app (alias: hermes gui)
hermes dashboard               # web admin panel + embedded chat
hermes proxy                   # OpenAI-compatible local proxy backed by your OAuth provider
```

---

## Reference Map — read the file before answering from memory

| To do this | Read |
|-----------|------|
| Look up a `hermes` CLI command or global flag | [references/cli-reference.md](references/cli-reference.md) |
| Look up an in-session `/slash` command | [references/slash-commands.md](references/slash-commands.md) |
| Find a config key, a path under `~/.hermes`, a provider env var, or a toolset name | [references/config-reference.md](references/config-reference.md) |
| Decide between `.hermes.md` / `AGENTS.md` / `CLAUDE.md`, or debug context injection | [references/project-context-files.md](references/project-context-files.md) |
| Change secret/PII redaction or command-approval behavior | [references/security-privacy.md](references/security-privacy.md) |
| Set up STT/TTS or voice mode | [references/voice.md](references/voice.md) |
| Run a second Hermes process (one-shot, tmux/PTY, multi-agent, resume) | [references/spawning-instances.md](references/spawning-instances.md) |
| Use delegation, cron, curator, or kanban | [references/durable-systems.md](references/durable-systems.md) |
| Install or configure MCP servers | [references/native-mcp.md](references/native-mcp.md) |
| Set up webhook triggers | [references/webhooks.md](references/webhooks.md) |
| Learn about the desktop app, dashboard, proxy, or other surfaces | [references/surfaces.md](references/surfaces.md) |
| Debug something on Windows | [references/windows-quirks.md](references/windows-quirks.md) |
| Fix a broken feature (voice, tools, gateway, provider, skills not showing) | [references/troubleshooting.md](references/troubleshooting.md) |
| Contribute code: add a tool/command, run tests, follow commit conventions | [references/contributing.md](references/contributing.md) |

Load a reference with `skill_view(name="hermes-agent", file_path="references/<file>.md")`.

---

## Hard Constraints

These hold regardless of which reference you are following:

- **Never break prompt caching.** Do not change context, tools, or the system prompt mid-conversation. Tool/toolset changes only take effect on `/reset` (a new session) — that is deliberate, not a bug.
- **Message roles must alternate.** Never two assistant or two user messages in a row.
- **`security.redact_secrets` cannot be flipped mid-session.** It is snapshotted at import time. An agent cannot disable its own redaction — tell the user to change it in config from a terminal and start a new session.
- **YOLO / `approvals.mode: off` does not disable secret redaction.** They are independent.
- **Never hardcode `~/.hermes`.** Use `get_hermes_home()` from `hermes_constants` so profiles and `$HERMES_HOME` keep working.
- **Config values go in `config.yaml`, secrets go in `.env`.** Never write credentials into config.
- **Curator never deletes.** Its most destructive action is archive, and it only touches skills with `created_by: "agent"` provenance.
- **A backgrounded `delegate_task` child is not durable** — it dies with the parent process. For work that must outlive the process use `cronjob` or `terminal(background=True, notify_on_complete=True)`.
- **Absence from this skill is not evidence of absence.** Verify against `hermes --help`, the docs, and the source tree before telling a user a feature does not exist.

---

## What Takes Effect When

The single most common source of confusion — "I changed it and nothing happened":

| Changed | Takes effect on |
|---------|-----------------|
| Tools / toolsets / skills enablement | `/reset` (new session) |
| `config.yaml` values | New CLI invocation; `/restart` in gateway |
| `.env` variables | `/reload` in session, or new invocation |
| MCP servers | `/reload-mcp` |
| Newly added skill files | `/reload-skills` |
| `security.redact_secrets` | New process only (snapshotted at import) |
| Source code changes | Restart the CLI or gateway process |

---

## Where to Find Things

| Looking for... | Location |
|----------------|----------|
| Config options | `hermes config edit` or [Configuration docs](https://hermes-agent.nousresearch.com/docs/user-guide/configuration) |
| Available tools | `hermes tools list` or [Tools reference](https://hermes-agent.nousresearch.com/docs/reference/tools-reference) |
| Slash commands | `/help` in session or [Slash commands reference](https://hermes-agent.nousresearch.com/docs/reference/slash-commands) |
| Skills catalog | `hermes skills browse` or [Skills catalog](https://hermes-agent.nousresearch.com/docs/reference/skills-catalog) |
| Provider setup | `hermes model` or [Providers guide](https://hermes-agent.nousresearch.com/docs/integrations/providers) |
| Platform setup | `hermes gateway setup` or [Messaging docs](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/) |
| MCP servers | `hermes mcp list` or [MCP guide](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp) |
| Profiles | `hermes profile list` or [Profiles docs](https://hermes-agent.nousresearch.com/docs/user-guide/profiles) |
| Cron jobs | `hermes cron list` or [Cron docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron) |
| Memory | `hermes memory status` or [Memory docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory) |
| Env variables | `hermes config env-path` or [Env vars reference](https://hermes-agent.nousresearch.com/docs/reference/environment-variables) |
| CLI commands | `hermes --help` or [CLI reference](https://hermes-agent.nousresearch.com/docs/reference/cli-commands) |
| Gateway logs | `~/.hermes/logs/gateway.log` |
| Session files | `hermes sessions browse` (reads state.db) |
| Source code | `~/.hermes/hermes-agent/` |
