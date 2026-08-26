# Twilio platform plugin

Outbound-only Hermes plugin for Twilio. Umbrella for multiple channels
under one platform name (`"twilio"`), dispatched by target format:

- **RCS** — phone number target (`+15551234567`), sent via a Twilio
  **Messaging Service** (`MessagingServiceSid`); Twilio auto-falls-back
  to SMS/MMS for incapable recipients.
- **Email** — email address target (`someone@example.com`), sent via
  Twilio Email (SendGrid Mail Send API) — separate credentials
  (`SENDGRID_API_KEY`, not `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN`).

More channels (SMS, MMS, WhatsApp, Voice) are expected later — see
"Architecture notes" for how to add one without touching another
channel's code.

Note: the built-in `sms` platform (`plugins/platforms/sms/`) also talks
to Twilio and is independent of this plugin; they only overlap in
reading `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN`.

No inbound channel — no webhook, no polling, no `hermes gateway`
listener. Outbound only: `hermes send`, cron `deliver=twilio`, or an
agent's `terminal` tool shelling out to `hermes send`.

## For AI agents reading this file

There is **no agent-callable tool** for sending — `send_message` exists
as a schema in `tools/send_message_tool.py` but is never registered into
a toolset (see `toolsets.py` / `_HERMES_CORE_TOOLS`). To send, use your
`terminal` tool:

```bash
hermes send --to "twilio:+15551234567" "your message text"
```

For rich content, add a `CONTENT:` directive (see "Rich content" below).
Don't fabricate a raw JSON card payload — Twilio's Messages API only
accepts a `ContentSid` referencing a template created ahead of time.

## Setup

Only one channel needs to be configured — RCS and Email don't share env
vars.

**RCS** (`TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN` shared with the
built-in `sms` platform and the `telephony` skill):

| Env var | Required | Notes |
|---|---|---|
| `TWILIO_ACCOUNT_SID` | yes | Starts with `AC` |
| `TWILIO_AUTH_TOKEN` | yes | |
| `TWILIO_MESSAGING_SERVICE_SID` | yes | Starts with `MG`, needs an RCS Sender attached |
| `TWILIO_RCS_HOME_CHANNEL` | no | Destination E.164 number for cron `deliver=twilio` jobs |

**Email** (separate SendGrid credentials):

| Env var | Required | Notes |
|---|---|---|
| `SENDGRID_API_KEY` | yes | Starts with `SG.`, needs Mail Send permission |
| `SENDGRID_FROM_EMAIL` | yes | Verified Sender or authenticated domain in SendGrid |
| `SENDGRID_FROM_NAME` | no | Sender display name |
| `SENDGRID_API_BASE` | no | Override for a staging host |
| `SENDGRID_HOME_CHANNEL` | no | Destination address — **not wired to cron**, see "Architecture notes" |

Add whichever set you need to `~/.hermes/.env`; verify with `hermes
status` (`Twilio ✓ configured (plugin)` once one channel is ready).

## Sending plain text (RCS)

```bash
hermes send --to "twilio:+15551234567" "hello from Hermes"
```

Bare E.164 targets — this plugin declares its own
`parse_target_ref_fn`/`validate_target_ref_fn` since it isn't in core's
hardcoded phone-platform allowlist (`tools/send_message_tool._PHONE_PLATFORMS`).

Markdown-stripped, chunked at `MAX_RCS_LENGTH` (3072 — Twilio's
documented RCS limit; re-verify if messages start truncating).

## Sending email

```bash
hermes send --to "twilio:someone@example.com" "Order shipped
Your package is on its way — track it at https://example.com/track/123"
```

Bare email address routes to Email — no ambiguity with RCS's phone
format. First line becomes the subject, rest is the body; single-line
content gets a generic default subject. Never chunked (one email, one
document).

HTML or an explicit subject needs `metadata={"subject": ..., "html":
True}` on the adapter's own `send()` — the CLI/cron `standalone_send()`
path only has the first-line convention; there's no `hermes send` flag
for it.

SendGrid error bodies often echo the offending address — masked before
logging/returning (`_mask_email`/`_redact_emails_in_text` in `channels/email.py`).

## Rich content (cards, carousels)

Twilio's Messages API only accepts pre-created **Content API templates**
via `ContentSid` (+ optional `ContentVariables`) — no inline JSON for
cards/carousels on RCS or WhatsApp. A freshly created template sends
immediately, no approval step.

**RCS-supported types (Twilio docs):** `twilio/text`, `twilio/media`,
`twilio/card`, `twilio/carousel`. `twilio/quick-reply` is **not** in
that list — `create-quick-reply` still works (verified schema, real
WhatsApp type) but RCS sends silently fall back to SMS/MMS instead of
rendering chips. Use `create-card`/`create-carousel` for true RCS rich
content.

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

# Quick-reply chips — WhatsApp-verified, not true RCS rich content (see above)
python plugins/platforms/twilio/scripts/manage_content.py create-quick-reply \
  --friendly-name "order_confirm" \
  --body "Your order shipped! Track it?" \
  --action "Yes:track_yes" \
  --action "No:track_no"
