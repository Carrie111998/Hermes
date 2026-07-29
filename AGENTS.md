# Hermes Agent - Development Guide

## What Hermes Is

Hermes is a personal AI agent running the same core across CLI, messaging gateway, TUI, and desktop. Capability lives at the edges (plugins, skills, MCP servers), not the core.

Two design principles:

- **Per-conversation prompt caching is sacred.** Mutating past context, swapping toolsets, or rebuilding the system prompt mid-conversation invalidates cache and multiplies cost. The one exception is context compression.
- **The core is a narrow waist; capability lives at the edges.** Every model tool ships on every API call, so the bar for new *core* tools is high. Prefer CLI command + skill, service-gated tool, or plugin over core changes.

## Key Paths & Config

```
~/.hermes/config.yaml       Main configuration (behavioral settings)
~/.hermes/.env              API keys and secrets only
$HERMES_HOME/skills/        Installed skills
~/.hermes/state.db          SQLite session store (FTS5)
~/.hermes/logs/             agent.log / errors.log / gateway.log
```

## Project Structure (concise)

```
hermes-agent/
├── run_agent.py          # AIAgent class — core conversation loop
├── model_tools.py        # Tool discovery + handle_function_call()
├── toolsets.py           # Toolset definitions, _HERMES_CORE_TOOLS
├── cli.py                # HermesCLI — interactive CLI
├── hermes_state.py       # SessionDB — SQLite session store
├── agent/                # Prompt builder, compression, memory, routing
├── hermes_cli/           # CLI subcommands, setup, commands registry
│   ├── commands.py       # COMMAND_REGISTRY — all slash commands derive from here
├── tools/                # One file per tool, auto-discovered via registry.py
├── gateway/              # Messaging gateway + platforms/
├── cron/                 # Job scheduler
├── tests/                # Pytest suite
└── website/              # Docusaurus docs
```

## AIAgent Class

```python
class AIAgent:
    def __init__(self,
        base_url: str = None, api_key: str = None, provider: str = None,
        model: str = "", max_iterations: int = 90,
        enabled_toolsets: list = None, session_id: str = None,
        # ... plus callbacks, credential_pool, reasoning_config, etc.
    ): ...
    def chat(self, message: str) -> str:
    def run_conversation(self, user_message: str, system_message: str = None,
                         conversation_history: list = None) -> dict:
```

## The Footprint Ladder (new capability decision)

Choose the highest (least-footprint) rung that solves the problem:

1. **Extend existing code** — zero new surface
2. **CLI command + skill** — agent runs `hermes <subcommand>` guided by a skill
3. **Service-gated tool (`check_fn`)** — only appears when prerequisite is configured
4. **Plugin** — third-party/niche capability in `~/.hermes/plugins/`
5. **MCP server** — structured I/O tool that's not core-fundamental
6. **New core tool** — only when fundamentally necessary (last resort)

## System prompt's execution-environment block

Host/backend guidance (OS, `$HOME`, cwd, terminal backend) is emitted by
`agent/prompt_builder.py::build_environment_hints()`. With a **remote**
terminal backend (`docker, singularity, modal, daytona, ssh`), host info is
suppressed — the prompt must never describe a host the agent can't touch.
