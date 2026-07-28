---
sidebar_position: 9
title: "Fluxer"
description: "Run Hermes Agent as a native Fluxer bot"
---

# Fluxer Setup

Hermes connects directly to Fluxer's REST API and real-time Gateway. The adapter does not route through Discord or require `discord.py`; Fluxer is a separate messaging platform with its own bot token and endpoints.

## Supported behavior

| Context or feature | Behavior |
|---|---|
| Direct messages | Hermes responds without a mention. |
| Server channels | Hermes requires a bot mention by default. |
| Text and replies | Incoming reply context is preserved; outgoing responses can reply to the triggering message. |
| Images and files | Incoming attachments are downloaded with SSRF and size protections; outgoing files use Fluxer's multipart API. |
| Typing and streaming | Native typing indicators and progressive edits are supported. |
| Connection recovery | Gateway sessions heartbeat, reconnect with backoff, and resume when Fluxer permits it. |
| Proactive delivery | Cron jobs and notifications can use a configured home channel. |

Fluxer voice-channel participation and message reactions are not currently implemented. Audio files and voice-message attachments are supported as ordinary media.

## 1. Create a Fluxer bot

1. Open Fluxer **User Settings** and select **Applications**.
2. Create an application, add a bot user, and copy its bot token.
3. Add the bot to each server where Hermes should operate.
4. Copy your Fluxer user ID and any channel IDs you want to allow or use for proactive delivery.

:::warning
Treat the bot token as a password. Do not post it in chat, put it in `config.yaml`, or commit it to source control. Each simultaneously running Hermes profile must use a different Fluxer bot token.
:::

## 2. Configure Hermes

Run the guided gateway setup:

```bash
hermes gateway setup
```

Select **Fluxer**, enter the bot token locally, and optionally enter an allowed-user list and home channel. The setup writes secrets to the active profile's `.env` file.

You can also configure the active profile manually:

```bash
FLUXER_BOT_TOKEN=***
FLUXER_ALLOWED_USERS=123456789012345678
FLUXER_HOME_CHANNEL=234567890123456789
```

Multiple allowed users are comma-separated. `FLUXER_ALLOW_ALL_USERS=true` permits any sender and is intended only for controlled development environments.

## 3. Start and verify the gateway

```bash
hermes gateway restart
hermes gateway status
```

Send the bot a DM. In a server channel, mention the bot in the message unless that channel is configured for free response.

## Channel controls

```bash
# Bot responds only in these server channels; DMs are unaffected.
FLUXER_ALLOWED_CHANNELS=234567890123456789,345678901234567890

# These channels do not require a bot mention.
FLUXER_FREE_RESPONSE_CHANNELS=234567890123456789

# Disable mention gating in every allowed server channel.
FLUXER_REQUIRE_MENTION=false
```

The bot still applies the normal Hermes user authorization policy through `FLUXER_ALLOWED_USERS` and `FLUXER_ALLOW_ALL_USERS`.

## Self-hosted or proxied Fluxer

The production defaults are:

```bash
FLUXER_API_URL=https://api.fluxer.app/v1
```

Fluxer's `/gateway/bot` endpoint supplies the Gateway URL automatically. Self-hosted deployments can override either endpoint:

```bash
FLUXER_API_URL=https://chat.example.com/api/v1
FLUXER_GATEWAY_URL=wss://gateway.chat.example.com/
FLUXER_MAX_UPLOAD_BYTES=26214400
```

Hermes requires HTTPS/WSS for remote endpoints so the bot token cannot cross a
plaintext connection. Plain HTTP/WS is accepted only for loopback development
endpoints such as `127.0.0.1` or `localhost`.

Use `FLUXER_PROXY` for an adapter-specific HTTP or SOCKS proxy.

## Troubleshooting

### Authentication failed

Regenerate the bot token in Fluxer's Applications settings and rerun `hermes gateway setup`. Fluxer REST requests use `Authorization: Bot <token>`; a normal user session token is not accepted as a bot credential.

### Bot responds in DMs but not a server channel

Verify that the bot belongs to the server, the channel is included in `FLUXER_ALLOWED_CHANNELS` when that variable is set, and the message mentions the bot unless the channel is in `FLUXER_FREE_RESPONSE_CHANNELS`.

### Reconnect loop

Check that HTTPS and WebSocket traffic can reach the configured API and Gateway hosts. If you set `FLUXER_GATEWAY_URL`, it must use `wss://` unless it points to loopback development. The adapter reconnects with exponential backoff and attempts Gateway resume after transient disconnects.

### Two profiles cannot use the same bot

This is intentional. Hermes places a machine-local lock on each Fluxer bot identity to prevent duplicate Gateway consumers. Create a distinct Fluxer application and bot token for every concurrently running profile.
