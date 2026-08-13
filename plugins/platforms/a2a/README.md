# A2A — Agent-to-Agent protocol for Hermes

Talk to other agents, and let other agents talk to you, over the open
[A2A protocol](https://a2a-protocol.org) **v1.0**. Works with any A2A-compliant
peer (another Hermes, LangChain, CrewAI, Google ADK, OpenClaw, …). Stdlib only —
no `a2a-sdk` dependency.

## Enable

```bash
hermes gateway setup      # pick A2A, or:
```

```yaml
# ~/.hermes/config.yaml
gateway:
  platforms:
    a2a:
      enabled: true
      extra:
        port: 9900

# peers you want to call (outbound):
a2a_agents:
  researcher:
    url: "http://localhost:9999"
    auth: { type: bearer, token: "<peer-token>" }
    timeout: 120
    capabilities: [web_search, research]
```

## Outbound — call other agents

The agent gets five tools:

- `a2a_discover(url)` — what can this agent do?
- `a2a_call(agent, message, context_id?)` — send it a task, get the reply.
- `a2a_list()` — configured peers, saved conversations, metrics.
- `a2a_history(context_id)` — recall a saved A2A conversation.
- `a2a_orchestrate(capability, message, mode?)` — fan-out a task to every
  peer advertising a capability (`all` / `first` / `best`).

## Inbound — be callable

When the `a2a` platform is enabled, Hermes serves a v1.0 Agent Card at
`http://<host>:<port>/.well-known/agent-card.json` (the legacy
`/.well-known/agent.json` path is also answered for pre-1.0 clients) and
accepts JSON-RPC `SendMessage`, `SendStreamingMessage`, `GetTask`, `ListTasks`,
`CancelTask`, `SubscribeToTask`, and push-notification config CRUD. The
pre-1.0 path aliases (`message/send`, `message/stream`, `tasks/get`, and so
on) remain accepted. Incoming tasks are injected into your **live** agent
session — the same agent that's talking to you, with full memory — and the
reply is returned over A2A. Completed tasks stay queryable via `GetTask` or
`tasks/get`.

For a v1.0 `SendMessage` request, the JSON-RPC `result` contains a `task` (or
`message`) wrapper. Legacy path aliases return the payload without that wrapper.
The JSON-RPC request id is echoed independently from the server-generated task
id. `contextId` belongs inside `params.message`; repeat it for a multi-turn
conversation. If an Agent Card interface advertises a `tenant`, echo that
value in `params.tenant`.

## Security

- **No token ⇒ localhost only.** The server binds `127.0.0.1` and refuses to
  widen unless you configure a token *and* set `A2A_HOST`.
- **Per-peer tokens**: `A2A_PEER_TOKENS="peer-a:<token-a>,peer-b:<token-b>"` gives each
  remote agent its own credential; that authenticated name (never anything
  in the request body) drives rate limiting, trust, and audit.
- Inbound text — including `/`-prefixed text — is run through
  prompt-injection filters and framed as untrusted peer input; remote peers
  cannot invoke operator slash commands.
- Outbound text is scrubbed of known credential-shaped strings; arbitrary
  secrets are not guaranteed to be removed.
- Push callbacks are checked for unsafe internal destinations and HMAC-SHA256
  signed (`X-A2A-Signature`) when a push secret is configured.
- Exchanges are written best-effort to `~/.hermes/a2a_audit.jsonl`; protect the
  bounded summaries in that file as sensitive local data.
- Conversations persist to `~/.hermes/a2a_conversations/` — they survive context
  compaction and restarts (`a2a_history` recalls them).

Streaming uses `SendStreamingMessage`/`message/stream` and emits SSE
`statusUpdate` or `artifactUpdate` members. v1.0 frames are JSON-RPC-wrapped;
stream closure, rather than a `final` field or a `done` data event, signals
completion. `tasks/cancel` is a soft cancel: it drops the A2A reply but cannot
abort a live agent turn.

## Env vars

| Var | Default | Meaning |
|---|---|---|
| `A2A_PEER_TOKENS` | _(unset)_ | Per-peer credentials `name:<token>,…` (preferred). |
| `A2A_BEARER_TOKEN` | _(unset)_ | Shared token; identity falls back to caller IP. |
| `A2A_HOST` | `127.0.0.1` | Bind host. Only widens with a token set. |
| `A2A_PORT` | `9900` | Inbound port. |
| `A2A_AGENT_NAME` | hostname-derived | Name on the Agent Card. |
| `A2A_PUBLIC_URL` | _(unset)_ | Routable URL advertised on the card (reverse proxies). |
| `A2A_TRUSTED_PEERS` | _(unset)_ | Allow-list of authenticated identities. |
| `A2A_ALLOW_ALL_USERS` | `false` | Allow any authed peer (dev only). |
| `A2A_RATE_LIMIT` | `60` | Requests/minute per identity. |
| `A2A_MAX_PINGPONG_TURNS` | `5` | Anti-loop turn cap per context (max 20). |
| `A2A_REPLY_TIMEOUT` | `300` | Seconds to wait for the agent's reply. |
| `A2A_PUSH_SECRET` | bearer credential | HMAC secret for push signing; falls back to the shared bearer credential. |
| `A2A_ADVERTISED_TOOLSETS` | all registered | Restrict skills on the Agent Card. |

See `DESIGN.md` for architecture and the requirement-tracing table.
