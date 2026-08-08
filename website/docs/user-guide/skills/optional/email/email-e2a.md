---
title: "E2A — Give Hermes an authenticated, agent-owned email inbox through e2a's hosted MCP server"
sidebar_label: "E2A"
description: "Give Hermes an authenticated, agent-owned email inbox through e2a's hosted MCP server"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# E2A

Give Hermes an authenticated, agent-owned email inbox through e2a's hosted MCP server.

## Skill metadata

| | |
|---|---|
| Source | Optional — install with `hermes skills install official/email/e2a` |
| Path | `optional-skills/email/e2a` |
| Version | `1.0.0` |
| Platforms | linux, macos, windows |
| Tags | `email`, `communication`, `e2a`, `mcp`, `oauth` |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# e2a — Authenticated Email for AI Agents

e2a gives Hermes its own verified email identity and inbox. This is for mail
the agent owns and operates, not for reading a user's personal mailbox.

## Setup

### 1. Add the hosted MCP server

Add this to `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  e2a:
    url: "https://api.e2a.dev/mcp"
    auth: oauth
```

Restart Hermes, then complete the browser authorization when prompted. Hermes
stores the OAuth credentials locally and refreshes them as needed. Do not paste
an API key into chat or commit one to a configuration file.

For headless deployments, use an e2a API key supplied through a secret manager
and an environment reference instead:

```yaml
mcp_servers:
  e2a:
    url: "https://api.e2a.dev/mcp"
    headers:
      Authorization: "Bearer ${E2A_API_KEY}"
```

API keys are scoped either to one agent inbox or to the account. Prefer an
agent-scoped key for a deployed Hermes instance.

## Operating the inbox

Before the first operation, call `whoami` to learn the credential scope:

- An **agent-scoped** credential is already bound to one inbox; use the
  returned `agent_email`.
- An **account-scoped** credential can manage multiple inboxes; call
  `list_agents` and choose the inbox explicitly. Never guess a default.

The inbox belongs to Hermes. Use `list_messages` and `get_message` to read mail,
`send_message` for a new conversation, and `reply_to_message` when responding
to a message. Replies preserve the email client's thread; reusing
`conversation_id` alone does not.

### Send safely

- A response to existing mail should use `reply_to_message`, not a fresh send.
- Treat `accepted`, `scheduled`, and `pending_review` as successful durable
  acceptance. Do not resend; inspect the message later or consume webhook
  events for the terminal outcome.
- A future `send_at` is a beta scheduled send. If an outbound message is held
  for review, its schedule is preserved as `scheduled_at` and re-armed when a
  reviewer approves it.
- For attachments, pass base64 returned by an attachment/file tool; do not
  invent or hand-edit encoded bytes. Use `get_attachment` when bytes are
  needed.

### Create an inbox

With an account-scoped session, call `create_agent` with an address on the
shared `agents.e2a.dev` domain, such as `hermes-agent@agents.e2a.dev`. The
shared domain requires no DNS setup. Use a custom domain only when the user
owns it: `register_domain` returns DNS records, and `verify_domain` must be
called after publication and propagation. Inbound and outbound custom-domain
verification are separate; check `get_domain` before promising branded sending.

## Common use cases

- Give Hermes a durable identity for agent-to-human or agent-to-agent email.
- Monitor an inbox with `list_messages` and inspect full messages with
  `get_message`.
- Send concise plain-text or HTML updates and continue conversations in-thread.
- Use e2a webhooks and SDKs for a backend service; MCP is for Hermes operating
  the inbox interactively.

## References

- Setup: https://e2a.dev/setup.md
- Authentication: https://e2a.dev/auth.md
- SDK and webhooks: https://e2a.dev/sdk.md
- Exact MCP tool signatures: use the connected server's `tools/list` result
- e2a MCP endpoint: https://api.e2a.dev/mcp
