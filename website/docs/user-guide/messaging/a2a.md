# A2A (Agent-to-Agent)

[A2A](https://a2a-protocol.org) is an open protocol for communication between
independent agents. The Hermes A2A plugin works in both directions: your agent
can call other A2A agents as tools, and other agents can send tasks to Hermes
over HTTP.

Use A2A for cross-process, cross-machine, or cross-framework communication. For
multiple agents on the same machine, prefer [delegation](../features/delegation.md)
or the [kanban board](../features/kanban.md).

## Enable

Run the setup flow:

```bash
hermes gateway setup      # choose A2A
```

Or enable the inbound platform in `~/.hermes/config.yaml`:

```yaml
gateway:
  platforms:
    a2a:
      enabled: true
      extra:
        port: 9900
```

The outbound client tools belong to the `a2a` toolset, which is off by default.
Enable that toolset with `hermes tools` when the agent should call peers.

## Outbound: call another agent

With the `a2a` toolset enabled, Hermes provides:

| Tool | Purpose |
|---|---|
| `a2a_discover(url)` | Fetch and summarize a peer's Agent Card. |
| `a2a_call(agent, message, context_id?)` | Send a task and return the peer's reply. Reuse `context_id` for another turn. |
| `a2a_list()` | List configured peers, saved conversations, and metrics. |
| `a2a_history(context_id)` | Recall a persisted A2A conversation. |
| `a2a_orchestrate(capability, message, mode?)` | Fan a task out to configured peers advertising a capability. Modes are `all`, `first`, and `best`. |

Configure named peers in `config.yaml`. The token is a placeholder; keep the
real credential in the supported secret configuration and out of source files:

```yaml
a2a_agents:
  researcher:
    url: "http://localhost:9999"
    auth:
      type: bearer
      token: "<peer-token>"
    timeout: 120
    capabilities: [web_search, research]
```

The `agent` argument can also be a direct `http(s)` URL. Before sending a task,
the client prefers the JSON-RPC URL from the peer's `supportedInterfaces[]`
entry and falls back to the card's legacy top-level `url`.

## Caller protocol

### Discover the Agent Card

The canonical v1.0 discovery path is:

```text
GET <A2A_BASE_URL>/.well-known/agent-card.json
```

Hermes also answers the legacy `/.well-known/agent.json` path. Select the
`supportedInterfaces[]` entry whose `protocolBinding` is `JSONRPC`; its `url`
is the endpoint for JSON-RPC requests. If a peer has no `supportedInterfaces`
array, use its top-level `url` as a compatibility fallback.

The card's `capabilities` reports whether streaming and push notifications are
available. `skills` is a live view of the advertised toolsets. By default it is
derived from the tool registry; `A2A_ADVERTISED_TOOLSETS` or
`extra.advertised_toolsets` can restrict what is advertised.

### Send a task

The v1.0 method is `SendMessage`. The legacy `message/send` alias is also
accepted. Both use a JSON-RPC 2.0 envelope:

```json
{
  "jsonrpc": "2.0",
  "id": "request-1",
  "method": "SendMessage",
  "params": {
    "message": {
      "role": "ROLE_USER",
      "parts": [
        {"text": "Summarize the supplied report.", "mediaType": "text/plain"}
      ],
      "messageId": "message-1",
      "contextId": "context-1"
    }
  }
}
```

`A2A-Version: 1.0` is the preferred request header. `messageId` and
`contextId` are generated when omitted. To continue a conversation, repeat the
same `contextId` inside each `params.message`. If the selected Agent Card
interface advertises a `tenant`, echo it as `params.tenant`.

Message parts are discriminated by member presence, not by a `kind` field:

- Text parts contain `text` and `mediaType`.
- File parts contain `url` or base64 `raw`, plus optional `filename` and
  `mediaType`.
- Data parts contain structured `data` and `mediaType`.

Inbound file parts are rendered into the agent's text input with their filename
and URL (or a base64 size note); data parts are rendered as JSON. Raw file bytes
are not decoded by the plugin. Outbound replies are text-only. Older peers'
text parts and the older nested file shape are tolerated.

The JSON-RPC request `id` is echoed independently from the server-generated
task id. For a v1.0 `SendMessage` request, `result` contains a `task` or
`message` member:

```json
{
  "jsonrpc": "2.0",
  "id": "request-1",
  "result": {
    "task": {
      "id": "task-<generated>",
      "contextId": "context-1",
      "status": {
        "state": "TASK_STATE_COMPLETED",
        "timestamp": "<utc-millisecond-timestamp>"
      },
      "artifacts": [
        {"parts": [{"text": "The report is summarized here.", "mediaType": "text/plain"}]}
      ]
    }
  }
}
```

Legacy path aliases return the task or message without the v1.0 wrapper. A
caller should check artifacts first, then `status.message`, and tolerate a bare
message result from older peers. A v1.0 Task has a status timestamp; it does
not include `createdAt` or `lastModified`. Push-configuration objects do include
`createdAt`.

### Task states

| State | Meaning |
|---|---|
| `TASK_STATE_SUBMITTED` | Initial state, most visible at the start of a stream. |
| `TASK_STATE_WORKING` | The agent is processing the task. |
| `TASK_STATE_INPUT_REQUIRED` | The agent needs clarification; read `status.message` and answer with the same `contextId`. |
| `TASK_STATE_COMPLETED` | The task completed; read its artifacts. |
| `TASK_STATE_FAILED` | Processing failed or timed out; read `status.message` when present. |
| `TASK_STATE_CANCELED` | The task was canceled. |
| `TASK_STATE_REJECTED` | The request was rejected, for example by the anti-loop limit. |

### Streaming and task queries

The v1.0 streaming method is `SendStreamingMessage`; the legacy alias is
`message/stream`. Reconnecting to an existing task uses `SubscribeToTask` or
`tasks/subscribe`. Other v1.0 methods and their legacy path aliases are:

| v1.0 method | Legacy alias | Purpose |
|---|---|---|
| `GetTask` | `tasks/get` | Fetch a task. |
| `ListTasks` | `tasks/list` | List tasks with optional context/state filters and pagination. |
| `CancelTask` | `tasks/cancel` | Soft-cancel a task and reset its context's anti-loop counter. |
| `SubscribeToTask` | `tasks/subscribe` | Reconnect to a running task's stream. |
| `CreateTaskPushNotificationConfig` | `tasks/pushNotificationConfig/create` | Register one callback for a task. |
| `GetTaskPushNotificationConfig` | `tasks/pushNotificationConfig/get` | Fetch the task's callback configuration. |
| `ListTaskPushNotificationConfigs` | `tasks/pushNotificationConfig/list` | List the task's callback configuration. |
| `DeleteTaskPushNotificationConfig` | `tasks/pushNotificationConfig/delete` | Remove the task's callback configuration. |

Streaming responses are SSE `data:` frames. Each v1.0 frame is a JSON-RPC
response whose `result` is a `StreamResponse` containing a member such as
`task`, `statusUpdate`, or `artifactUpdate`. There is no `final` field or named
`done` data event: the stream closes to signal the terminal state. Keepalive
comments and the final `: done` comment are not data frames.

Completed tasks remain queryable in the in-memory task store, bounded to the
most recent 500 terminal tasks. `CancelTask` cannot interrupt a live agent turn;
it prevents the late reply from being returned to the canceled A2A task.

### Push notifications

Register a callback inline on `SendMessage` or with
`CreateTaskPushNotificationConfig`:

```json
{
  "configuration": {
    "taskPushNotificationConfig": {
      "url": "<callback-url>"
    }
  }
}
```

The legacy create path is `tasks/pushNotificationConfig/create`; get, list, and
delete paths are also supported. Hermes keeps at most one push configuration per
task. A created configuration returns `configId` and `createdAt`.

On a terminal transition, Hermes posts a `statusUpdate` StreamResponse. When a
push secret is configured, the body is signed with HMAC-SHA256 in the
`X-A2A-Signature` header. The signature covers the JSON body serialized with
sorted keys; verify it before trusting the callback. The secret is
`A2A_PUSH_SECRET`, falling back to the shared inbound bearer credential. If
neither a dedicated push secret nor a shared bearer credential is configured,
notifications are unsigned. In localhost-only mode, loopback callbacks are
allowed for local testing; remote mode blocks obvious internal destinations.

Callback URLs are limited to `http` and `https`. In remote mode, obvious
loopback, link-local, private, reserved, and unspecified IP destinations are
blocked. Localhost-only mode permits loopback callbacks for local testing but
still blocks other private ranges.

## Security model

Hermes keeps the inbound surface local by default:

- With neither `A2A_BEARER_TOKEN` nor `A2A_PEER_TOKENS`, the server binds to
  `127.0.0.1` and the card does not require authentication.
- A token alone does not widen the bind. Remote exposure requires a token and
  an explicit non-loopback `A2A_HOST`.
- `A2A_PEER_TOKENS` maps each presented bearer credential to a named peer. A
  shared `A2A_BEARER_TOKEN` maps the authenticated identity to the caller IP.
  Nothing in the request body can assert or replace that identity, and token
  comparisons are constant-time.
- `A2A_TRUSTED_PEERS` optionally restricts authenticated identities. Without an
  allow-list, any authenticated peer is accepted; `A2A_ALLOW_ALL_USERS` can
  explicitly override the allow-list for development.
- Inbound text is filtered for common prompt-injection and role-boundary
  markers, then framed as untrusted peer input. Remote peers cannot invoke
  operator slash commands.
- Outbound text is scrubbed for known credential-shaped patterns. Treat this as
  a safety filter, not as a guarantee that arbitrary secrets are safe to send.
- Exchanges are written best-effort to `<HERMES_HOME>/a2a_audit.jsonl`. Audit
  records contain a bounded message summary, so protect the file as sensitive
  local data; outbound redaction does not make audit storage a secret store.

Conversations are persisted outside the context-compaction pipeline under
`<HERMES_HOME>/a2a_conversations/`. The local `a2a_history` tool can recall
them after compaction or restart. The default inbound route uses the live
gateway session, including its memory and context; deployments that configure
additional served-agent routes may forward a task to another local profile.

Each context has an anti-loop limit controlled by
`A2A_MAX_PINGPONG_TURNS` (default 5, hard maximum 20). Once the limit is
exceeded, a task is rejected; canceling a task resets that context's counter.

## Configuration reference

These inbound settings are read by the A2A plugin:

| Variable | Default | Meaning |
|---|---|---|
| `A2A_PEER_TOKENS` | _(unset)_ | Per-peer credentials in `name:<token>,…` form. |
| `A2A_BEARER_TOKEN` | _(unset)_ | Shared inbound credential; identity falls back to caller IP. |
| `A2A_HOST` | `127.0.0.1` | Bind host; a non-loopback value is honored only when a token is set. |
| `A2A_PORT` | `9900` | Inbound HTTP port. |
| `A2A_AGENT_NAME` | hostname-derived | Name shown on the Agent Card. |
| `A2A_PUBLIC_URL` | _(unset)_ | Public URL advertised on the card behind a proxy. Forwarded host/proto headers are used when this is unset. |
| `A2A_TRUSTED_PEERS` | _(unset)_ | Comma-separated authenticated identities allowed to run tasks. |
| `A2A_ALLOW_ALL_USERS` | `false` | Allow authenticated peers even when an allow-list is configured; development use only. |
| `A2A_RATE_LIMIT` | `60` | Requests per minute per authenticated identity. |
| `A2A_MAX_PINGPONG_TURNS` | `5` | Per-context anti-loop limit, capped at 20. |
| `A2A_REPLY_TIMEOUT` | `300` | Seconds to wait for an agent reply. |
| `A2A_PUSH_SECRET` | shared bearer credential | Dedicated HMAC secret for push signing. |
| `A2A_ADVERTISED_TOOLSETS` | all registered | Restrict skills shown on the Agent Card. |

## Safe local check

The following shows the request shape without a live endpoint or credential:

```bash
BASE_URL="${A2A_BASE_URL}"  # set this only in your own environment

curl -s "${BASE_URL}/.well-known/agent-card.json"

curl -s -X POST "${BASE_URL}/" \
  -H 'Content-Type: application/json' \
  -H 'A2A-Version: 1.0' \
  -d '{"jsonrpc":"2.0","id":"request-1","method":"SendMessage",
       "params":{"message":{"role":"ROLE_USER",
       "parts":[{"text":"What capabilities do you advertise?"}]}}}'
```

If the card advertises a security scheme, add the bearer header through your
secret manager or other private credential mechanism. Do not put a live
credential in a shell history, document, issue, or repository.

## Troubleshooting

| Symptom | Likely cause and next step |
|---|---|
| The card URL is unreachable or advertises the wrong host | The server is stopped, the bind host is local-only, or a proxy is rewriting the public address. Set `A2A_PUBLIC_URL`, or configure the forwarded host/proto headers at the proxy. |
| `401` or JSON-RPC `-32050` | The remote server requires a bearer credential and the request did not present the matching one. Use the peer's configured auth mechanism without exposing the value. |
| `403` or JSON-RPC `-32052` | Authentication succeeded, but the identity is not in `A2A_TRUSTED_PEERS`. |
| HTTP `429` or JSON-RPC `-32051` | The authenticated identity exceeded `A2A_RATE_LIMIT`; slow down or use a separate approved peer identity. |
| `TASK_STATE_REJECTED` | The context reached the anti-loop limit. Start a new context or cancel the old task before continuing. |
| `TASK_STATE_INPUT_REQUIRED` | This is a clarification request, not a failure. Read `status.message` and send the answer with the same `contextId`. |
| A remote bind is forced back to localhost | A token is missing, or `A2A_HOST` is still a loopback value. Configure both deliberately before exposing the service. |
| A long task times out | Raise `A2A_REPLY_TIMEOUT`, use streaming, or register a push callback and query the task later. Remember that canceling a task is soft. |
| A reply does not contain text that was sent | Known credential-shaped content may have been removed by outbound redaction. Never use A2A as a secret transport. |
| `/help`-style text has no operator effect | Remote input is intentionally framed and filtered; peers cannot invoke operator slash commands. |
