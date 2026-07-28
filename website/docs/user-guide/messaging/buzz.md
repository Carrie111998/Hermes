---
title: "Buzz"
description: "Connect the full Hermes gateway to a Buzz workspace through authenticated Nostr WebSockets"
---

# Buzz

The Buzz platform plugin connects the full Hermes gateway to a
[Buzz](https://github.com/block/buzz) workspace. Buzz is the messaging
transport. Hermes keeps its normal profile, memory, tools, approvals, sessions,
and runtime host.

Inbound events use a persistent authenticated Nostr WebSocket. Outbound
messages use the Buzz CLI so scheduled and one-shot delivery do not depend on a
temporary WebSocket connection.

## Prerequisites

- A Buzz relay that the Hermes host can reach over HTTPS and WebSockets.
- The `buzz` CLI on the Hermes host's `PATH`, or an executable path in
  `BUZZ_CLI`.
- A dedicated Buzz identity that is already authorized for the workspace.
- The identity's Nostr private key in the Hermes secret store or
  `~/.hermes/.env`.
- Channel membership for each shared channel Hermes should monitor.

Do not configure the same Buzz identity as both a Buzz-managed ACP agent and an
external Hermes gateway. Two runtimes using one identity can produce duplicate
replies. Use a different keypair for each independently running Hermes gateway.

## Configure Hermes

The required settings are:

```dotenv title="~/.hermes/.env"
BUZZ_RELAY_URL=https://buzz.example.com
BUZZ_PRIVATE_KEY=nsec1...
```

Keep `BUZZ_PRIVATE_KEY` out of `config.yaml`, shell history, logs, and source
control.

You can place non-secret settings in `config.yaml`:

```yaml title="~/.hermes/config.yaml"
gateway:
  platforms:
    buzz:
      enabled: true
      extra:
        relay_url: https://buzz.example.com
        channels:
          - your-shared-channel-id
        home_channel:
          chat_id: your-shared-channel-id
          name: Buzz
        discover_dms: true
        require_mention: true
        wake_words:
          - Hermes
          - Maximus
        allowed_users:
          - your-owner-pubkey
        allow_all_users: false
```

Environment variables override values bridged from `config.yaml`.

| Variable | Required | Description |
|----------|:--------:|-------------|
| `BUZZ_RELAY_URL` | yes | Buzz relay HTTP(S) or WebSocket URL |
| `BUZZ_PRIVATE_KEY` | yes | Dedicated Hermes identity as `nsec` or 64-character hex |
| `BUZZ_AUTH_TAG` | no | NIP-OA owner attestation required by some relays |
| `BUZZ_CHANNELS` | no | Comma-separated shared-channel IDs |
| `BUZZ_DM_CHANNELS` | no | Comma-separated DM IDs to subscribe to explicitly |
| `BUZZ_DISCOVER_DMS` | no | Discover relay-confirmed DMs automatically; default `true` |
| `BUZZ_HOME_CHANNEL` | no | Default channel for cron and proactive delivery |
| `BUZZ_ALLOWED_USERS` | recommended | Comma-separated sender pubkeys allowed to direct Hermes |
| `BUZZ_ALLOW_ALL_USERS` | no | Permit all workspace members; default `false` |
| `BUZZ_REQUIRE_MENTION` | no | Require addressing in shared channels; default `true` |
| `BUZZ_WAKE_WORDS` | no | Comma-separated display names accepted as typed mentions |
| `BUZZ_PROFILE_NAME` | no | Profile name to publish; unset preserves the existing name |
| `BUZZ_PROFILE_ABOUT` | no | Profile description to publish |
| `BUZZ_CLI` | no | Executable Buzz CLI path or command name |

Set profile fields only when Hermes should intentionally change the public Buzz
profile. Leaving them unset prevents an existing identity such as Maximus from
being renamed during startup.

## Start and verify

```bash
hermes gateway start
hermes gateway status
```

Verify each behavior before relying on the connection:

1. Send a DM and confirm exactly one Hermes reply.
2. In a shared channel, send `@Maximus /status` using the configured wake word.
3. Restart the gateway and repeat the DM.
4. Confirm the first message did not replay after reconnection.
5. If approvals are enabled, verify an approval or clarification response
   returns to the waiting session.

Direct messages do not require a mention. Shared channels require a Nostr
`p` tag, a reply to a Hermes message, or a configured typed wake word when
`BUZZ_REQUIRE_MENTION=true`.

## Remote Hermes gateways

The plugin runs on the Hermes gateway host, not on the device that sends the
Buzz message. A remote Hermes instance can therefore stay available while a
laptop is off:

```text
Buzz phone or desktop -> Buzz relay -> remote Hermes gateway
```

The remote host needs the Buzz CLI, its own protected Buzz identity, outbound
relay access, and a continuously running Hermes gateway service.

## Capabilities and limits

| Capability | Support |
|------------|---------|
| Text and Markdown | yes |
| Direct messages | yes |
| Shared channels | yes |
| Explicit threads | yes |
| Mention gating | yes |
| Approvals and clarification text | yes |
| Cron and standalone delivery | yes |
| Attachments | not yet |
| Reactions | not yet |
| Typing indicators | not yet |

The adapter keeps bounded event-ID deduplication and per-channel resume
timestamps. It reconnects with exponential backoff and restores subscriptions
after a transient WebSocket failure.
