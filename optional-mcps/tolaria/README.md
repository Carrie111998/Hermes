# Tolaria MCP (optional)

Exposes a [Tolaria](https://tolaria.md) vault on a trusted Mac to Hermes
running on `mfc1` (Hetzner, Tailscale `100.86.92.99`) as an MCP server.

Tolaria's built-in MCP server ships 14 vault tools (`open_note`, `read_note`,
`create_note`, `search_notes`, `append_to_note`, `edit_note_frontmatter`,
`delete_note`, `link_notes`, `list_notes`, `vault_context`, `ui_open_note`,
`ui_open_tab`, `ui_highlight`, `ui_set_filter`) over WebSocket on
`127.0.0.1:9710` (tools) and `9711` (UI broadcast), plus a stdio transport
for local Claude Code / Cursor.

Hermes lives on `mfc1`, so we bridge the Mac's loopback MCP endpoint to
`mfc1` over a Tailscale-scoped SSH reverse tunnel (autossh), then wrap
that TCP endpoint with a tiny stdio→ws shim so it plugs into any MCP
client that speaks stdio (the shape Hermes' `.mcp.json` uses).

## Data flow

```
Mac (MacBook Air M4)                             mfc1 (Hetzner)
+------------------------------+                 +--------------------------+
| Tolaria.app                  |                 | hermes-agent             |
|   MCP server 127.0.0.1:9710  |                 |   .mcp.json -> "tolaria" |
|          ^                   |                 |          |               |
|          | ws (loopback)     |                 |          v stdio         |
|   autossh -R 9710:...    ====== Tailscale ====>|  tolaria-mcp-bridge      |
|                              |   (SSH tunnel)  |          |               |
|                              |                 |          v ws            |
|                              |                 |   127.0.0.1:9710         |
+------------------------------+                 +--------------------------+
```

Nothing is published to the public internet. Access is scoped to your
Tailnet, matching the standing Hermes posture ("private access paths only").

## Install

### On mfc1 (where Hermes runs)

1. Install `websocat` (stdio↔ws bridge):
   ```bash
   sudo apt-get install -y websocat   # or download release binary
   ```

2. Drop the bridge script:
   ```bash
   install -m 0755 tolaria-mcp-bridge.sh ~/.local/bin/tolaria-mcp-bridge
   ```

3. Add the `tolaria` block from `.mcp.json.example` to your live `.mcp.json`.

4. Start Tolaria in **Vault Safe** mode on the Mac first. Watch a few
   Hermes calls before switching to Power User.

### On the Mac (MacBook Air M4)

See [`docs/tolaria-mcp.md`](../../docs/tolaria-mcp.md) in the repo for the
autossh LaunchAgent, SSH config, and Tolaria LaunchAgent.

## Permission mode

Tolaria's AI panel exposes two modes per vault:

- **Vault Safe** — file / search / edit tools only. Start here.
- **Power User** — adds shell access scoped to the active vault. Only
  flip once you've verified Hermes' dispatch loop doesn't misfire against
  the vault.

## Failure semantics

If the Mac sleeps or the Tailscale link drops, `tolaria-mcp-bridge` exits
non-zero on connect failure. Hermes should mark the tool as degraded, not
crash the dispatch cycle — same reliability posture as the DAN-2146
fallback chain work in `dotfiles`.
