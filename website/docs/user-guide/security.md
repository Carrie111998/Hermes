---
sidebar_position: 8
title: "Security"
description: "Security model, exact terminal authorization, user authorization, container isolation, and production deployment best practices"
---

# Security

Hermes Agent is designed with a defense-in-depth security model. This page covers every security boundary — from command approval to container isolation to user authorization on messaging platforms.

## Overview

The security model has eight layers:

1. **User authorization** — who can talk to the agent (allowlists, DM pairing)
2. **Exact terminal authorization** — structural, non-replayable authority for each terminal operation
3. **File write safety** — denylist and optional write sandbox for `write_file`/`patch`
4. **Container isolation** — Docker/Singularity/Modal sandboxing with hardened settings
5. **MCP credential filtering** — environment variable isolation for MCP subprocesses
6. **Context file scanning** — prompt injection detection in project files
7. **Cross-session isolation** — sessions cannot access each other's data or state; cron job storage paths are hardened against path traversal attacks
8. **Input sanitization** — working directory parameters in terminal tool backends are validated against an allowlist to prevent shell injection

## Exact Terminal Authorization

Hermes does not interpret terminal text with keywords, regular expressions, command classifiers, or routers. The active LLM is the sole semantic authority. The runtime verifies only whether the exact pending operation has structural authority.

### Approval Modes

The approval system supports two modes, configured via `approvals.mode` in `~/.hermes/config.yaml`:

```yaml
approvals:
  mode: manual                    # manual | off
  timeout: 300                    # seconds to wait for user response (default: 300)
  cron_mode: deny                 # deny | approve — whether cron gets an explicit whole-terminal grant
  mcp_reload_confirm: true        # /reload-mcp asks before invalidating the MCP tool cache
  destructive_slash_confirm: true # /clear, /new, /reset, /undo prompt before discarding state
```

The full set of keys:

| Key | Default | What it controls |
|---|---|---|
| `mode` | `manual` | Owner-prompt policy for exact terminal operations — see the table below. |
| `timeout` | `300` | Seconds Hermes waits for an approval reply before timing out. |
| `cron_mode` | `deny` | Whether [cron jobs](./features/cron.md) receive an explicit whole-terminal capability. `deny` withholds it; `approve` grants the terminal surface for that cron run without interpreting command prose. |
| `mcp_reload_confirm` | `true` | When true, `/reload-mcp` asks before rebuilding the MCP tool set. Rebuilding invalidates the provider prompt cache (tool schemas live in the system prompt), so the next message re-sends full input tokens. Users who click **Always Approve** flip this key to `false`. |
| `destructive_slash_confirm` | `true` | When true, destructive session slash commands (`/clear`, `/new`, `/reset`, `/undo`) prompt before discarding conversation state. Three-option dialog (Approve Once / Always Approve / Cancel) routed through native yes/no buttons on Telegram, Discord, and Slack; text fallback elsewhere. Users who click **Always Approve** flip this key to `false`. TUI uses its own modal overlay (set `HERMES_TUI_NO_CONFIRM=1` to opt out there). |

| Mode | Behavior |
|------|----------|
| **manual** (default) | Require structural authority for every exact host-reaching terminal operation. |
| **off** | Skip the owner prompt while retaining invocation-bound, non-replayable authority — equivalent to running with `--yolo`. |

Legacy `smart` values are normalized to `manual`; no auxiliary model is allowed to grant or deny authorization.

:::warning
Setting `approvals.mode: off` disables terminal owner prompts. Use only in trusted environments with an appropriate structural isolation boundary.
:::

### YOLO Mode

YOLO mode bypasses terminal owner prompts for the current session. It does not add command-text interpretation or reusable command-pattern grants. It can be activated three ways:

1. **CLI flag**: Start a session with `hermes --yolo` or `hermes chat --yolo`
2. **Slash command**: Type `/yolo` during a session to toggle it on/off
3. **Environment variable**: Set `HERMES_YOLO_MODE=1`

