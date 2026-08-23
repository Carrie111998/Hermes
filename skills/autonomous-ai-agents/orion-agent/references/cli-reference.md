# Orion CLI Reference

Live sources when anything looks stale: `orion --help`, `orion <command> --help`,
https://your-orion-docs.example/docs/reference/cli-commands

### Global Flags

```
orion [flags] [command]        (no subcommand = interactive chat)

  --version, -V             Show version
  -z, --oneshot PROMPT      One-shot: print ONLY the final response (for scripts/pipes)
  -m MODEL  --provider P    Model/provider override for this invocation
  -t, --toolsets LIST       Comma-separated toolsets for this invocation
  --resume, -r SESSION      Resume session by ID or title
  --continue, -c [NAME]     Resume by name, or most recent session
  --worktree, -w            Isolated git worktree mode (parallel agents)
  --skills, -s SKILL        Preload skills (comma-separate or repeat)
  --profile, -p NAME        Use a named profile
  --yolo                    Skip dangerous command approval
  --tui / --cli             Force the Ink TUI / classic REPL
  --ignore-rules            Skip AGENTS.md/SOUL.md/memory/skill injection
  --safe-mode               Disable ALL customizations (troubleshooting)
  --pass-session-id         Include session ID in system prompt
```

### Chat

```
orion chat [flags]
  -q, --query TEXT          Single query, non-interactive
  --image PATH              Attach a local image to a single query
  -Q, --quiet               Suppress banner, spinner, tool previews
  --checkpoints             Enable filesystem checkpoints (/rollback)
  --max-turns N             Cap tool-calling iterations
  --source TAG              Session source tag (default: cli)
```
(plus the global flags above)

### Configuration

```
orion setup [section]      Wizard (model|tts|terminal|gateway|tools|agent)
orion model                Interactive model/provider picker
orion fallback [add|remove|list]  Fallback provider chain
orion config [show|edit|get|set|unset|path|env-path|check|migrate]
orion login / logout       OAuth sign-in / clear stored auth
orion doctor [--fix]       Check dependencies and config
orion status [--all]       Component status
```

### Tools & Skills

```
orion tools [list|enable NAME|disable NAME]   Per-platform toolsets (curses UI with no args)

orion skills list|browse|search QUERY|inspect ID
orion skills install ID    Hub identifier OR a direct https://…/SKILL.md URL
orion skills config        Enable/disable skills per platform
orion skills check|update|uninstall|publish PATH
orion skills tap add REPO  Add a GitHub repo as a skill source
orion bundles              Skill bundles (one /<name> alias loads several skills)
```

### MCP Servers

```
orion mcp add NAME (--url or --command) | remove | list | test NAME
orion mcp catalog | install NAME     Curated catalog install
orion mcp configure NAME             Toggle tool selection
orion mcp serve                      Run Orion as an MCP server
```
Details (transport, tool discovery, catalog): `references/native-mcp.md`.

### Gateway (Messaging Platforms)

```
orion gateway run|install|start|stop|restart|status|setup
```

20+ platforms: Telegram, Discord, Slack, WhatsApp (Baileys + Business Cloud API), iMessage (Photon — `orion photon setup`), Signal, Email, SMS, Matrix, Mattermost, Teams, LINE, SimpleX, ntfy, Google Chat, Home Assistant, DingTalk, Feishu, WeCom, Weixin, API Server, Webhooks. Open WebUI connects via the API Server adapter. Most adapters ship under `plugins/platforms/`.
Docs: https://your-orion-docs.example/docs/user-guide/messaging/

### Sessions

```
orion sessions list|browse|rename ID TITLE|delete ID|export OUT|prune|stats
```

### Cron / Webhooks

```
orion cron list|create SCHED|edit ID|pause|resume|run ID|remove|status
    Schedules: '30m', 'every 2h', '0 9 * * *', ISO timestamp
orion webhook subscribe NAME|list|remove NAME|test NAME
```
Webhook payloads/routes: `references/webhooks.md`.

### Profiles

```
orion profile list|create NAME (--clone|--clone-all|--clone-from)|use|show|delete
orion profile rename A B | alias NAME | export NAME | import FILE
```

### Credentials & Pools

```
orion auth                 Interactive credential manager
orion auth add [PROVIDER]  Add OAuth or API-key credential (nous, openai-codex, qwen-oauth, …)
orion auth list|remove P IDX|reset PROVIDER|status
```
Multiple credentials per provider form a pool that rotates automatically and skips exhausted keys.

### Other

```
orion desktop / gui        Native desktop app
orion dashboard            Web admin panel + embedded chat (--stop / --status)
orion proxy                OpenAI-compatible local proxy backed by an OAuth provider
orion portal               Quick setup / sign in via Nous Portal
orion kanban <verb>        Multi-agent work-queue board
orion project              Named multi-folder workspaces
orion skin list|use|set    Switch/tweak skins (see references/themes.md)
orion pets <verb>          Pet mascots (see references/petdex.md)
orion memory setup|status|off|reset   Memory provider
orion secrets bitwarden|onepassword   External secret stores
orion moa                  Mixture-of-Agents slots
orion hooks / security / backup / import / checkpoints / console
orion logs [-f] [errors]   View agent/error logs
orion send                 One-off message through a gateway platform
orion pairing / plugins / insights / journey / computer-use
orion acp                  ACP server (IDE integration)
orion completion bash|zsh|fish
orion update / uninstall / claw migrate
```

Plugin- and provider-supplied subcommands (e.g. `orion photon setup`) only appear once their plugin is installed/active.

### Where to Find Things

| Looking for... | Location |
|---|---|
| Config options | `orion config edit` · [Configuration docs](https://your-orion-docs.example/docs/user-guide/configuration) |
| Tools / toolsets | `orion tools list` · [Tools reference](https://your-orion-docs.example/docs/reference/tools-reference) |
| Skills catalog | `orion skills browse` · [Skills catalog](https://your-orion-docs.example/docs/reference/skills-catalog) |
| Provider setup | `orion model` · [Providers guide](https://your-orion-docs.example/docs/integrations/providers) |
| Env variables | `orion config env-path` · [Env vars reference](https://your-orion-docs.example/docs/reference/environment-variables) |
| Gateway logs | `~/.orion/logs/gateway.log` (or `orion logs`) |
| Sessions | `orion sessions browse` (reads state.db) |
