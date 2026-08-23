---
name: gbr
description: >
  Pair a phone running Build Remote Agent to Hermes. Requires gbr-agent run on
  the host. Attach via Bot API 127.0.0.1:8788 or hermes mcp add gbr (stdio gbr-mcp).
  Use when the user wants a mobile spectator / inject into this Hermes session.
version: 1.0.0
author: community
license: MIT
platforms: [linux, macos, windows]
compatibility: Requires gbr-agent ≥ 0.6.0 on the host. Loopback only. No mailbox keys in this file.
metadata:
  hermes:
    tags: [MCP, Mobile, Pairing, Tools]
    homepage: https://grokbuildremote.com/
    product: "Build Remote Agent"
  version: "0.6.1"
prerequisites:
  commands: [node, gbr-agent]
---

# Build Remote Agent — pairing device

One adapter. Protocol `gbr/1`. No fourth pair protocol.

Independent product by Linespotting AB. Not affiliated with xAI or SpaceX.

Hermes messaging channels (Telegram, WhatsApp, …) are not `gbr/1`. The phone
app spectates the **desktop** Hermes session through the host Bot API / MCP.

This skill is optional (not bundled). Install with `hermes skills install` from
this path, or copy to `~/.hermes/skills/`.

## Pair (unchanged)

1. Phone: [Build Remote Agent](https://grokbuildremote.com/) → Connect.
2. PC: `gbr-agent pair` — browser QR **and** printed 8-char code.
3. Phone scans QR **or** types the 8-char code.
4. PC: `gbr-agent run` (keep it running).

```bash
curl -fsSL https://grokbuildremote.com/install.sh | bash   # Windows: irm https://grokbuildremote.com/install.ps1 | iex
gbr-agent version    # need v0.6.0+
gbr-agent pair && gbr-agent run
```

Unpair on the phone before a new mailbox. Force-close is not enough.

## Attach (only these)

| How | Where |
|-----|--------|
| Bot API | `http://127.0.0.1:8788` after `gbr-agent run` |
| MCP | `gbr-mcp` stdio (same JSON as Bot API) |

Phone is spectator + veto, not orchestrator.

```bash
curl -sS http://127.0.0.1:8788/health
curl -sS http://127.0.0.1:8788/v1/sessions
curl -sS -X POST http://127.0.0.1:8788/v1/inject \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"SESSION","text":"hello","submit":true}'
```

## Hermes MCP

```bash
git clone https://github.com/LinespottingOrg/GrokBuildRemote-Agents.git
cd GrokBuildRemote-Agents/mcp/gbr-mcp && npm install
node bin/gbr-mcp.js --diagnose

# stdio (works even if you only have gbr-mcp on this box)
hermes mcp add gbr -- stdio -- node ./bin/gbr-mcp.js

# HTTP only if gbr-agent run is already on this same host:
# hermes mcp add gbr -- http://127.0.0.1:8788
```

Equivalent `~/.hermes/config.yaml` (never put mailbox keys here):

```yaml
mcp_servers:
  gbr:
    command: "node"
    args: ["/absolute/path/to/GrokBuildRemote-Agents/mcp/gbr-mcp/bin/gbr-mcp.js"]
```

Remote bots: phone **Settings → Bot API** copies relay URL + mailbox id + key. Never commit the key.

## Loop

diagnose → open/attach → lock → inject → wait idle → harvest excerpt → iterate or close

Docs: https://github.com/LinespottingOrg/GrokBuildRemote-Agents/blob/main/docs/BOT-API.md
