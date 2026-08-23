---
name: gbr
description: Pair a phone spectator to this Hermes host session.
version: 1.0.0
author: David Rad (LinespottingPrivate), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [MCP, Mobile, Pairing]
    related_skills: [hermes-agent]
    homepage: https://grokbuildremote.com/
prerequisites:
  commands: [gbr-agent]
---

# GBR Skill

Pair a phone running Build Remote Agent as a spectator (and veto) on this Hermes host session. Protocol is `gbr/1` only: phone app, `gbr-agent pair` (QR and printed 8-char code), then `gbr-agent run`. This skill does not make the phone the orchestrator, does not treat Hermes chat channels as the pair path, and does not require Hermes to run on a GBR host — MCP add stays on the Hermes box.

Independent product by Linespotting AB. Not affiliated with xAI or SpaceX.

## When to Use

- The user wants a phone to spectate or veto this Hermes host session.
- The user asks to pair Build Remote Agent / `gbr-agent` with Hermes.

Do not use for:

- Making the phone the orchestrator
- Pairing through Hermes messaging (Telegram, WhatsApp, Discord, …)
- Any fourth pair protocol beyond phone app + `gbr-agent pair` + `gbr-agent run`

## Prerequisites

- `gbr-agent` v0.6.0 or newer on PATH. Prefer a pinned GitHub Release binary from https://github.com/LinespottingOrg/GrokBuildRemote-Agents/releases/tag/v0.6.0 (assets `gbr-agent-<os>-<arch>`). The website `install.sh` is mutable; do not treat it as a pin.
- Phone app: [Build Remote Agent](https://grokbuildremote.com/) → Connect.
- Attach is only `http://127.0.0.1:8788` (after `gbr-agent run` on this same host) or stdio `gbr-mcp`.
- MCP setup is on the Hermes box, not on a remote GBR host. HTTP loopback works only when `gbr-agent run` is local to Hermes. Stdio `gbr-mcp` needs Node on the Hermes box.

Register MCP through `terminal` after the binary or `gbr-mcp` is present:

```
terminal(command="hermes mcp add gbr -- http://127.0.0.1:8788")
```

or stdio:

```
terminal(command="hermes mcp add gbr -- stdio -- node ./bin/gbr-mcp.js")
```

Equivalent `~/.hermes/config.yaml` (HTTP, same host):

```yaml
mcp_servers:
  gbr:
    url: "http://127.0.0.1:8788"
```

## How to Run

Invoke host commands through the `terminal` tool. Do not substitute a fourth pair path.

```
terminal(command="gbr-agent version")
terminal(command="gbr-agent pair", pty=true, timeout=180)
terminal(command="gbr-agent run", background=true)
```

`gbr-agent pair` prints an 8-char code and opens a browser QR. The phone scans the QR or types that code. Then keep `gbr-agent run` running.

## Quick Reference

| Command | Purpose |
|---------|---------|
| `gbr-agent version` | Confirm v0.6.0+ |
| `gbr-agent pair` | QR **and** printed 8-char code |
| `gbr-agent run` | Serve loopback `http://127.0.0.1:8788` |
| `gbr-agent doctor` | Prove install and pair health |
| `gbr-agent status` | List local session |
| `hermes mcp add gbr -- http://127.0.0.1:8788` | HTTP attach (same host) |
| `hermes mcp add gbr -- stdio -- node ./bin/gbr-mcp.js` | Stdio `gbr-mcp` on the Hermes box |

## Procedure

1. Confirm `gbr-agent version` reports v0.6.0 or newer via `terminal`. Done when stdout includes `v0.6` or higher.
2. On the phone, open Build Remote Agent → Connect.
3. On the host, invoke `gbr-agent pair` through `terminal` (`pty=true`). Done when a QR page is open **and** an 8-char code is printed.
4. Phone scans the QR **or** types the 8-char code. Done when the phone shows this host as paired.
5. Invoke `gbr-agent run` through `terminal` (`background=true`). Done when the process stays up and loopback `http://127.0.0.1:8788` is the attach URL (or stdio `gbr-mcp` is registered on the Hermes box).
6. Phone spectates and may veto; it does not drive the Hermes tool loop.

## Pitfalls

- Unpair on the phone before pairing a new host. Force-close is not enough.
- Website `install.sh` / `install.ps1` can change without a tag; pin GitHub Release v0.6.0+.
- `http://127.0.0.1:8788` is loopback. It is not reachable if Hermes is on another machine — use stdio `gbr-mcp` on the Hermes box instead.
- Hermes messaging channels are not `gbr/1`.
- Do not invent a fourth pair protocol.
- Phone is spectator + veto, not orchestrator.

## Verification

One command, through `terminal`:

```
terminal(command="gbr-agent doctor")
```

Done when `gbr-agent doctor` exits 0.
