# Email Draft Approval (desktop runtime plugin)

Review and approve pending outbound-email drafts from the desktop app.

While the backend email platform runs in `draft_only` mode, every outbound
email is queued as a `pending` draft instead of being sent over SMTP. This
plugin adds an **Email Drafts** pane (right edge of the workspace) that lists
those drafts and lets you **Approve** (send now) or **Deny** (reject) each one.

## Install

Copy `plugin.js` into one of the runtime plugin doors the desktop app loads:

- `<hermes home>/desktop-plugins/email-draft-approval/plugin.js`
- `<hermes home>/plugins/email-draft-approval/desktop/plugin.js`

Restart the desktop app (or reload the plugin from Settings ▸ Plugins). The
plugin is plain ESM with `@hermes/plugin-sdk` imports — the runtime loader
(`apps/desktop/src/contrib/runtime-loader.ts`) rewrites those to live shims at
load time, so no rebuild is needed.

## Requirements

- Backend P1 stack: `gateway/outbound_drafts.py` +
  `tui_gateway/methods_email_drafts.py` (this branch).
- `platforms.email.extra.draft_only: true` in the agent config, so outbound
  email lands in the draft store.
- RPC identity: `email.drafts.*` is owner-only. The desktop gateway socket
  authenticates as the owner profile; calls from other identities are rejected
  with an auth error (4403).

## Gateway RPC surface used

| Method | Purpose |
|---|---|
| `email.drafts.list` | List pending/denied drafts |
| `email.drafts.approve` | Approve and send a draft (one-shot) |
| `email.drafts.deny` | Reject a draft |
| `email.drafts.cancel` | Cancel drafts for a stopped generation |

The pane degrades gracefully: missing RPC or auth errors render a message in
the pane instead of crashing the app.

## Notes

- The plugin is UI-only; it does not hold mail credentials. Delivery is done by
  the backend's one-shot SMTP path.
- Do not ship this plugin to remote/untrusted sources: the runtime loader is
  error-isolation only, not a security boundary (see `runtime-loader.ts`).