The `/yolo` command is a **toggle** — each use flips the mode on or off:

```
> /yolo
  ⚡ YOLO mode ON — owner prompts skipped; exact authority still applies.

> /yolo
  ⚠ YOLO mode OFF — exact terminal operations require owner approval.
```

YOLO mode is available in both CLI and gateway sessions. Internally, it sets the `HERMES_YOLO_MODE` environment variable which is checked before every command execution.

When YOLO is active, Hermes shows two persistent visual reminders so it's hard to forget that approval prompts are bypassed:

- A red banner line at session start when YOLO is already active: `⚠ YOLO mode — all approval prompts bypassed`. Hidden when YOLO is off so the default banner stays uncluttered.
- A `⚠ YOLO` fragment in the status bar across all width tiers, updated live as you toggle YOLO on or off (rich-text renderer and plain-text fallback).

:::danger
YOLO mode grants the whole terminal surface for the session. Use it only when you trust the model and the execution environment; prefer an isolated backend for experiments.
:::

For state-discarding session slash commands (`/clear`, `/new` / `/reset`, `/undo`, `/quit --delete` — `/exit --delete` is an alias), the CLI also prompts for confirmation before running them. This is a separate state-loss confirmation, not terminal command authority. See [Slash Commands — Confirmation prompts for state-discarding slash commands](../reference/slash-commands.md#confirmation-prompts-for-state-discarding-slash-commands).

### Model authority and exact execution capabilities

Hermes does not classify command text with keywords, regexes, fuzzy matching, or a second non-LLM policy engine. The active LLM remains the sole semantic authority.

The execution boundary enforces only exact, non-semantic contracts:

- isolated terminal backends are authorized by their structural isolation boundary;
- an owner can approve the exact bytes of one terminal operation once;
- an approved plan can carry exact, bounded command capabilities with an expiry and use count;
- delegated workers may consume those capabilities but cannot mint or broaden them;
- `approvals.mode: off`, session YOLO, and cron approval grant an explicit whole-surface capability rather than interpreting command prose;
- unknown or mismatched opaque capabilities fail closed.

Authorization never depends on whether a command contains a word such as `delete`, `sudo`, `deploy`, or any other inferred category.

### Approval Timeout

When an exact terminal authorization prompt appears, the user has a configurable amount of time to respond. If no response is given within the timeout, the operation is **denied** by default (fail-closed).

Configure the timeout in `~/.hermes/config.yaml`:

```yaml
approvals:
  timeout: 300  # seconds (default: 300)
```

### Approval flow

In manual mode, a host-reaching terminal operation without an exact capability pauses for the owner. The prompt shows the exact operation bytes and offers only:

- **once** — authorize this exact operation once;
- **deny** — do not execute it.

There are no semantic session patterns and no permanent command-text allowlist. Gateway approval responses are resolved by opaque approval ID; ordinary chat text is not parsed as an authorization decision.

For a long autonomous task, the trusted planning model can request a bounded plan capability containing exact operations. The owner approves the plan boundary once, and workers consume only the exact entries in that capability. Any changed byte, expired capability, exhausted use count, or missing owner binding requires a new explicit decision.

## File Write Safety {#file-write-safety}

Before `write_file` or `patch` touches disk, Hermes checks the target path against a denylist and an optional sandbox. Blocked writes return an error to the agent immediately — **there is no approval prompt** and no way to override from the chat UI. The model may still claim the edit succeeded; when `display.file_mutation_verifier` is on (default), trust the [file-mutation verifier footer](./configuration.md#file-mutation-verifier) over the assistant's closing summary.

### Protected paths (always blocked)

These categories are always denied, even when `HERMES_WRITE_SAFE_ROOT` is unset:

| Category | Examples |
|----------|----------|
| OS credential stores | `~/.ssh/`, `~/.aws/`, `~/.kube/`, `/etc/sudoers`, `~/.netrc` |
| Hermes credential stores | `auth.json`, `.env`, `.anthropic_oauth.json`, `mcp-tokens/`, `pairing/` under HERMES_HOME (active profile and global root) |
| Project secret files | `.env`, `.env.local`, `.env.production`, `.envrc` anywhere on disk |

Sensitive paths inside the safe root are still blocked — pointing `HERMES_WRITE_SAFE_ROOT` at `$HOME` does not allow writing `~/.ssh/id_rsa`.

Safe-root violations return `Write denied: '…' is outside HERMES_WRITE_SAFE_ROOT (…)`. Credential-path blocks use `Write denied: '…' is a protected system/credential file.`

### HERMES_WRITE_SAFE_ROOT (optional sandbox)

When set, `write_file` and `patch` may only target paths inside the listed directory prefix(es). Anything outside is **hard-blocked** by the file boundary and cannot be overridden by a terminal approval.

- Set automatically in the [official Docker image](https://github.com/NousResearch/hermes-agent) (`HERMES_WRITE_SAFE_ROOT=/opt/data`)
- Supports multiple roots separated by `:` on Unix or `;` on Windows
- **Do not add to `~/.hermes/.env` casually.** If you set it to a project directory, the agent cannot write to `~/.hermes/cron/jobs.json`, profile skills, or other Hermes state outside that prefix

To allow both a workspace and Hermes home:

```bash
export HERMES_WRITE_SAFE_ROOT=/path/to/project:/home/you/.hermes
```

Unset the variable to restore unrestricted writes (subject to the protected-path denylist). Full reference: [HERMES_WRITE_SAFE_ROOT](../reference/environment-variables.md#hermes_write_safe_root).

### Cron and other Hermes state

Do not ask the agent to `patch` `~/.hermes/cron/jobs.json` directly. Use the `cronjob` tool, [`hermes cron`](./features/cron.md), or `/cron` — they update the job store through the supported API. The same applies to other Hermes control files when write safety blocks direct edits.

:::note Defense-in-depth, not a hard boundary
Write guards apply to `write_file` and `patch` only. The `terminal` tool runs as the same OS user and can still `cat` or overwrite denied paths via shell commands. The denylist reduces accidental damage and gives models a clear stop signal; it does not sandbox a hostile or compromised agent.
:::

## User Authorization (Gateway)

When running the messaging gateway, Hermes controls who can interact with the bot through a layered authorization system.

### Authorization Check Order

The `_is_user_authorized()` method checks in this order:

1. **Per-platform allow-all flag** (e.g., `DISCORD_ALLOW_ALL_USERS=true`)
2. **DM pairing approved list** (users approved via pairing codes)
3. **Platform-specific allowlists** (e.g., `TELEGRAM_ALLOWED_USERS=12345,67890`)
4. **Global allowlist** (`GATEWAY_ALLOWED_USERS=12345,67890`)
5. **Global allow-all** (`GATEWAY_ALLOW_ALL_USERS=true`)
6. **Default: deny**

### Platform Allowlists

Set allowed user IDs as comma-separated values in `~/.hermes/.env`:

```bash
# Platform-specific allowlists
TELEGRAM_ALLOWED_USERS=123456789,987654321
DISCORD_ALLOWED_USERS=111222333444555666
WHATSAPP_ALLOWED_USERS=15551234567
SLACK_ALLOWED_USERS=U01ABC123

# Cross-platform allowlist (checked for all platforms)
GATEWAY_ALLOWED_USERS=123456789

# Per-platform allow-all (use with caution)
DISCORD_ALLOW_ALL_USERS=true

# Global allow-all (use with extreme caution)
GATEWAY_ALLOW_ALL_USERS=true
```

:::warning
If **no allowlists are configured** and `GATEWAY_ALLOW_ALL_USERS` is not set, **all users are denied**. The gateway logs a warning at startup:

```
No user allowlists configured. All unauthorized users will be denied.
Set GATEWAY_ALLOW_ALL_USERS=true in ~/.hermes/.env to allow open access,
or configure platform allowlists (e.g., TELEGRAM_ALLOWED_USERS=your_id).
```
:::

### DM Pairing System

For more flexible authorization, Hermes includes a code-based pairing system. Instead of requiring user IDs upfront, unknown users receive a one-time pairing code that the bot owner approves via the CLI.

**How it works:**

1. An unknown user sends a DM to the bot
2. The bot replies with an 8-character pairing code
3. The bot owner runs `hermes pairing approve <platform> <code>` on the CLI
4. The user is permanently approved for that platform

Control how unauthorized direct messages are handled in `~/.hermes/config.yaml`:

```yaml
unauthorized_dm_behavior: pair

whatsapp:
  unauthorized_dm_behavior: ignore
```

- `pair` is the default for chat-style DM platforms. Unauthorized DMs get a pairing code reply.
- `ignore` silently drops unauthorized DMs.
- Email defaults to `ignore` unless `platforms.email.unauthorized_dm_behavior: pair` is set, because inboxes can contain unrelated unread mail.
- Platform sections override the global default, so you can keep pairing on Telegram while keeping WhatsApp silent.

**Security features** (based on OWASP + NIST SP 800-63-4 guidance):

| Feature | Details |
|---------|---------|
| Code format | 8-char from 32-char unambiguous alphabet (no 0/O/1/I) |
| Randomness | Cryptographic (`secrets.choice()`) |
| Code TTL | 1 hour expiry |
| Rate limiting | 1 request per user per 10 minutes |
| Pending limit | Max 3 pending codes per platform |
| Lockout | 5 failed approval attempts → 1-hour lockout |
| File security | `chmod 0600` on all pairing data files |
| Logging | Codes are never logged to stdout |

**Pairing CLI commands:**

```bash
# List pending and approved users
hermes pairing list

# Approve a pairing code
hermes pairing approve telegram ABC12DEF

# Revoke a user's access
hermes pairing revoke telegram 123456789

# Clear all pending codes
hermes pairing clear-pending
```

:::tip Docker users: run pairing commands as the `hermes` user
The official Docker image runs the gateway as the unprivileged `hermes` user
(uid 10000) via `gosu`, but `docker exec` defaults to root. Approval files
created by root are written with mode `0600 root:root` and the gateway
cannot read them — the approval is silently ignored ([#10270][i10270]).

Always pass `-u hermes`:

```bash
docker exec -u hermes hermes-agent hermes pairing approve telegram ABC12DEF
```

If you already ran the command as root and the user is still unauthorized,
restart the container — the entrypoint will fix ownership on the next start.

[i10270]: https://github.com/NousResearch/hermes-agent/issues/10270
:::

**Storage:** Pairing data is stored in `~/.hermes/pairing/` with per-platform JSON files:
- `{platform}-pending.json` — pending pairing requests
- `{platform}-approved.json` — approved users
- `_rate_limits.json` — rate limit and lockout tracking

## Container Isolation

When using the `docker` terminal backend, Hermes applies strict security hardening to every container.

### Docker Security Flags

Every container runs with these flags (defined in `tools/environments/docker.py`):

```python
_BASE_SECURITY_ARGS = [
    "--cap-drop", "ALL",                          # Drop ALL Linux capabilities
    "--cap-add", "DAC_OVERRIDE",                  # Root can write to bind-mounted dirs
    "--cap-add", "CHOWN",                         # Package managers need file ownership
    "--cap-add", "FOWNER",                        # Package managers need file ownership
    "--security-opt", "no-new-privileges",         # Block privilege escalation
    "--pids-limit", "256",                         # Limit process count
    "--tmpfs", "/tmp:rw,nosuid,size=512m",         # Size-limited /tmp
    "--tmpfs", "/var/tmp:rw,noexec,nosuid,size=256m",  # No-exec /var/tmp
]
```

`SETUID`/`SETGID` are **not** in the base list — they're added conditionally when the container starts as root and an init/entrypoint must drop privileges (the s6 privilege-drop path). They're skipped when the container already runs as a non-root `--user`. The `/run` tmpfs is also split out from the base list and mounted per-image (hardened `noexec` by default, `exec` only for s6-overlay images that exec from `/run`).

### Resource Limits

Container resources are configurable in `~/.hermes/config.yaml`:

```yaml
terminal:
  backend: docker
  docker_image: "nikolaik/python-nodejs:python3.11-nodejs20"
  docker_forward_env: []  # Explicit allowlist only; empty keeps secrets out of the container
  container_cpu: 1        # CPU cores
  container_memory: 5120  # MB (default 5GB)
  container_disk: 51200   # MB (default 50GB, requires overlay2 on XFS)
  container_persistent: true  # Persist filesystem across sessions
```

### Filesystem Persistence

- **Persistent mode** (`container_persistent: true`): Bind-mounts `/workspace` and `/root` from `~/.hermes/sandboxes/docker/<task_id>/`
- **Ephemeral mode** (`container_persistent: false`): Uses tmpfs for workspace — everything is lost on cleanup

:::tip
For production gateway deployments, use a `docker`, `modal`, `daytona`, or `vercel_sandbox` backend to isolate agent operations from your host system. Isolation is its own structural authority boundary, not a command-text classification shortcut.
:::

:::warning
If you add names to `terminal.docker_forward_env`, those variables are intentionally injected into the container for terminal commands. This is useful for task-specific credentials like `GITHUB_TOKEN`, but it also means code running in the container can read and exfiltrate them.
:::

## Terminal Backend Security Comparison

| Backend | Isolation | Structural authority boundary | Best For |
|---------|-----------|-------------------------------|----------|
| **local** | None — runs on host | Exact capability or explicit whole-surface grant required | Development, trusted users |
| **ssh** | Remote machine | Exact capability or explicit whole-surface grant required | Running on a separate server |
| **docker** | Container | Container defines the isolated execution surface | Production gateway |
| **singularity** | Container | Container defines the isolated execution surface | HPC environments |
| **modal** | Cloud sandbox | Sandbox defines the isolated execution surface | Scalable cloud isolation |
| **daytona** | Cloud sandbox | Sandbox defines the isolated execution surface | Persistent cloud workspaces |
| **vercel_sandbox** | Cloud microVM | Sandbox defines the isolated execution surface | Cloud execution with snapshot persistence |

## Environment Variable Passthrough {#environment-variable-passthrough}

Both `execute_code` and `terminal` strip sensitive environment variables from child processes to prevent credential exfiltration by LLM-generated code. However, skills that declare `required_environment_variables` legitimately need access to those vars.

### How It Works

Two mechanisms allow specific variables through the sandbox filters:

**1. Skill-scoped passthrough (automatic)**

When a skill is loaded (via `skill_view` or the `/skill` command) and declares `required_environment_variables`, any of those vars that are actually set in the environment are automatically registered as passthrough. Missing vars (still in setup-needed state) are **not** registered.

```yaml
# In a skill's SKILL.md frontmatter
required_environment_variables:
  - name: TENOR_API_KEY
    prompt: Tenor API key
    help: Get a key from https://developers.google.com/tenor
```

After loading this skill, `TENOR_API_KEY` passes through to `execute_code`, `terminal` (local), **and remote backends (Docker, Modal)** — no manual configuration needed.

:::info Docker & Modal
Prior to v0.5.1, Docker's `forward_env` was a separate system from the skill passthrough. They are now merged — skill-declared env vars are automatically forwarded into Docker containers and Modal sandboxes without needing to add them to `docker_forward_env` manually.
:::

**2. Config-based passthrough (manual)**

For env vars not declared by any skill, add them to `terminal.env_passthrough` in `config.yaml`:

```yaml
terminal:
  env_passthrough:
    - MY_CUSTOM_KEY
    - ANOTHER_TOKEN
```

### Credential File Passthrough (OAuth tokens, etc.) {#credential-file-passthrough}

Some skills need **files** (not just env vars) in the sandbox — for example, Google Workspace stores OAuth tokens as `google_token.json` under the active profile's `HERMES_HOME`. Skills declare these in frontmatter:

```yaml
required_credential_files:
  - path: google_token.json
    description: Google OAuth2 token (created by setup script)
  - path: google_client_secret.json
    description: Google OAuth2 client credentials
```

When loaded, Hermes checks if these files exist in the active profile's `HERMES_HOME` and registers them for mounting:

- **Docker**: Read-only bind mounts (`-v host:container:ro`)
- **Modal**: Mounted at sandbox creation + synced before each command (handles mid-session OAuth setup)
- **Local**: No action needed (files already accessible)

You can also list credential files manually in `config.yaml`:

```yaml
terminal:
  credential_files:
    - google_token.json
    - my_custom_oauth_token.json
```

Paths are relative to `~/.hermes/`. Files are mounted to `/root/.hermes/` inside the container. This list is read by `tools/credential_files.py` (`terminal.credential_files`) — it lives under the `terminal:` block but is loaded by the credential-files module, not the core terminal backend, so it isn't part of the bundled `DEFAULT_CONFIG` snapshot.

### What Each Sandbox Filters

| Sandbox | Default Filter | Passthrough Override |
|---------|---------------|---------------------|
| **execute_code** | Blocks vars containing `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `CREDENTIAL`, `PASSWD`, `AUTH` in name; only allows safe-prefix vars through | ✅ Passthrough vars bypass both checks |
| **terminal** (local) | Blocks explicit Hermes infrastructure vars (provider keys, gateway tokens, tool API keys) | ✅ Passthrough vars bypass the blocklist |
| **terminal** (Docker) | No host env vars by default | ✅ Passthrough vars + `docker_forward_env` forwarded via `-e` |
| **terminal** (Modal) | No host env/files by default | ✅ Credential files mounted; env passthrough via sync |
| **MCP** | Blocks everything except safe system vars + explicitly configured `env` | ❌ Not affected by passthrough (use MCP `env` config instead) |

### Security Considerations

- The passthrough only affects vars you or your skills explicitly declare — the default security posture is unchanged for arbitrary LLM-generated code
- Credential files are mounted **read-only** into Docker containers
- Skills Guard scans skill content for suspicious env access patterns before installation
- Missing/unset vars are never registered (you can't leak what doesn't exist)
- Hermes infrastructure secrets (provider API keys, gateway tokens) should never be added to `env_passthrough` — they have dedicated mechanisms

## MCP Credential Handling

MCP (Model Context Protocol) server subprocesses receive a **filtered environment** to prevent accidental credential leakage.

### Safe Environment Variables

Only these variables are passed through from the host to MCP stdio subprocesses:

```
PATH, HOME, USER, LANG, LC_ALL, TERM, SHELL, TMPDIR
```

Plus any `XDG_*` variables. All other environment variables (API keys, tokens, secrets) are **stripped**.

Variables explicitly defined in the MCP server's `env` config are passed through:

```yaml
mcp_servers:
  github:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "ghp_..."  # Only this is passed
```

### Credential Redaction

Error messages from MCP tools are sanitized before being returned to the LLM. The following patterns are replaced with `[REDACTED]`:

- GitHub PATs (`ghp_...`)
- OpenAI-style keys (`sk-...`)
- Bearer tokens
- `token=`, `key=`, `API_KEY=`, `password=`, `secret=` parameters

### Website Access Policy

You can restrict which websites the agent can access through its web and browser tools. This is useful for preventing the agent from accessing internal services, admin panels, or other sensitive URLs.

```yaml
# In ~/.hermes/config.yaml
security:
  website_blocklist:
    enabled: true
    domains:
      - "*.internal.company.com"
      - "admin.example.com"
    shared_files:
      - "/etc/hermes/blocked-sites.txt"
```

When a blocked URL is requested, the tool returns an error explaining the domain is blocked by policy. The blocklist is enforced across `web_search`, `web_extract`, `browser_navigate`, and all URL-capable tools.

See [Website Blocklist](/user-guide/configuration#website-blocklist) in the configuration guide for full details.

### SSRF Protection

All URL-capable tools (web search, web extract, vision, browser) validate URLs before fetching them to prevent Server-Side Request Forgery (SSRF) attacks. Blocked addresses include:

- **Private networks** (RFC 1918): `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`
- **Loopback**: `127.0.0.0/8`, `::1`
- **Link-local**: `169.254.0.0/16` (includes cloud metadata at `169.254.169.254`)
- **CGNAT / shared address space** (RFC 6598): `100.64.0.0/10` (Tailscale, WireGuard VPNs)
- **Cloud metadata hostnames**: `metadata.google.internal`, `metadata.goog`
- **Reserved, multicast, and unspecified addresses**

SSRF protection is always active for internet-facing use and DNS failures are treated as blocked (fail-closed). Redirect chains are re-validated at each hop to prevent redirect-based bypasses.

#### Intentionally allowing private URLs

Some setups legitimately need private/internal URL access — home networks that resolve `home.arpa` to RFC 1918 space, LAN-only Ollama/llama.cpp endpoints, internal wikis, cloud metadata debugging, and the like. For those cases there's a global opt-out:

```yaml
security:
  allow_private_urls: true   # default: false
```

When on, web tools, the browser, vision URL fetches, and gateway media downloads no longer reject RFC 1918 / loopback / link-local / CGNAT / cloud-metadata destinations. **This is a deliberate trust boundary** — only enable it on machines where the agent running arbitrary prompt-injected URLs against the local network is an acceptable risk. Public-facing gateways should leave it off.

The host-substring guard (which blocks lookalike Unicode domain tricks even when the underlying IP is public) stays on regardless of this setting.

### Context File Injection Protection

Context files (AGENTS.md, .cursorrules, SOUL.md) are scanned for prompt injection before being included in the system prompt. The scanner checks for:

- Instructions to ignore/disregard prior instructions
- Hidden HTML comments with suspicious keywords
- Attempts to read secrets (`.env`, `credentials`, `.netrc`)
- Credential exfiltration via `curl`
- Invisible Unicode characters (zero-width spaces, bidirectional overrides)

Blocked files show a warning:

```
[BLOCKED: AGENTS.md contained potential prompt injection (prompt_injection). Content not loaded.]
```

## Best Practices for Production Deployment

### Gateway Deployment Checklist

1. **Set explicit allowlists** — never use `GATEWAY_ALLOW_ALL_USERS=true` in production
2. **Use container backend** — set `terminal.backend: docker` in config.yaml
3. **Restrict resource limits** — set appropriate CPU, memory, and disk limits
4. **Store secrets securely** — keep API keys in `~/.hermes/.env` with proper file permissions
5. **Enable DM pairing** — use pairing codes instead of hardcoding user IDs when possible
6. **Review active capabilities** — keep plan scopes exact, bounded, and short-lived
7. **Set `terminal.cwd`** — don't let the agent operate from sensitive directories
8. **Run as non-root** — never run the gateway as root
9. **Monitor logs** — check `~/.hermes/logs/` for unauthorized access attempts
10. **Keep updated** — run `hermes update` regularly for security patches

### Securing API Keys

```bash
# Set proper permissions on the .env file
chmod 600 ~/.hermes/.env

# Keep separate keys for different services
# Never commit .env files to version control
```

### Network Isolation

For maximum security, run the gateway on a separate machine or VM. Set `terminal.backend: ssh` in `config.yaml`, then provide host details via environment variables in `~/.hermes/.env`:

```yaml
# ~/.hermes/config.yaml
terminal:
  backend: ssh
```

```bash
# ~/.hermes/.env
TERMINAL_SSH_HOST=agent-worker.local
TERMINAL_SSH_USER=hermes
TERMINAL_SSH_KEY=~/.ssh/hermes_agent_key
```

The SSH connection details live in `.env` (not `config.yaml`) so they aren't checked in or shared along with profile exports. This keeps the gateway's messaging connections separate from the agent's command execution.

## Supply-chain advisory checking

Hermes ships with a built-in advisory scanner that flags Python packages in the active venv that match a curated catalog of known-compromised versions (supply-chain worms like the May 2026 `mistralai 2.4.6` poisoning). Implementation lives in `hermes_cli/security_advisories.py`.

How it runs:

- **CLI startup banner.** A one-line warning is printed if any advisory matches, with a pointer to `hermes doctor` for the full remediation.
- **`hermes doctor`.** Surfaces every active advisory with version specifics and 2-4 step remediation instructions.
- **Gateway startup.** Logged to `gateway.log`; the first interactive message gets a short operator banner.

Each advisory carries a stable id. Once you have read and acted on it you can dismiss it for good:

```bash
hermes doctor --ack <advisory-id>
```

The ack is persisted to `config.security.acked_advisories` and survives restart. Old advisories are intentionally **not** removed from the catalog — leaving them in place keeps fresh installs warned about historically poisoned versions that might still be cached in a private mirror.

The check itself is stdlib-only and runs from one `importlib.metadata.version()` lookup per advisory, so it's safe to run on every startup.

### Lazy install of optional dependencies

Many features (Mistral TTS, ElevenLabs, Honcho memory, Bedrock, Slack, Matrix, …) depend on Python packages that not every user needs. Hermes installs these **lazily** on first use rather than eagerly under `hermes-agent[all]`. The implementation lives in `tools/lazy_deps.py`.

The trade-off this fixes:

- **Fragility.** When one extra's transitive dependency becomes unavailable on PyPI (quarantined for malware, yanked, broken upload), the entire `[all]` resolve would fail and fresh installs would silently fall back to a stripped tier — losing 10+ unrelated extras at once. Lazy install isolates each backend so one poisoned dep can't break unrelated features.
- **Bloat.** A user who only ever talks to one provider no longer pulls hundreds of packages they will never import.

How it works:

1. A backend module calls `ensure("feature.name")` at the top of its first-import path.
2. If the deps are missing, `ensure` checks `security.allow_lazy_installs` in `config.yaml` (default `true`) and runs a venv-scoped `pip install` for the allowlisted specs.
3. If the install fails or the user has disabled lazy installs, the call raises `FeatureUnavailable` with the actual pip stderr and a pointer at `hermes tools`.

Security guarantees enforced by `tools/lazy_deps.py`:

| Guarantee | What it means |
|---|---|
| Venv-scoped only | Installs target `sys.executable` in the active venv — never the system Python |
| PyPI by name only | Specs accept `"package>=1.0,<2"` syntax. No `--index-url`, `git+https://`, or file: paths — a malicious `config.yaml` cannot redirect the install |
| Allowlist | Only specs that appear in the in-tree `LAZY_DEPS` map can be installed via this path. A typo in a feature name does NOT get install-anything semantics |
| Opt-out | Set `security.allow_lazy_installs: false` to disable runtime installs entirely. Useful for restricted networks or strict security postures |
| No silent retries | Failures surface as `FeatureUnavailable` — no caching of bad state, no retry storms |

To disable runtime installs:

```yaml
# ~/.hermes/config.yaml
security:
  allow_lazy_installs: false
```

When disabled, backends that need optional deps will tell the user to run the install manually (`pip install …`) or pick a different backend via `hermes tools`.
