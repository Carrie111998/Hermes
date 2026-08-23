# Wiring Tolaria (Mac) to Hermes (mfc1)

This runbook wires the Mac-side [Tolaria](https://tolaria.md) vault into
Hermes on `mfc1` over Tailscale. Nothing here is exposed to the public
internet.

**Vault:** `~/obsidian-vault` (existing Obsidian vault — Tolaria reads
the same `.md` + YAML frontmatter format, no conversion needed).

**mfc1:** Hetzner box, Tailscale IP `100.86.92.99`, running `nexus-gateway`
and the Hermes agent.

## One-time setup on the Mac

### 1. Install Tolaria & helpers

```bash
brew install --cask tolaria       # already installed if you followed the previous turn
brew install autossh websocat     # autossh for the tunnel, websocat is optional here
```

### 2. Bring Tailscale up

Tolaria's MCP server binds `127.0.0.1` only, so the tunnel *must* originate
from the Mac. Tailscale needs to be running for `ssh mfc1` to resolve.

```bash
open -a Tailscale             # bring the daemon up
# Verify:
/Applications/Tailscale.app/Contents/MacOS/Tailscale status | head
```

### 3. Add mfc1 to `~/.ssh/config`

```sshconfig
Host mfc1
  HostName 100.86.92.99
  User root                    # TODO adjust if your mfc1 user differs
  IdentityFile ~/.ssh/id_ed25519
  ServerAliveInterval 30
  ServerAliveCountMax 3
  # Reverse tunnel: Mac's Tolaria MCP -> mfc1:9710
  RemoteForward 9710 127.0.0.1:9710
  ExitOnForwardFailure yes
```

Test once by hand: `ssh mfc1 'ss -tlnp | grep 9710'` should show the
forwarded listener on mfc1's loopback once Tolaria is running on the Mac.

### 4. Persistent tunnel (autossh LaunchAgent)

Install `~/Library/LaunchAgents/com.dizhaky.hermes.tolaria-tunnel.plist`
(see `mac-files/LaunchAgents/` in this patch). Then:

```bash
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.dizhaky.hermes.tolaria-tunnel.plist
launchctl kickstart -k gui/$UID/com.dizhaky.hermes.tolaria-tunnel
```

### 5. Autostart Tolaria on login (optional but recommended)

The tunnel is useless if Tolaria isn't running. Add Tolaria.app to Login
Items via System Settings → General → Login Items, or install
`com.dizhaky.hermes.tolaria-app.plist` (also in `mac-files/LaunchAgents/`).

### 6. Point Tolaria at the vault and lock permissions

1. Open Tolaria, choose **Open Vault**, point at `~/obsidian-vault`.
2. In the AI panel, set the vault permission mode to **Vault Safe**.
3. Verify the MCP server is listening: `lsof -iTCP:9710 -sTCP:LISTEN`
   should show a `Tolaria` process on `127.0.0.1:9710`.

## One-time setup on mfc1

### 1. Install the stdio→ws bridge

```bash
sudo apt-get install -y websocat
install -m 0755 optional-mcps/tolaria/tolaria-mcp-bridge.sh \
  ~/.local/bin/tolaria-mcp-bridge
```

### 2. Register `tolaria` in `.mcp.json`

Copy the `tolaria` block from `.mcp.json.example` into the live
`.mcp.json` Hermes reads.

### 3. Smoke test

With Tolaria running on the Mac and the tunnel up:

```bash
ssh mfc1 'ss -tlnp | grep 9710'      # should show 127.0.0.1:9710 LISTEN
ssh mfc1 'echo "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}" \
  | ~/.local/bin/tolaria-mcp-bridge | head -c 400'
```

You should see the 14 Tolaria tools listed. Restart Hermes and confirm
`tolaria.*` tools appear in its tool catalog.

## Failure modes to expect

| Symptom | Root cause | Fix |
|---|---|---|
| `tolaria` tools missing after Hermes restart | `.mcp.json` block not picked up | Confirm block in the live file, restart Hermes |
| Bridge exits immediately | Mac asleep / Tailscale down / Tolaria closed | Wake Mac, check `Tailscale status`, ensure Tolaria running |
| `9710` not listening on mfc1 | autossh not running | `launchctl print gui/$UID/com.dizhaky.hermes.tolaria-tunnel` |
| Tools appear but writes fail | Vault Safe mode + tool tried shell | Expected; either keep Safe mode, or flip to Power User |
| Bridge hangs, no responses | NAT killed idle connection | `--ping-interval 20` in bridge should prevent this; check TS logs |

## Why this shape

- **Vault stays on the Mac.** Matches the standing posture ("trusted local
  Mac for sensitive work, small VPS only for gateway uptime").
- **Tailscale only.** No public MCP endpoint, no bearer-token auth to
  manage. ACL scope = your tailnet.
- **stdio wrapper.** Hermes' `.mcp.json` speaks stdio; the bridge is a
  15-line script so there's nothing custom to maintain.
- **Regression-covered.** Restart failures show up the same way the
  DAN-2201 `agentmemory.service` failures do — visible in `journalctl`
  and Slack diagnostics.

## Related tickets / PRs

- Standing rule: Hermes stays on SSH tunnels / Tailscale / VPN
- DAN-2201 — `agentmemory.service` restart-failure lane on mfc1
- DAN-2146 / DAN-2108 / DAN-2109 — fallback chain hardening
- PR #106 / `hermes-infrastructure` #4 — 1Password-backed bootstrap
