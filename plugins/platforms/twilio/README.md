# Twilio platform plugin

Outbound-only Hermes platform plugin for Twilio. An umbrella for multiple
Twilio channels, all registered under one platform name (`"twilio"`) and
dispatched by target format:

- **RCS** — a phone number target (`+15551234567`), sent through a Twilio
  **Messaging Service** (`MessagingServiceSid`); Twilio automatically
  selects RCS for capable recipients and falls back to SMS/MMS otherwise.
- **Email** — an email address target (`someone@example.com`), sent
  through Twilio Email (SendGrid's Mail Send API under the hood) — a
  completely separate credential surface (`SENDGRID_API_KEY`, not
  `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN`).

More channels (SMS, MMS, WhatsApp, Voice) are expected to land here over
time — see "Architecture notes" below for how a channel is added without
touching another channel's code.

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

Only one channel needs to be fully configured for this plugin to be
useful — RCS and Email don't depend on each other's env vars.

**RCS** (`TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN` shared with the
built-in `sms` platform and the optional `telephony` skill):

| Env var | Required | Notes |
|---|---|---|
| `TWILIO_ACCOUNT_SID` | yes | Starts with `AC` |
| `TWILIO_AUTH_TOKEN` | yes | |
| `TWILIO_MESSAGING_SERVICE_SID` | yes | Starts with `MG` — must have an RCS Sender (approved by Google) attached in the Twilio Console |
| `TWILIO_RCS_HOME_CHANNEL` | no | Destination E.164 number for cron `deliver=twilio` jobs |

**Email** (its own credential surface — SendGrid, not core Twilio):

| Env var | Required | Notes |
|---|---|---|
| `SENDGRID_API_KEY` | yes | Starts with `SG.`, needs Mail Send permission |
| `SENDGRID_FROM_EMAIL` | yes | Must be a Verified Sender or part of an authenticated domain in SendGrid |
| `SENDGRID_FROM_NAME` | no | Display name for the sender |
| `SENDGRID_API_BASE` | no | Override for a staging SendGrid host |
| `SENDGRID_HOME_CHANNEL` | no | Destination email address — **not yet wired to cron delivery**, see "Architecture notes" |

Add whichever set you need to `~/.hermes/.env`, then verify with `hermes
status` (shows `Twilio ✓ configured (plugin)` once at least one channel
is ready).

## Sending plain text (RCS)

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

## Sending email

```bash
hermes send --to "twilio:someone@example.com" "Order shipped
Your package is on its way — track it at https://example.com/track/123"
```

Target is a bare email address — routed to the Email channel purely by
format (no phone number can also be a valid email address, so there's no
ambiguity). By convention, the **first line becomes the subject** and the
rest becomes the body; a single-line message gets a generic default
subject rather than guessing one from the whole text. Unlike RCS, an
email is never chunked — it's one document, not a multi-part SMS train.

Sending HTML or an explicit subject requires going through the live
gateway's `send()` with `metadata={"subject": ..., "html": True}` (the
CLI/cron `standalone_send()` path always uses the first-line convention
and plain text) — there's currently no `hermes send` flag for this, only
a `metadata` kwarg on the adapter's own `send()`.

Errors from SendGrid, and any email address that appears inside them
(SendGrid's own validation errors commonly echo the offending address
back), are masked before being logged or returned — see
`_mask_email`/`_redact_emails_in_text` in `channels/email.py`.

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

The plugin is split into three layers so that adding a new channel never
requires touching another channel's code:

- **`adapter.py`** — thin `BasePlatformAdapter` glue only. Handles
  connect/disconnect lifecycle and the `SendResult`/dict shape Hermes
  expects; contains zero channel-specific logic. It holds a list of
  channel instances (`_CHANNELS = [RcsChannel(), EmailChannel()]`) and
  dispatches every channel-specific decision to whichever one matches
  the send target's format (`_channel_for_target()`).
- **`channels/`** — one file per channel. `channels/base.py` declares two
  contracts:
  - `Channel` — the minimal shape every channel implements
    (`check_requirements`, `connect_requirements_ok`, `is_connected`,
    `parse_target_ref`, `validate_target_ref`, `send`, `standalone_send`).
  - `MessagingChannel(Channel)` — for channels transported over Twilio's
    Messages API resource. It implements `send()`/`standalone_send()`
    once, generically (via `core/messages_api.py`), so a Messages-API
    channel only needs `format_message()` + `build_send_requests()`.
    `channels/rcs.py` is this shape, and is fully self-contained — the
    `CONTENT:` directive parsing, the E.164 target regex, and
    `MAX_RCS_LENGTH` all live only there.

  `channels/email.py` implements `Channel` **directly**, not
  `MessagingChannel` — Twilio Email doesn't use `MessagingServiceSid` or
  the Messages.json resource at all (it's SendGrid's Mail Send API, a
  different credential surface and a different transport entirely), so
  it owns its own `send()`/`standalone_send()` from scratch rather than
  being forced into a shape that doesn't fit. A future `channels/sms.py`
  or `channels/whatsapp.py` would extend `MessagingChannel` like RCS
  does; neither would edit `rcs.py` or `email.py`, and a bug in one can't
  reach into another.
