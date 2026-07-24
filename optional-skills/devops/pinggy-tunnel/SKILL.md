---
name: pinggy-tunnel
description: Zero-install localhost tunnels over SSH via Pinggy.
version: 0.1.0
author: Teknium (teknium1), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Pinggy, Tunnel, Networking, SSH, Webhook, Localhost]
    related_skills: [cloudflared-quick-tunnel, webhook-subscriptions]
---

# Pinggy Tunnel Skill

Expose a local service (dev server, webhook receiver, MCP endpoint, demo) to the public internet using a Pinggy SSH reverse tunnel. No daemon to install — the user's stock SSH client connects to `a.pinggy.io:443` and Pinggy hands back a public HTTP/HTTPS URL.

Free tier: 60-minute tunnels, random subdomain, no signup. Pro tier ($3/mo) is an opt-in with a token.

## When to Use

- User asks to "expose this locally", "share my dev server", "make this URL public", "tunnel port N", "get a public URL for a webhook"
- Need to receive a webhook callback during a local task (Stripe, GitHub, Discord, AgentMail)
- Sharing a one-off HTTP demo (MCP server, Ollama/vLLM endpoint, dashboard) with a remote party
- The host has SSH but no `cloudflared` / `ngrok` binary, and installing one would be overkill

If the host already has `cloudflared` configured, prefer the `cloudflared-quick-tunnel` skill — Cloudflare quick tunnels don't expire after 60 minutes.

## Prerequisites

- `ssh` on PATH (`ssh -V`). Default on Linux, macOS, and Windows 10+. No other install.
- A local service listening on `127.0.0.1:<port>` before the tunnel starts. Pinggy will return URLs but they'll 502 until the local origin is up.

Optional:

- `PINGGY_TOKEN` env var for paid Pro features (persistent subdomain, custom domain, multiple tunnels, no 60-minute cap). Free tier needs no credentials.

## Routing table — read the reference you need

| Intent | Do this |
|---|---|
| Need the exact ssh invocation for HTTP / TCP / TLS / basic-auth / bearer-token / IP-whitelist / CORS / Pro tunnels; the full username-keyword table; the `localhost:4300` web debugger | read `references/cli-cookbook.md` |
| Run the full 5-step flow: confirm origin, launch background tunnel, parse the URL from the log, verify, tear down | read `references/procedure.md` |
| Composite patterns: receive a webhook callback, expose an MCP server over HTTP/SSE, expose a local LLM endpoint (Ollama/vLLM/llama.cpp), share a dev server with a one-shot password | read `references/recipes.md` |

## Red lines

- **Bind the origin to `127.0.0.1`**, and never tunnel anything sensitive without an access-control flag (`b:`, `k:`, or `w:`). A bare HTTP tunnel is reachable by anyone with the URL.
- **Always quote the whole `user@host` argument** when it contains a `+`.
- **Always pass `-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null`** for unattended runs, and always redirect stdout to a logfile and `grep` the file — `process(action='log')` may miss the SSH banner where the URLs are printed.
- **Never bookmark or hard-code a free-tier URL.** It is random, changes on every restart, and dies at the 60-minute mark.

## Shortest end-to-end skeleton

```bash
# End-to-end: spin up a trivial origin, tunnel it, hit it, tear down
python3 -m http.server 18000 --bind 127.0.0.1 >/tmp/origin.log 2>&1 &
ORIGIN_PID=$!

nohup ssh -p 443 \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -R0:localhost:18000 free@a.pinggy.io >/tmp/pinggy-verify.log 2>&1 &
SSH_PID=$!

sleep 5
URL=$(grep -oE 'https://[a-z0-9-]+\.[a-z]+\.pinggy\.link' /tmp/pinggy-verify.log | head -1)
echo "URL: $URL"
curl -sI "$URL/" | head -1

kill "$SSH_PID" "$ORIGIN_PID"
```

Expected: a `pinggy.link` URL and `HTTP/2 200` on the curl head.

## Pitfalls

- **60-minute hard cap on the free tier.** The SSH session terminates at the 60-minute mark; the URL goes dead. For longer shares, either use `PINGGY_TOKEN` (Pro) or auto-restart with a shell loop (note that the URL changes on every restart for free-tier).
- **Free-tier URL is random and changes on restart.** Don't bookmark it, don't paste it into a config file. Re-parse from the log each time.
- **Concurrent free tunnels are limited to one per source IP.** Starting a second tunnel from the same machine usually kills the first. Pro tier lifts this.
- **`+` in usernames must be quoted.** Bare `ssh ... b:admin:secret+free@a.pinggy.io` works in bash but breaks under shells that treat `+` specially or when assembled programmatically. Always wrap in double quotes.
- **Don't tunnel anything sensitive without an access-control flag.** A bare HTTP tunnel is reachable by anyone with the URL. Use `b:`, `k:`, or `w:` for non-public services.
- **`process(action='log')` may miss SSH banner output.** Pinggy prints the URLs and then the SSH session goes interactive. Always redirect to a logfile and `grep` the file directly — same pattern as `cloudflared-quick-tunnel`.
- **Host-key prompt on first run.** Default OpenSSH config asks the user to accept Pinggy's host key. Always pass `-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null` for unattended runs.
- **TCP and TLS tunnels return a `<subdomain>.a.pinggy.online:<port>` pair, not an https URL.** Parse with a different regex (`tcp://` and a port). Don't assume every Pinggy tunnel is HTTP.
- **Pro mode requires the token as the username, not a flag.** Use `"$PINGGY_TOKEN+a.pinggy.io"` (no `free@`). With a token you can also add `:persistent` for a stable subdomain — see `pinggy.io/docs/`.
