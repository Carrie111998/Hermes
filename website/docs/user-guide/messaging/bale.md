---
sidebar_position: 2
title: "Bale (بله)"
description: "Connect Hermes Agent to Bale with Persian gateway messages"
---

# Bale Setup

Hermes connects to [Bale](https://bale.ai/) through the messaging gateway. The
bundled Bale plugin reuses Hermes' Telegram-compatible transport with Bale's
documented Bot API endpoints, but keeps Bale credentials, authorization, chat
delivery, and session identity separate.

Hermes replies in Persian by default on Bale unless the user asks for another
language. Setting `display.language: fa` also translates Hermes' static
approval and gateway messages into Persian.

## Step 1: Create a Bale bot

1. Open [Bale BotFather](https://ble.ir/botfather).
2. Create a bot and copy its token.
3. Keep the token secret. Anyone who has it can operate the bot.

The adapter uses Bale's documented API roots:

- Bot API: `https://tapi.bale.ai/bot<token>/METHOD_NAME`
- Files: `https://tapi.bale.ai/file/bot<token>/<file_path>`

## Step 2: Configure credentials and access

Add the token and an allowlist to `~/.hermes/.env`:

```bash
BALE_BOT_TOKEN=YOUR_BALE_BOT_TOKEN
BALE_ALLOWED_USERS=123456789,987654321
```

`BALE_ALLOWED_USERS` is strongly recommended because Hermes can use tools on
the host. For a temporary development bot, `BALE_ALLOW_ALL_USERS=true` disables
the allowlist.

## Step 3: Enable Bale and Persian

Add this to `~/.hermes/config.yaml`:

```yaml
display:
  language: fa

gateway:
  platforms:
    bale:
      enabled: true
      disable_link_previews: true
```

The two Persian behaviors are related but distinct:

- The Bale platform hint makes normal agent answers Persian by default. A user
  can still request another language in the conversation.
- `display.language: fa` translates Hermes-owned static messages, including
  approval prompts and supported gateway replies. It does not translate logs,
  tool output, errors, or arbitrary model responses.

The display language is global, so other Hermes CLI and gateway surfaces in the
same process also use Persian static messages.

## Step 4: Start the gateway

Run in the foreground while testing:

```bash
hermes gateway
```

For an installed gateway service:

```bash
hermes gateway start
hermes gateway status
```

Send the bot a Bale message and confirm that it answers in Persian.

## Cron and notification delivery

Set a default Bale chat for scheduled text delivery:

```bash
BALE_HOME_CHANNEL=123456789
BALE_HOME_CHANNEL_NAME=گزارش‌ها
```

Cron jobs can then use `deliver: bale`. Standalone Bale delivery currently
supports text only and returns a clear error instead of dropping media
attachments.

## Compatibility boundary

The live adapter uses Bale's Telegram-compatible polling, message, edit, and
file APIs. Hermes deliberately does not call Telegram-only extensions that
Bale does not document:

- Telegram command-menu registration
- message reactions
- rich messages and rich drafts
- Telegram private-chat topics and forum setup

The first release is intended for long polling. It does not expose Bale webhook
configuration. Use a real Bale bot token for the final deployment smoke test;
the repository's automated tests exercise the adapter without contacting Bale.

## Environment variables

| Variable | Required | Description |
|---|---:|---|
| `BALE_BOT_TOKEN` | yes | Bale Bot API token |
| `BALE_ALLOWED_USERS` | recommended | Comma-separated Bale user IDs allowed to use Hermes |
| `BALE_ALLOW_ALL_USERS` | development only | Allow every Bale user |
| `BALE_HOME_CHANNEL` | no | Default chat ID for cron and notification delivery |
| `BALE_HOME_CHANNEL_NAME` | no | Display name for the default chat |

## Troubleshooting

**Bale is available but not connected.** Check that `BALE_BOT_TOKEN` is in the
environment of the gateway process, not only in your interactive shell.

**The bot receives messages but does not answer.** Confirm the sender's numeric
ID is present in `BALE_ALLOWED_USERS`, then inspect `hermes gateway status` and
the gateway log.

**Static messages are still English.** Confirm `display.language: fa` is in
`~/.hermes/config.yaml` and restart the gateway. `HERMES_LANGUAGE` overrides the
config value when it is set.