- **`core/`** — infra genuinely shared across *Messages-API* channels:
  `credentials.py` (Account SID / Auth Token resolution, Basic Auth
  header — also reused by Email for its own env-var reads, since
  `get_scoped_secret` is generic) and `messages_api.py` (the POST loop
  against Twilio's Messages resource — reusable by RCS, and later
  SMS/MMS/WhatsApp, since they're the same REST resource; **not**
  applicable to Voice, which would need its own `core/` module for the
  Calls.json resource).

### Channel dispatch (how RCS and Email coexist under one platform name)

Both channels register under the single platform name `"twilio"`.
Dispatch is by **target format**, decided in `adapter.py`:

- `+15551234567` (E.164) → `RcsChannel`
- `someone@example.com` → `EmailChannel`

This works cleanly *because* the two formats can never collide — nothing
that validates as an E.164 phone number can also validate as an email
address. That won't hold for every future channel: SMS, MMS, and
WhatsApp would all also use phone-number targets, so adding one of those
requires an explicit disambiguation scheme (e.g. a channel prefix in the
target ref or message) rather than the implicit format-sniffing used
here — decide that deliberately when the time comes, don't bolt it on
by guessing which channel a bare phone number "really" means.

**Known limitation — cron delivery is single-channel.** Hermes core's
cron scheduler (`cron/scheduler.py._resolve_home_env_var`) resolves
exactly one static env var name per registered platform via
`cron_deliver_env_var` — there's no per-channel hook. This plugin keeps
that slot pointed at RCS's `TWILIO_RCS_HOME_CHANNEL`. `SENDGRID_HOME_CHANNEL`
exists as a documented env var (`plugin.yaml`) for forward compatibility,
but **cron `deliver=twilio` jobs cannot currently target the Email
channel** — only `hermes send --to twilio:<email>` (interactive/scripted,
not cron) works today. Fixing this would need either a core change to
`PlatformEntry`/cron (supporting multiple home-channel vars per
platform) or splitting Email into its own registered platform name —
not done here to avoid scope creep on this merge.

- `TwilioAdapter.connect()` succeeds if **any** channel's
  `connect_requirements_ok()` passes (not all) — a user configuring only
  Email shouldn't see the whole platform refuse to start because RCS
  isn't set up, and vice versa. Same "any channel ready" logic applies
  to `check_requirements()`/`is_connected()`.
- `_standalone_send()` is the primary delivery path in practice, since
  `hermes send` and cron jobs usually run in a separate process from any
  live gateway.
- `register(ctx)` in `adapter.py` wires everything into
  `gateway.platform_registry` — no core Hermes files were touched to add
  this plugin (see `website/docs/developer-guide/adding-platform-adapters.md`).
- `max_message_length` is registered as the **largest** value across
  channels (Email's 200,000, not RCS's 3,072), because
  `tools/send_message_tool.py` pre-chunks by this single value before
  ever reaching a channel — using RCS's smaller limit here would
  silently split long emails into multiple separate sends. RCS still
  gets correctly chunked at its own 3,072-char limit internally, inside
  `RcsChannel.build_send_requests()`.

### Adding a new channel

1. Create `channels/<name>.py`. If it sends via the Messages API
   resource (SMS, MMS, WhatsApp), extend `MessagingChannel` from
   `channels/base.py` and implement only `format_message()` +
   `build_send_requests()` — `send()`/`standalone_send()` come for free.
   If it doesn't (Voice, and anything else with its own transport),
   extend `Channel` directly and implement `send()`/`standalone_send()`
   yourself, the way `channels/email.py` does.
2. Do **not** edit `channels/rcs.py` or `channels/email.py` to do this —
   if you find yourself needing to, the shared piece you need probably
   belongs in `core/` instead.
3. Add an instance to `_CHANNELS` in `adapter.py`. If the new channel's
   target format could collide with an existing one (any phone-number
   channel added alongside RCS), stop and design explicit
   disambiguation — see "Channel dispatch" above — before wiring it in.
4. Decide what to do about `cron_deliver_env_var` for the new channel —
   see the cron limitation noted above; it isn't solved generically yet.

## Files

```
twilio/
  __init__.py              # re-exports register() for plugin discovery
  plugin.yaml               # kind: platform, env var declarations
  adapter.py                 # BasePlatformAdapter glue + channel dispatch — no channel-specific logic
  core/
    credentials.py           # Account SID/Auth Token resolution, Basic Auth header, generic scoped-secret read
    messages_api.py          # shared POST loop against the Messages API resource (RCS today)
  channels/
    base.py                  # Channel + MessagingChannel interfaces
    rcs.py                    # RCS channel — CONTENT: directive, E.164 targets, MAX_RCS_LENGTH
    email.py                  # Email channel — SendGrid Mail Send API, subject/body split, PII masking
  scripts/
    manage_content.py    # Content API template create/list/get helper (RCS rich content)
```
