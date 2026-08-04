# TUI Engineering Guide

Root [`AGENTS.md`](../AGENTS.md) still applies. This file owns the Ink TUI and
its Python `tui_gateway` boundary.

## Process boundary

`hermes --tui` runs Ink/TypeScript over newline-delimited stdio JSON-RPC to the
Python gateway. TypeScript owns rendering and interaction; Python owns agent
sessions, tools, model calls, and command logic.

Keep request/event contracts explicit. Built-in client commands remain local;
other slash commands flow through `slash.exec` and the existing dispatch
fallback.

## Dashboard

The dashboard `/chat` surface embeds the real TUI through the PTY bridge. Do not
rebuild the transcript, composer, or terminal in React. Add primary chat
behavior to Ink so both surfaces receive it.

Supporting React views such as inspectors and sidebars are allowed when they do
not become a second chat implementation. Their failures must not take down the
PTY pane or own its session state.

## Development checks

From `ui-tui/`, use the package scripts for build, typecheck, lint, formatting,
and Vitest. Test the JSON-RPC boundary when a change crosses TypeScript and
Python; a component-only snapshot is not transport proof.

Desktop is a separate chat surface with its own policy in
[`apps/desktop/AGENTS.md`](../apps/desktop/AGENTS.md).
