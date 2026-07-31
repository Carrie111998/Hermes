# Kanban Engineering Guide

Root [`AGENTS.md`](../../AGENTS.md) and plugin policy
[`plugins/AGENTS.md`](../AGENTS.md) still apply.

Kanban is a durable SQLite-backed work queue, not an in-process delegation
shortcut.

## Invariants

- The board is the hard isolation boundary. Worker processes receive a pinned
  `HERMES_KANBAN_BOARD` and must not cross it.
- Tenant is only a namespace within a board; it is not a security boundary.
- The dispatcher atomically claims work, reclaims stale claims, and promotes
  ready tasks.
- Repeated non-success attempts hit `kanban.failure_limit` and block rather
  than spin forever.
- Dispatcher-spawned workers get the task lifecycle tools. Wider board-routing
  tools remain limited to explicitly configured orchestrators.
- A worker must finish through the Kanban protocol; process exit alone does not
  silently complete a running task.

Dashboard assets and standalone service files stay within this plugin. Generic
capabilities needed by other plugins belong in the shared plugin surface, not a
Kanban-specific core branch.

User and worker contracts:

- [`website/docs/user-guide/features/kanban.md`](../../website/docs/user-guide/features/kanban.md)
- [`website/docs/user-guide/features/kanban-worker-lanes.md`](../../website/docs/user-guide/features/kanban-worker-lanes.md)
