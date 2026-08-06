# TUI Gateway Engineering Guide

Read [`../AGENTS.md`](../AGENTS.md) first; Hermes does not automatically merge
parent and child context files. This backend serves multiple clients. Before
changing a protocol, event, command, or session behavior, read the scoped guide
for every affected client:

- `../ui-tui/AGENTS.md` for the Ink TUI and stdio JSON-RPC contract.
- `../web/AGENTS.md` for the dashboard's PTY-embedded TUI.
- `../apps/desktop/AGENTS.md` for the Electron client's JSON-RPC integration.

Do not optimize one client by silently breaking another.
