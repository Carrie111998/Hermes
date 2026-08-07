#!/usr/bin/env bash
# tolaria-mcp-bridge — stdio ↔ WebSocket shim for Tolaria's MCP server.
#
# Reads MCP JSON-RPC frames from stdin, forwards them to Tolaria's WS
# endpoint (default ws://127.0.0.1:9710, tunneled from the Mac via
# autossh -R), and writes responses back to stdout.
#
# Registered in Hermes' .mcp.json as:
#   "tolaria": { "type": "stdio", "command": "~/.local/bin/tolaria-mcp-bridge" }

set -euo pipefail

URL="${TOLARIA_MCP_URL:-ws://127.0.0.1:9710}"

if ! command -v websocat >/dev/null 2>&1; then
  echo "tolaria-mcp-bridge: websocat not found on PATH" >&2
  exit 127
fi

# --no-close keeps the connection open across MCP request/response frames.
# --ping-interval keeps NAT/idle timeouts from killing the Tailscale hop.
exec websocat --no-close --ping-interval 20 - "$URL"
