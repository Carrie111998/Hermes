---
sidebar_position: 30
title: "Chatto"
description: "Set up Hermes Agent as a Chatto bot"
---

# Chatto Setup

Hermes Agent integrates with [Chatto](https://github.com/chattocorp/chatto), a self-hosted team chat server. The adapter connects via the Chatto WebSocket realtime protocol (binary protobuf) for inbound message delivery and the ConnectRPC JSON API for outbound messages. It processes messages through the Hermes Agent pipeline (including tool use, memory, and reasoning) and responds in real time. It supports text, markdown, threads, reactions, file attachments, typing indicators, and more.

No external Python packages are required beyond `websockets`, which is bundled with Hermes. The adapter includes a pure-stdlib protobuf codec — no `protobuf` library needed.

Before setup, here's the part most people want to know: how Hermes behaves once it's in your Chatto instance.

## How Hermes Behaves

| Context | Behavior |
|---------|----------|
| **DMs** | Hermes responds to every message. No `@mention` needed. Each DM has its own session. |
| **Rooms** | Hermes responds when you `@mention` it. Without a mention, Hermes ignores the message (configurable via `CHATTO_REQUIRE_MENTION`). |
| **Threads** | Hermes supports thread/reply chains. If you reply in a thread, Hermes keeps the thread context isolated from the parent room. The bot auto-follows threads it participates in. |
| **Processing indicators** | Hermes adds a 👀 reaction when it starts processing a message, and replaces it with ✅ on success or ❌ on failure. |
| **Typing indicators** | Hermes broadcasts persistent typing indicators while it's working, so users know the bot is active. |
| **Message batching** | Long responses (>10000 chars) are automatically split into multiple messages. |

:::tip
If you want Hermes to respond to all messages in a room without requiring an `@mention`, set `CHATTO_REQUIRE_MENTION=false`. DMs always get a response regardless of this setting.
:::

## Capability Matrix

| Capability | Chatto |
|------------|--------|
| text | yes |
| markdown | yes |
| threads | yes |
| reactions | yes |
| message editing | yes |
| message deletion | yes |
| typing indicators | yes |
| processing notifications | yes (👀/✅/❌) |
| file attachments | yes (chunked upload) |
| DM initiation | yes |
| room creation | yes |
| member directory | yes (cached) |
| presence broadcasting | yes (online/away) |
| custom status | yes |
| read state management | yes |
| auto-reconnect | yes |

## Prerequisites

1. **A running Chatto server** — self-hosted and accessible from the Hermes host. See the [Chatto repository](https://github.com/chattocorp/chatto) for installation instructions.
2. **A Chatto user account** — the adapter logs in with a username and password. Create a dedicated account for the bot (e.g., `hermes`).
3. **Room membership** — the bot account must be a member of any room where you want it to respond. The adapter auto-joins rooms specified in `CHATTO_CHANNELS`. For DMs, simply start a direct message with the bot.
4. **Network access** — the Hermes host must reach the Chatto server URL over HTTPS (or HTTP) and establish a WebSocket connection to `/api/realtime`.

:::info
The adapter uses WebSocket protocol v1 (compatible with Chatto v0.4.19+). Ensure your Chatto server is up to date.
:::

## Installation

The Chatto adapter is a platform plugin. It lives at:

```
~/.hermes/plugins/platforms/chatto/
```

### Option A: Plugin Install (Recommended)

If the plugin is available in a plugin registry:

```bash
hermes plugins install chatto-platform
```

### Option B: Manual Installation

Copy the plugin files to the correct location:

```bash
mkdir -p ~/.hermes/plugins/platforms/chatto
cp -r chatto-plugin/* ~/.hermes/plugins/platforms/chatto/
```

The plugin directory should contain:

```
~/.hermes/plugins/platforms/chatto/
├── __init__.py
├── adapter.py          # Main adapter (WebSocket + ConnectRPC)
├── plugin.yaml         # Plugin manifest (env var definitions)
└── test_adapter.py     # Unit tests
```

Hermes auto-discovers platform plugins on gateway startup. No manual registration is needed.

## Configuration

### Option A: Interactive Setup (Recommended)

Run the guided setup command:

```bash
hermes gateway setup
```

Select **Chatto** when prompted, then provide your server URL, login, and password when asked.

### Option B: Manual Configuration

Add the following to your `~/.hermes/.env` file:

```bash
# Required
CHATTO_URL=https://chat.example.com
CHATTO_LOGIN=hermes
CHATTO_PASSWORD=your-password

# Optional: restrict to specific rooms (comma-separated room IDs)
# CHATTO_CHANNELS=REljMv5Pgolo6Y9,abc123def456

# Optional: home channel for cron/notification delivery
# CHATTO_HOME_CHANNEL=REljMv5Pgolo6Y9

# Optional: restrict who can talk to the bot (comma-separated logins)
# CHATTO_ALLOWED_USERS=alice,bob

# Optional: allow any user to talk to the bot (default: false)
# CHATTO_ALLOW_ALL_USERS=true

# Optional: require @mention in rooms (default: true). DMs always respond.
# CHATTO_REQUIRE_MENTION=true
```

Or configure via `~/.hermes/config.yaml`:

```yaml
gateway:
  platforms:
    chatto:
      enabled: true
      extra:
        url: https://chat.example.com
        channels:                  # room IDs to watch (empty = all joined)
          - REljMv5Pgolo6Y9
        home_channel: REljMv5Pgolo6Y9
        require_mention: true      # only respond to @mentions in rooms
        allowed_users: []          # empty = allow all (or set allow_all_users)
        allow_all_users: true
```

:::note
Environment variables override `config.yaml` values. Secrets (`CHATTO_PASSWORD`) should always go in `~/.hermes/.env`, not in `config.yaml`.
:::

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `CHATTO_URL` | Yes | — | Base URL of the Chatto server (e.g., `https://chat.example.com`) |
| `CHATTO_LOGIN` | Yes | — | Chatto username (login) |
| `CHATTO_PASSWORD` | Yes | — | Chatto password |
| `CHATTO_CHANNELS` | No | All joined rooms | Comma-separated room IDs to watch |
| `CHATTO_HOME_CHANNEL` | No | First watched room | Room ID for cron/notification delivery |
| `CHATTO_ALLOWED_USERS` | No | _(deny all)_ | Comma-separated Chatto logins allowed to talk to the agent |
| `CHATTO_ALLOW_ALL_USERS` | No | `false` | Allow any Chatto user to talk to the agent (`true`/`false`) |
| `CHATTO_REQUIRE_MENTION` | No | `true` | Only respond to `@mentions` in rooms. DMs always get a response. |

### Start the Gateway

Once configured, start the gateway:

```bash
hermes gateway
```

The bot should connect to your Chatto server within a few seconds. Send it a message — either a DM or `@mention` it in a room — to test.

:::tip
You can run `hermes gateway` in the background or as a systemd service for persistent operation. See the deployment docs for details.
:::

## Features

### WebSocket Realtime Protocol

The adapter maintains a persistent WebSocket connection to `/api/realtime` on the Chatto server using the binary protobuf realtime protocol (protocol v1). Inbound events — including room messages, mention notifications, and DM notifications — are decoded from protobuf frames in real time.

Key protocol details:

- **Client hello** — on connect, the adapter sends a `RealtimeClientHello` with protocol version 1 and a bearer token for authentication.
- **Subscribe events** — after hello, the adapter subscribes to room timeline events for all watched rooms. A resume cursor is used to avoid replaying old messages after reconnection.
- **Heartbeat** — the adapter sends ping frames every 30 seconds and expects pong responses. Missed heartbeats trigger a reconnect.
- **Auto-reconnect** — if the WebSocket drops, the adapter reconnects with exponential backoff (1s → 30s max) and resumes from the last cursor.

### ConnectRPC JSON API (Outbound)

All outbound actions — sending messages, reactions, typing indicators, file uploads, etc. — use the Chatto ConnectRPC JSON API over HTTP POST. The adapter handles bearer token authentication and automatic re-login on 401 responses.

### Markdown Support

Hermes sends messages with full Markdown formatting. Chatto renders Markdown in its web UI, so code blocks, lists, bold/italic, links, and inline code all display correctly.

### Thread / Reply Support

When you reply to a message in Chatto (creating a thread), Hermes responds within that thread. Thread context stays isolated from the parent room — each thread has its own session namespace. The bot also auto-follows threads it participates in, so it continues to receive new messages in that thread.

### Reactions

Hermes uses emoji reactions for processing lifecycle notifications:

- 👀 — added when the agent starts processing your message
- ✅ — replaces 👀 when processing completes successfully
- ❌ — replaces 👀 when processing fails

The adapter converts Unicode emoji to Chatto shortcode names internally (e.g., `👀` → `eyes`, `✅` → `white_check_mark`). Reactions can also be added/removed programmatically by the agent.

### Message Editing and Deletion

The adapter supports editing existing messages (via `UpdateMessage`) and deleting them (via `DeleteMessage`). This is used for updating processing indicators and can be used by the agent to correct or retract messages.

### Typing Indicators

The adapter broadcasts persistent typing indicators to rooms while the agent is working. This uses the `UpdateTypingIndicator` RPC endpoint and runs as a background task per room, refreshing the indicator until the response is sent.

### Message Batching and Splitting

- **Splitting**: Messages longer than 10000 characters are automatically split into multiple sequential messages (split threshold: 9900 chars to leave room for separators).
- **Batching**: Rapid successive outbound messages to the same room are merged into a single message with a 0.6-second batch delay, reducing noise in busy rooms.

### Chunked File / Attachment Upload

File attachments are uploaded via the Chatto chunked asset upload API:

1. `CreateUpload` — initiates an upload session
2. `UploadChunk` — uploads the file in 256 KB chunks
3. `CompleteUpload` — finalizes the upload and attaches it to the message

This supports files of any size, streamed in chunks to avoid memory issues.

### Liveness Probe and Auto-Reconnect

A background liveness probe checks the connection health every 60 seconds. After 3 consecutive failures, the adapter triggers a full reconnect — re-login, room discovery, and WebSocket reconnection with resume cursor support.

### Read State Management

After processing messages in a room, the adapter marks the room as read via `MarkRoomAsRead`. Thread read state is also managed via `MarkThreadAsRead`, so the bot's unread indicators stay accurate.

### DM Initiation

The adapter can proactively start direct messages with other Chatto users via the `StartDM` RPC endpoint. This is used for cron job delivery and notifications when no home channel is configured.

### Room Creation

The adapter can create new rooms via `CreateRoom`. This is available to the agent for organizing conversations or creating dedicated channels for tasks.

### Notification Dismissal

After processing mentions and DMs, the adapter dismisses the corresponding Chatto notifications via `DismissNotification` / `DismissAllNotifications`, keeping the bot's notification queue clean.

### Member Directory

The adapter maintains a cached member directory using `ListUsers`, `GetUser`, and `BatchGetUsers` endpoints. User lookups (for mention resolution, display names, etc.) are cached per user ID to reduce API calls.

### Presence Broadcasting

The adapter can broadcast presence status (online, away, do-not-disturb) via `UpdatePresence`. On startup, the bot sets itself to online. Status can be changed programmatically.

| Presence Status | API Value |
|-----------------|-----------|
| Online | 1 |
| Away | 2 |
| Do Not Disturb | 3 |

### Custom Status Messages

The adapter supports setting and clearing custom status messages via `UpdateCustomStatus` / `DeleteCustomStatus`. This can be used to show "Processing…" or other contextual status indicators in the Chatto UI.

## Usage Notes

### Mention Detection

In rooms with `CHATTO_REQUIRE_MENTION=true` (default), Hermes only responds when the message contains an `@mention` of the bot's login name. The mention is automatically stripped from the message before processing. In DMs, every message gets a response — no mention required.

### Allowed Users

By default, if neither `CHATTO_ALLOWED_USERS` nor `CHATTO_ALLOW_ALL_USERS` is set, the bot denies all users as a safety measure. Configure one of:

- `CHATTO_ALLOWED_USERS=alice,bob` — only these Chatto logins can interact with the bot
- `CHATTO_ALLOW_ALL_USERS=true` — any Chatto user can interact

:::warning
Setting `CHATTO_ALLOW_ALL_USERS=true` means any user on your Chatto server has full access to the agent's capabilities, including tool use and system access. Use this only on trusted, private Chatto instances.
:::

### Home Channel

The home channel is where the bot sends proactive messages — cron job output, reminders, and notifications. Set it via:

- `CHATTO_HOME_CHANNEL` env var (room ID)
- `config.yaml` → `gateway.platforms.chatto.extra.home_channel`
- `/sethome` slash command in any room where the bot is present

If unset, the first watched room is used as the default home channel.

### Thread Auto-Following

When Hermes responds in a thread, it automatically follows that thread. This means it continues to receive all new messages in the thread, even if not explicitly `@mentioned` — similar to how a human user would follow a conversation they've joined.

## Troubleshooting

### Bot is not responding to messages

**Cause**: The bot account is not a member of the room, or the user is not in `CHATTO_ALLOWED_USERS`.

**Fix**: Verify the bot is a member of the room (the adapter auto-joins rooms listed in `CHATTO_CHANNELS`). Check that your Chatto login is in `CHATTO_ALLOWED_USERS`, or set `CHATTO_ALLOW_ALL_USERS=true`. Restart the gateway.

### Connection refused / WebSocket fails to connect

**Cause**: The Chatto server is unreachable, or the URL is incorrect.

**Fix**: Verify `CHATTO_URL` points to your Chatto server (include `https://`, no trailing slash). Test connectivity:

```bash
curl -s -o /dev/null -w "%{http_code}" https://chat.example.com/api/connect/chatto.api.v1.ViewerService/GetViewer
```

If you use a reverse proxy (nginx, Apache), ensure WebSocket upgrade headers are configured for `/api/realtime`:

```nginx
location /api/realtime {
    proxy_pass http://chatto-backend;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 600s;
}
```

### Authentication failed

**Cause**: The `CHATTO_LOGIN` or `CHATTO_PASSWORD` is incorrect.

**Fix**: Verify credentials by testing the login endpoint directly:

```bash
curl -X POST https://chat.example.com/auth/login \
  -H "Content-Type: application/json" \
  -d '{"login":"hermes","password":"your-password"}'
```

If the response does not include a `token` field, the credentials are wrong. Reset the bot account password in Chatto and update `~/.hermes/.env`.

### Protocol version mismatch

**Cause**: The Chatto server is running an older version that doesn't support realtime protocol v1.

**Fix**: The adapter uses protocol v1, supported by Chatto v0.4.19 and later. Upgrade your Chatto server:

```bash
# Check your Chatto server version
curl -s https://chat.example.com/api/version | jq .
```

If you see a `RealtimeError` with a protocol-related message in the logs, upgrading the server is the fix.

### WebSocket disconnects / reconnection loops

**Cause**: Network instability, Chatto server restarts, or firewall/proxy issues with WebSocket connections.

**Fix**: The adapter automatically reconnects with exponential backoff (1s → 30s max) and resumes from the last cursor. Check:

1. Your server's WebSocket configuration — reverse proxies need upgrade headers (see above).
2. No firewall is blocking WebSocket connections on your Chatto server.
3. The Chatto server is running and healthy.

Check gateway logs for details:

```bash
grep -i "chatto\|websocket\|realtime" ~/.hermes/logs/gateway.log | tail -30
```

### "No rooms to watch" on startup

**Cause**: `CHATTO_CHANNELS` is not set and the bot account hasn't joined any rooms.

**Fix**: Either join rooms with the bot account first, or set `CHATTO_CHANNELS` to specific room IDs. The adapter will auto-join rooms listed in `CHATTO_CHANNELS`.

### 401 Unauthorized during operation

**Cause**: The bearer token expired during a session.

**Fix**: The adapter automatically re-logs in on 401 responses. If this fails repeatedly, check that the bot account hasn't been deactivated and the password hasn't changed. If the password was changed, update `CHATTO_PASSWORD` in `~/.hermes/.env` and restart the gateway.

### Bot is offline

**Cause**: The Hermes gateway isn't running, or it failed to connect.

**Fix**: Check that `hermes gateway` is running. Look at the terminal output or gateway logs for error messages. Common issues: wrong URL, expired credentials, Chatto server unreachable. Run `hermes status` to check component health.

## Security

:::warning
Always set `CHATTO_ALLOWED_USERS` to restrict who can interact with the bot. Without it (and without `CHATTO_ALLOW_ALL_USERS=true`), the gateway denies all users by default as a safety measure. Only add logins of people you trust — authorized users have full access to the agent's capabilities, including tool use and system access.
:::

For more information on securing your Hermes Agent deployment, see the [Security Guide](../security.md).

## Notes

- **Self-hosted friendly**: Works with any self-hosted Chatto instance. No cloud account or subscription required.
- **No extra dependencies**: The adapter uses `websockets` (bundled with Hermes) and a pure-stdlib protobuf codec. No `protobuf` library or other external packages needed.
- **Pure stdlib protobuf**: The adapter implements just enough of the protobuf binary format to encode/decode Chatto realtime frames — no protoc compilation step, no generated code.
- **Resume cursor support**: On reconnect, the adapter resumes from the last event cursor, so no messages are lost or replayed during transient disconnects.