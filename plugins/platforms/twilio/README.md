# Twilio platform plugin

Outbound-only Hermes platform plugin for Twilio. Meant to grow into an
umbrella for multiple Twilio channels over time (SMS, MMS, WhatsApp,
Voice, Email) — **currently implements only RCS**, sent through a Twilio
**Messaging Service** (`MessagingServiceSid`). Twilio automatically
selects RCS for capable recipients and falls back to SMS/MMS otherwise —
the send call looks identical either way.

Note: Hermes already ships a separate built-in `sms` platform
(`plugins/platforms/sms/`) that also talks to Twilio. This plugin doesn't
replace it — they're independent, and currently overlap only in that both
read `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN`.

There is **no inbound channel**: no webhook, no polling, no `hermes gateway`
listener. This plugin only participates in *outbound* delivery — `hermes
send`, cron `deliver=twilio`, and (if explicitly asked) an agent's own
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
hermes send --to "twilio:+15551234567" "your message text"
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
| `TWILIO_RCS_HOME_CHANNEL` | no | Destination E.164 number for cron `deliver=twilio` jobs |

Add them to `~/.hermes/.env`, then verify with `hermes status` (shows
`Twilio RCS  ✓ configured (plugin)`).

## Sending plain text

```bash
hermes send --to "twilio:+15551234567" "hello from Hermes"
```

Targets are bare E.164 numbers (`+` followed by 7–15 digits) — this
platform declares its own `parse_target_ref_fn`/`validate_target_ref_fn`
since it isn't in core's hardcoded phone-platform allowlist
(`tools/send_message_tool._PHONE_PLATFORMS`).

Plain text is markdown-stripped (the Body field renders literal
characters) and chunked at `MAX_RCS_LENGTH` (3072 chars — Twilio's
documented RCS text limit; re-verify against current docs if messages
start getting truncated unexpectedly).

## Rich content (cards, carousels)

Twilio's Messages API only accepts pre-created **Content API templates**
referenced by `ContentSid` (+ optional `ContentVariables`) for rich
content — there is no inline/ad-hoc JSON parameter for cards or
carousels on either RCS or WhatsApp. Verified against Twilio's docs and
confirmed empirically: a freshly created template sent immediately with
no separate approval step.

**RCS-supported Content API types, per Twilio's docs:** `twilio/text`,
`twilio/media`, `twilio/card`, `twilio/carousel`. `twilio/quick-reply` is
**not** in that list — `manage_content.py create-quick-reply` still
exists (its schema is verified, it's a real WhatsApp-supported type,
and Content API creation succeeds) but Twilio silently falls back to
plain SMS/MMS on RCS sends rather than rendering reply chips. Use
`create-card` or `create-carousel` for anything that needs to render as
true RCS rich content.

### 1. Create a template

```bash
# Rich card (title/subtitle/media + buttons) — RCS-supported
python plugins/platforms/twilio/scripts/manage_content.py create-card \
  --friendly-name "elite_status" \
  --title "You've reached Elite status!" \
  --subtitle "Reply STOP to unsubscribe" \
  --media "https://example.com/card.jpg" \
  --action "url:Shop now:https://example.com" \
  --action "phone:Call us:+15551234567"

# Carousel (multiple swipeable cards) — RCS-supported
python plugins/platforms/twilio/scripts/manage_content.py create-carousel \
  --friendly-name "product_picks" \
  --body "Check out these options:" \
  --cards-json '[
    {"title":"Option A","body":"First option","media":"https://example.com/a.jpg",
     "actions":[{"type":"QUICK_REPLY","title":"Pick A","id":"pick_a"}]},
    {"title":"Option B","body":"Second option","media":"https://example.com/b.jpg",
     "actions":[{"type":"QUICK_REPLY","title":"Pick B","id":"pick_b"}]}
  ]'

# Quick-reply chips — WhatsApp-verified, NOT confirmed as true RCS rich content (see above)
python plugins/platforms/twilio/scripts/manage_content.py create-quick-reply \
  --friendly-name "order_confirm" \
  --body "Your order shipped! Track it?" \
  --action "Yes:track_yes" \
  --action "No:track_no"
```

All three print the resulting `ContentSid` (`HX...`) and a ready-to-paste
`hermes send` command. `list` and `get <content_sid>` are also available
to inspect existing templates.

**`media` field shape differs by type — confirmed live, not just from
docs:**

| Type | `media` field | Confirmed |
|---|---|---|
| `twilio/card` (top-level) | **array** of URL strings, e.g. `["https://..."]` | live test: bare string 400s, array works |
| `twilio/carousel` (per-card) | **single string** URL | live test + matches Twilio's doc example verbatim |

`create-card --media <url>` and `create-carousel --cards-json '[{"media": "<url>", ...}]'`
already wrap/unwrap this correctly — the asymmetry only matters if you're
calling `create_card()`/`create_carousel()` directly or hand-writing a
payload.

The script uses Python stdlib HTTP only (no `aiohttp`/`requests`
dependency) and reads `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN` from
`~/.hermes/.env` (or the environment) — it can run standalone, outside
the Hermes venv.

