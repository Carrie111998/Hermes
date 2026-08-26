# Hermes Desktop Debug MCP

Native UI-debugging tools for LLM agents working on `apps/desktop`. Wraps the
perf-harness CDP client (`scripts/perf/lib/cdp.mjs`) so agents inspect and drive
the live renderer without hand-rolling `eval.mjs` one-liners each session.

Proposal thread: NousResearch/hermes-agent#95489.

## Tools

Read-only (always available):

| Tool | What it does |
|---|---|
| `desktop_ui_status` | Is the CDP port alive? Which targets/selectors exist? Call first. |
| `ui_inspect` | One element: tag, classes, box, visibility, computed styles, inherited-rule hint. |
| `ui_query` | Up to 20 matching elements with bounded text snippets. |
| `ui_console` | Renderer console ring captured while connected. |
| `ui_screenshot` | PNG capture to a path (default `/tmp/desktop-debug-mcp/`). |

Mutating (require `DESKTOP_DEBUG_MCP_ALLOW_ACT=1` in the server env):

| Tool | What it does |
|---|---|
| `ui_click` / `ui_type` / `ui_press` | Real CDP Input events — blur/focus semantics match a human (this matters: synthetic DOM events skip the blur→cancel race the edit composer is known for). |
| `ui_eval` | Bounded JS eval escape hatch. |
| `ui_flow_edit` | Scripted edit flow: open edit on last user message → type → Enter → structured report (send accepted? composer stuck? timeline changed?). Reproduction harness for the chat-edit silent-fail races. |
| `ui_flow_model_switch` | Installs a MutationObserver over the thread to quantify model-switch row jank. |

## Running

```bash
cd apps/desktop/mcp
npm install
node server.mjs                       # stdio MCP server, port 9222 by default
```

Register with Hermes:

```bash
hermes mcp add desktop-debug \
  --command node \
  --args <abs-path>/apps/desktop/mcp/server.mjs
# mutating tools:
hermes mcp add desktop-debug --command node \
  --args <abs-path>/apps/desktop/mcp/server.mjs \
  --env DESKTOP_DEBUG_MCP_ALLOW_ACT=1
```

Flags/env: `--port N` / `DESKTOP_DEBUG_MCP_PORT`, `--match STR` (target URL filter,
default `5174`), `DESKTOP_DEBUG_MCP_ALLOW_ACT=1`.

## The port problem (read this first)

The CDP port exists **only for dev-server runs**; packaged builds never open it.
If `desktop_ui_status` reports `cdpAlive: false`:

1. Ask the user to start the dev server (`cd apps/desktop && npm run dev`), or
2. Launch an isolated probe instance (does not touch the user's app):

```bash
cd apps/desktop
HERMES_HOME=/tmp/cdp-probe-home \
HERMES_DESKTOP_DEV_SERVER=http://127.0.0.1:5174 \
HERMES_DESKTOP_CDP_PORT=9333 \
  npx electron . --user-data-dir=/tmp/cdp-probe-userdata
# then: node mcp/server.mjs --port 9333
```

**Never relaunch or kill the user's running app** to get a port.

## Safety rails

- Outputs are bounded (≤20 nodes, ≤80-char snippets, ≤4KB eval) — never dump full DOM.
- Mutating tools are opt-in via env; flows refuse to run without it.
- One shared connection; friendly errors instead of raw discovery dumps.
- Real input events only — no synthetic `dispatchEvent` shortcuts.
