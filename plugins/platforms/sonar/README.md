# Hermes Agent gateway (native)

Hermes ships a **first-class Sonar platform plugin** (no external bridge script required):

- Path: `plugins/platforms/sonar/` in [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
- Enable: `hermes gateway setup` or `config.yaml` → `gateway.platforms.sonar.enabled: true`
- Transport: `sonar-cli listen` / `sonar-cli send` (this repo)

## Quick start (Hermes users)

```bash
# 1. Build/install sonar-cli from this repository
cargo install --path <crates/sonar-cli path per your layout>

# 2. One-time identity
export SONAR_CLI_HOME=~/.sonar-agent
sonar-cli init && sonar-cli publish

# 3. Configure Hermes
export SONAR_ALLOWED_SENDERS="npub1YOUR_PERSONAL_KEY"
export SONAR_HOME_CHANNEL="npub1YOUR_PERSONAL_KEY"   # cron deliver=sonar
hermes config set gateway.platforms.sonar.enabled true

# 4. Run gateway (replaces legacy sonar_bridge_hermes.py systemd bridge)
hermes gateway
```

## JSON contract (stable — do not break without versioning)

`sonar-cli listen` emits **one JSON object per line**:

| Field | Meaning |
|-------|---------|
| `type` | `"message"` for inbound DMs |
| `sender` | Sender **npub** (not `from`) |
| `content` | Plain text body (not `text`) |
| `id` | Message id for deduplication |
| `mine` | Skip when true |

Send: `sonar-cli send --to <npub> --text "..."` (no `--group` on send).

## What stays in **this** repo vs Hermes

| bitchat-to-sonar | hermes-agent |
|------------------|--------------|
| `sonar-cli` binary, Marmot/Nostr protocol | Gateway plugin `plugins/platforms/sonar/` |
| CLI docs + JSON contract | Agent loop, tools, memory, MCP |
| `examples/echo-bridge` (optional, agent-agnostic) | `hermes gateway`, cron `deliver=sonar` |

Legacy **out-of-tree** installer (still valid for non-gateway setups):

`sonar-hermes-bridge` Hermes skill → `scripts/install-sonar-bridge.sh`

Prefer **`hermes gateway`** when running Hermes Agent daily.