### 2. Send it

```bash
hermes send --to "twilio:+15551234567" "CONTENT:HXxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

# with template variables ({{1}}, {{2}}, ... in the template body)
hermes send --to "twilio:+15551234567" 'CONTENT:HXxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx:{"1":"Alice"}'
```

The `CONTENT:<sid>[:<json>]` directive is recognized in both `send()`
(live gateway) and `_standalone_send()` (out-of-process CLI/cron), and
mirrors the existing `MEDIA:<path>` convention used elsewhere in Hermes
cross-platform messaging. Malformed JSON after the SID raises a clear
error instead of silently sending garbage.

### Known gaps

`create-card`/`create-carousel`/`create-quick-reply` cover the fields
verified live (title, subtitle, body, media, URL/PHONE_NUMBER/QUICK_REPLY
actions). Not covered: `webview_size`/`height`/`orientation`/
`thumbnailImageAlignment` on cards (Twilio echoes these back with
defaults — e.g. `height: "TALL"`, `orientation: "VERTICAL"` — but their
valid value sets weren't explored), and RCS-specific delivery receipts /
read status (this plugin is send-only, no inbound webhook to receive
them on).

## Architecture notes

The plugin is split into three layers so that adding a new channel later
never requires touching another channel's code:

- **`adapter.py`** — thin `BasePlatformAdapter` glue only. Handles
  connect/disconnect lifecycle and the `SendResult`/dict shape Hermes
  expects; contains zero channel-specific logic. It holds a single
  `_CHANNEL` instance (`RcsChannel()` today) and delegates every
  channel-specific decision to it.
- **`channels/`** — one file per channel, each implementing the
  `MessagingChannel` interface declared in `channels/base.py`
  (`check_requirements`, `connect_requirements_ok`, `is_connected`,
  `parse_target_ref`, `validate_target_ref`, `format_message`,
  `build_send_requests`). `channels/rcs.py` is fully self-contained — the
  `CONTENT:` directive parsing, the E.164 target regex, and
  `MAX_RCS_LENGTH` all live only there. A future `channels/sms.py` would
  be a new file implementing the same interface; it would not edit
  `rcs.py`, and a bug in one can't reach into the other.
- **`core/`** — infra genuinely shared across channels: `credentials.py`
  (Account SID / Auth Token resolution, Basic Auth header) and
  `messages_api.py` (the POST loop against Twilio's Messages resource —
  reusable by RCS, and later SMS/MMS/WhatsApp, since they're the same
  REST resource; **not** applicable to Voice or Email, which use
  different Twilio/provider APIs and would need their own transport
  module here).

Other notes:

- `TwilioAdapter.connect()`/`disconnect()` are no-ops (`_mark_connected`/
  `_mark_disconnected` only) — there's nothing to actually connect to.
- `_standalone_send()` is the primary delivery path in practice, since
  `hermes send` and cron jobs usually run in a separate process from any
  live gateway.
- `register(ctx)` in `adapter.py` wires everything into
  `gateway.platform_registry` — no core Hermes files were touched to add
  this plugin (see `website/docs/developer-guide/adding-platform-adapters.md`).
- The platform is registered under the single name `"twilio"`, with one
  active channel (`_CHANNEL` in `adapter.py`). If/when a second channel
  (SMS, WhatsApp, Voice, Email) is added, it'll need its own way to pick
  a channel per send — nothing in `register_platform()` currently
  distinguishes channels within one platform name, and this refactor
  only isolates each channel's *code*, not the *selection* between them
  at send time. Worth deciding deliberately (e.g. a channel prefix in
  the target ref or message, or separate registered platform names per
  channel) before wiring in the next one, rather than bolting it on ad
  hoc.

### Adding a new channel

1. Create `channels/<name>.py` implementing `MessagingChannel` from
   `channels/base.py`. If it sends via the Messages API resource (SMS,
   MMS, WhatsApp), reuse `core/messages_api.send_message_requests()` for
   the actual HTTP call — don't reimplement the POST loop. If it doesn't
   (Voice, Email), add a new module under `core/` for that transport
   instead of forcing it through `messages_api.py`.
2. Do **not** edit `channels/rcs.py` to do this — if you find yourself
   needing to, the shared piece you need probably belongs in `core/`
   instead.
3. Decide and implement the channel-selection strategy in `adapter.py`
   (see the note above) — this is the one place multi-channel dispatch
   is expected to live.

## Files

```
twilio/
  __init__.py              # re-exports register() for plugin discovery
  plugin.yaml               # kind: platform, env var declarations
  adapter.py                 # BasePlatformAdapter glue only — no channel logic
  core/
    credentials.py           # Account SID/Auth Token resolution, Basic Auth header
    messages_api.py          # shared POST loop against the Messages API resource
  channels/
    base.py                  # MessagingChannel interface every channel implements
    rcs.py                    # RCS channel — CONTENT: directive, E.164 targets, MAX_RCS_LENGTH
  scripts/
    manage_content.py    # Content API template create/list/get helper
```
