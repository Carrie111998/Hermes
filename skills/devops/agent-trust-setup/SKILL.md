---
name: agent-trust-setup
title: Agent Trust Setup
description: "Use when enabling YOLO agent mode or isolating secrets."
trigger: "User wants to disable agent permission checks, isolate secrets, or make their machine reproducible."
category: devops
---

# Agent Trust Setup

The three-pillar architecture for running AI agents (Hermes, Claude Code, Codex, etc.) with maximum autonomy and minimum risk. Inspired by the "treat your machine as your employee's device" mindset: if the machine is reproducible and secrets are isolated, a destructive command is an inconvenience (rebuild), not a disaster.

## The three pillars

### Pillar 1 — Reproducible config (chezmoi)

The machine must be instantly rebuildable. If an agent destroys it, you clone your dotfiles repo, run one script, and you're back in minutes.

**Tool: chezmoi** (Go binary, available in Arch extra repo, Homebrew, and other package managers)

```bash
# Arch / CachyOS
sudo pacman -S chezmoi

# macOS
brew install chezmoi

# Anywhere
curl -fsSL https://chezmoi.io/get | sh
```

Initialize and add key config files:

```bash
chezmoi init
chezmoi add ~/.hermes/config.yaml
chezmoi add ~/.local/bin/hermes-sec
```

**What to version:**
- `~/.hermes/config.yaml` (settings — never secrets)
- `~/.local/bin/` wrapper scripts
- `~/.config/` key configs (KDE, shell, etc.)
- `scripts/pkglist.txt` (`pacman -Qqe > pkglist.txt`) — all native packages
- `scripts/aurlist.txt` (`pacman -Qqm > aurlist.txt`) — all AUR packages
- `scripts/bootstrap.sh` — provisions a fresh install from scratch

**What NOT to version:**
- `~/.hermes/.env` (secrets go to Infisical)
- `~/.hermes/auth.json` (OAuth tokens)
- `~/.hermes/state.db` (session data)
- Anything under `~/.hermes/cache/`

See `templates/bootstrap.sh` for a known-good bootstrap script.

### Pillar 2 — Secrets isolation (Infisical)

Secrets must never live in plaintext on the agent's machine. Move them to a vault accessed at runtime, not at rest.

**Tool: Infisical** (MIT, free cloud tier at app.infisical.com, or self-hosted)

```bash
npm install -g @infisical/cli    # takes 60+ seconds, use 300s timeout
infisical login                    # opens browser, authenticate to Infisical Cloud
cd ~ && infisical init             # interactive: pick org + project, writes ~/.infisical.json
infisical secrets set API_KEY="sk-..." --env=dev
infisical run --env=dev -- hermes desktop
```

The `infisical init` command creates a `.infisical.json` file in the current directory that links it to your Infisical project. The `infisical run` command reads that config and injects secrets as environment variables into any process — no hardcoded project IDs needed.

**The "YOLO some, gate others" pattern:**
- `dev` environment — no approval gate
- `prod` environment — approval workflow required (notified, approve, lease auto-expires)

Configure approval gates: Infisical dashboard → Settings → Access Controls → Access Requests → enable for prod env.

**Migrating Hermes secrets from .env to Infisical:**
1. Read current `.env` to identify active (uncommented) secrets
2. Back up: `cp ~/.hermes/.env ~/.hermes/.env.bak.$(date +%Y%m%d_%H%M%S)`
3. Add each to Infisical: `infisical secrets set KEY="value" --env=dev`
4. Strip secrets from `.env` (keep non-secret config like `TERMINAL_ENV`, `BROWSERBASE_PROXIES`)
5. Add comment block in `.env` documenting where secrets now live
6. Verify: `grep -E '(API_KEY|PASSWORD)=' ~/.hermes/.env | grep -v '^#'` returns nothing

See `templates/hermes-sec` for a wrapper script.

### Pillar 3 — Permission-free approvals (Hermes)

Only enable AFTER pillars 1 and 2 are solid.

```bash
hermes config set approvals.mode off
hermes config set security.redact_secrets false  # if secrets are in Infisical
```

Per-invocation bypass: `hermes --yolo` or `export HERMES_YOLO_MODE=1`.

## Implementation sequence

1. Install chezmoi, init repo, add config files
2. Generate pkglist.txt + aurlist.txt
3. Create bootstrap.sh
4. Install Infisical CLI
5. User logs into Infisical cloud, creates project
6. Run `infisical init` to link a directory to the project
7. Migrate secrets from .env to Infisical
8. Create hermes-sec wrapper script
9. Strip secrets from .env
10. Commit everything to chezmoi
11. Only now: set `approvals.mode off`

## Pitfalls

- **Never flip approvals.mode off before secrets are isolated.**
- **Hermes read_file refuses .env** (defense-in-depth). Use terminal `cat` to read it.
- **Use `exec infisical run`** in wrapper scripts so signals propagate.
- **Infisical CLI uses `infisical init` (interactive) to link a directory to a project.** This writes `.infisical.json` which `infisical run` reads automatically. No need to look up or hardcode project/org IDs.
- **chezmoi naming:** `private_dot_hermes/private_config.yaml` = `~/.hermes/config.yaml` mode 0600. `executable_` prefix sets executable bit.
- **Infisical CLI npm install takes 60+ seconds.** Use 300s timeout.
- **Always back up .env before stripping secrets.**
- **Non-secret config stays in .env** (TERMINAL_ENV, BROWSERBASE_PROXIES, etc.).

## Verification checklist

- [ ] chezmoi repo initialized with at least one commit
- [ ] bootstrap.sh passes `bash -n` and is executable
- [ ] pkglist.txt and aurlist.txt generated
- [ ] Infisical CLI installed
- [ ] `.infisical.json` exists (from `infisical init`)
- [ ] .env has no plaintext secrets
- [ ] .env backup exists
- [ ] hermes-sec is executable and calls `exec infisical run`
- [ ] chezmoi git working tree clean
- [ ] Only after all above: `hermes config set approvals.mode off`

## References

- [references/infisical-vs-alternatives.md](references/infisical-vs-alternatives.md) — Detailed comparison of secrets tools.

## Templates

- [templates/bootstrap.sh](templates/bootstrap.sh) — Machine provisioning script.
- [templates/hermes-sec](templates/hermes-sec) — Hermes wrapper using `infisical run`.
