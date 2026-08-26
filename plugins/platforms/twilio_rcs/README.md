# Twilio RCS platform plugin

Outbound-only Hermes platform plugin that sends messages through a Twilio
**Messaging Service** (`MessagingServiceSid`). Twilio automatically selects
RCS for capable recipients and falls back to SMS/MMS otherwise — the send
call looks identical either way.

There is **no inbound channel**: no webhook, no polling, no `hermes gateway`
listener. This plugin only participates in *outbound* delivery — `hermes
send`, cron `deliver=twilio_rcs`, and (if explicitly asked) an agent's own
`terminal` tool shelling out to `hermes send`.

## For AI agents reading this file

If a user asks you (the agent) to "send an RCS/text message to +1555...",
there is **no agent-callable tool for this** — `send_message` exists as a
tool schema in `tools/send_message_tool.py` but is intentionally never
registered into any toolset (outbound platform messaging is deliberately
kept outside the model's own initiative; see `toolsets.py` around
`_HERMES_CORE_TOOLS`). The only way to actually deliver a message from a
chat session is to use your `terminal` tool to run the CLI command
yourself:

```bash
hermes send --to "twilio_rcs:+15551234567" "your message text"
```

For rich content (a template created via `scripts/manage_content.py`),
run the same command with a `CONTENT:` directive as the message body —
see "Rich content" below. Do not fabricate a raw JSON card payload and
try to pass it as the message — Twilio's Messages API does not accept
inline rich-content JSON; it only accepts a `ContentSid` referencing a
template created ahead of time through the Content API.

## Setup

Env vars (`TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` are shared with the
built-in `sms` platform and the optional `telephony` skill — set once, used
by both):

| Env var | Required | Notes |
|---|---|---|
| `TWILIO_ACCOUNT_SID` | yes | Starts with `AC` |
| `TWILIO_AUTH_TOKEN` | yes | |
| `TWILIO_MESSAGING_SERVICE_SID` | yes | Starts with `MG` — must have an RCS Sender (approved by Google) attached in the Twilio Console |
| `TWILIO_RCS_HOME_CHANNEL` | no | Destination E.164 number for cron `deliver=twilio_rcs` jobs |

Add them to `~/.hermes/.env`, then verify with `hermes status` (shows
`Twilio RCS  ✓ configured (plugin)`).

## Sending plain text

```bash
hermes send --to "twilio_rcs:+15551234567" "hello from Hermes"
```

Targets are bare E.164 numbers (`+` followed by 7–15 digits) — this
platform declares its own `parse_target_ref_fn`/`validate_target_ref_fn`
since it isn't in core's hardcoded phone-platform allowlist
(`tools/send_message_tool._PHONE_PLATFORMS`).

Plain text is markdown-stripped (the Body field renders literal
characters) and chunked at `MAX_RCS_LENGTH` (3072 chars — Twilio's
documented RCS text limit; re-verify against current docs if messages
start getting truncated unexpectedly).

## Rich content (cards, quick-reply chips)

Twilio's Messages API only accepts pre-created **Content API templates**
referenced by `ContentSid` (+ optional `ContentVariables`) for rich
content — there is no inline/ad-hoc JSON parameter for cards or
quick-replies on either RCS or WhatsApp. Verified against Twilio's docs
and confirmed empirically: a freshly created template sent immediately
with no separate approval step.

### 1. Create a template

```bash
# Quick-reply chips
python plugins/platforms/twilio_rcs/scripts/manage_content.py create-quick-reply \
  --friendly-name "order_confirm" \
  --body "Your order shipped! Track it?" \
  --action "Yes:track_yes" \
  --action "No:track_no"

# Rich card (title/subtitle + buttons)
python plugins/platforms/twilio_rcs/scripts/manage_content.py create-card \
  --friendly-name "elite_status" \
  --title "You've reached Elite status!" \
  --subtitle "Reply STOP to unsubscribe" \
  --action "url:Shop now:https://example.com" \
  --action "phone:Call us:+15551234567"
```

Both print the resulting `ContentSid` (`HX...`) and a ready-to-paste
`hermes send` command. `list` and `get <content_sid>` are also available
to inspect existing templates.

The script uses Python stdlib HTTP only (no `aiohttp`/`requests`
dependency) and reads `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN` from
`~/.hermes/.env` (or the environment) — it can run standalone, outside
the Hermes venv.

### 2. Send it

```bash
hermes send --to "twilio_rcs:+15551234567" "CONTENT:HXxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

# with template variables ({{1}}, {{2}}, ... in the template body)
hermes send --to "twilio_rcs:+15551234567" 'CONTENT:HXxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx:{"1":"Alice"}'
```

The `CONTENT:<sid>[:<json>]` directive is recognized in both `send()`
(live gateway) and `_standalone_send()` (out-of-process CLI/cron), and
mirrors the existing `MEDIA:<path>` convention used elsewhere in Hermes
cross-platform messaging. Malformed JSON after the SID raises a clear
error instead of silently sending garbage.

### Known gaps

Only `twilio/quick-reply` and `twilio/card` (without a media/image field)
are implemented — `twilio/carousel` and a card `media` field exist per
Twilio's content-type list but their exact JSON schema wasn't verified
against current docs, so they were left out rather than guessed at. Add
them once confirmed (check `https://www.twilio.com/docs/content` for the
current schema) rather than assuming the shape shown for `twilio/card`
generalizes.

## Architecture notes

- `TwilioRcsAdapter.connect()`/`disconnect()` are no-ops (`_mark_connected`/
  `_mark_disconnected` only) — there's nothing to actually connect to.
- `_standalone_send()` is the primary delivery path in practice, since
  `hermes send` and cron jobs usually run in a separate process from any
  live gateway.
- `register(ctx)` in `adapter.py` wires everything into
  `gateway.platform_registry` — no core Hermes files were touched to add
  this plugin (see `website/docs/developer-guide/adding-platform-adapters.md`).

## Files

```
twilio_rcs/
  __init__.py           # re-exports register() for plugin discovery
  plugin.yaml            # kind: platform, env var declarations
  adapter.py             # TwilioRcsAdapter, _standalone_send, register(ctx)
  scripts/
    manage_content.py    # Content API template create/list/get helper
```