```

Each prints the resulting `ContentSid` (`HX...`) and a ready-to-paste
`hermes send` command. `list`/`get <content_sid>` inspect existing
templates.

**`media` field shape differs by type (live-confirmed, not just docs):**

| Type | `media` field |
|---|---|
| `twilio/card` (top-level) | **array** of URLs — bare string 400s |
| `twilio/carousel` (per-card) | **single string** URL |

`--media`/`--cards-json` already handle this correctly — only matters if
calling `create_card()`/`create_carousel()` directly.

Stdlib HTTP only (no `aiohttp`/`requests`), reads
`TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN` from `~/.hermes/.env` — runs
standalone, outside the Hermes venv.

### 2. Send it

```bash
hermes send --to "twilio:+15551234567" "CONTENT:HXxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

# with template variables ({{1}}, {{2}}, ... in the template body)
hermes send --to "twilio:+15551234567" 'CONTENT:HXxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx:{"1":"Alice"}'
```

`CONTENT:<sid>[:<json>]` is recognized in both `send()` and
`_standalone_send()`, mirroring the `MEDIA:<path>` convention used
elsewhere in Hermes. Malformed JSON raises a clear error.

### Known gaps

Not covered: `webview_size`/`height`/`orientation`/`thumbnailImageAlignment`
on cards (Twilio echoes defaults like `height: "TALL"`; valid value sets
unexplored), and RCS delivery receipts / read status (send-only, no
inbound webhook).

## Architecture notes

Three layers so a new channel never touches another channel's code:

- **`adapter.py`** — thin `BasePlatformAdapter` glue, no channel logic.
  Holds `_CHANNELS = [RcsChannel(), EmailChannel()]`, dispatches to
  whichever matches the target format (`_channel_for_target()`).
- **`channels/`** — one file per channel. `channels/base.py` declares:
  - `Channel` — minimal shape every channel implements
    (`check_requirements`, `connect_requirements_ok`, `is_connected`,
    `parse_target_ref`, `validate_target_ref`, `send`, `standalone_send`).
  - `MessagingChannel(Channel)` — for Messages-API channels. Implements
    `send()`/`standalone_send()` generically via `core/messages_api.py`;
    subclasses only need `format_message()` + `build_send_requests()`.
    `channels/rcs.py` is this shape.

  `channels/email.py` implements `Channel` directly, not
  `MessagingChannel` — SendGrid, not `MessagingServiceSid`/Messages.json
  — and owns its own transport. A future `channels/sms.py` or
  `whatsapp.py` would extend `MessagingChannel` like RCS; none of them
  edit each other's files.
- **`core/`** — shared across Messages-API channels: `credentials.py`
  (Account SID/Auth Token, Basic Auth header — Email also reuses
  `get_scoped_secret` for its own vars) and `messages_api.py` (the POST
  loop, reusable by RCS/SMS/MMS/WhatsApp; not Voice, which needs its own
  `core/` module for Calls.json).

### Channel dispatch

Dispatch is by target format:

- `+15551234567` → `RcsChannel`
- `someone@example.com` → `EmailChannel`

Works because the formats can't collide. SMS/MMS/WhatsApp would all
also be phone-number targets, so adding one requires an explicit
disambiguation scheme (e.g. a channel prefix) instead of format-sniffing
— decide deliberately, don't guess which channel a bare number means.

**Cron limitation.** `cron_deliver_env_var` is one static env var per
platform in Hermes core (`cron/scheduler.py._resolve_home_env_var`) — no
per-channel hook. This plugin keeps that slot on RCS's
`TWILIO_RCS_HOME_CHANNEL`; `SENDGRID_HOME_CHANNEL` is documented but
**cron `deliver=twilio` can't target Email** — only `hermes send --to
twilio:<email>` works. Fixing this needs a core change (multiple
home-channel vars per platform) or splitting Email into its own platform
name — not done here.

Other notes:

- `connect()`/`check_requirements()`/`is_connected()` succeed if **any**
  channel is ready — a user configuring only one channel shouldn't see
  the platform fail to start.
- `_standalone_send()` is the primary path in practice — `hermes send`
  and cron usually run in a separate process from any live gateway.
- `max_message_length` is registered as the largest across channels
  (Email's 200,000, not RCS's 3,072) because `send_message_tool.py`
  pre-chunks by this value before any channel sees the content — RCS's
  smaller limit would silently split long emails. RCS still chunks
  correctly at 3,072 internally.

### Adding a new channel

1. Create `channels/<name>.py`. Messages-API-based (SMS, MMS, WhatsApp):
   extend `MessagingChannel`, implement `format_message()` +
   `build_send_requests()`. Own-transport (Voice, etc.): extend `Channel`
   directly like `channels/email.py`.
2. Don't edit `rcs.py`/`email.py` to do this — shared logic belongs in `core/`.
3. Add an instance to `_CHANNELS` in `adapter.py`. If its target format
   could collide with an existing channel's, design explicit
   disambiguation first (see "Channel dispatch").
4. Decide what to do about `cron_deliver_env_var` — not solved generically yet.

## Files

```
twilio/
  __init__.py           # re-exports register() for plugin discovery
  plugin.yaml            # kind: platform, env var declarations
  adapter.py              # BasePlatformAdapter glue + channel dispatch
  core/
    credentials.py        # Account SID/Auth Token, Basic Auth header, scoped-secret read
    messages_api.py       # shared POST loop against the Messages API resource
  channels/
    base.py                # Channel + MessagingChannel interfaces
    rcs.py                  # RCS — CONTENT: directive, E.164 targets, MAX_RCS_LENGTH
    email.py                # Email — SendGrid Mail Send API, subject/body split, PII masking
  scripts/
    manage_content.py   # Content API template create/list/get helper
```
