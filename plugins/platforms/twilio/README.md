# Twilio platform plugin

Outbound-only Hermes plugin for Twilio. Registered under one platform
name (`"twilio"`), hosting two channels dispatched by target format:

- **RCS** — phone number target (`+15551234567`), sent via a Twilio
  **Messaging Service** (`MessagingServiceSid`); Twilio auto-falls-back
  to SMS/MMS for incapable recipients.
- **Email** — email address target (`customer@example.com`), sent via
  Twilio's **Email API** (`comms.twilio.com`, One Console — not the
  older SendGrid `api.sendgrid.com` v3 Mail Send API). Uses the same
  core Twilio credentials as RCS, not a separate key.

Built to host more channels (SMS, MMS, WhatsApp, Voice) over time —
see "Architecture notes" for how to add one without touching an
existing channel's code.

Note: the built-in `sms` platform (`plugins/platforms/sms/`) also talks
to Twilio and is independent of this plugin; they only overlap in
reading `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN`. The built-in `email`
platform is unrelated too — that one is generic personal-mailbox
IMAP/SMTP, nothing to do with Twilio.

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
hermes send --to "twilio:customer@example.com" "Subject line
Body text."
```

For rich RCS content, add a `CONTENT:` directive (see "Rich content"
below). Don't fabricate a raw JSON card payload — Twilio's Messages API
only accepts a `ContentSid` referencing a template created ahead of
time. For an email attachment, use the standard `MEDIA:<path>`
convention (see "Sending email" below) — same mechanism every platform
in this repo uses, not something Email-specific.

## Setup

| Env var | Required | Notes |
|---|---|---|
| `TWILIO_ACCOUNT_SID` | yes | Starts with `AC` — shared with the built-in `sms` platform, `telephony` skill, and both channels here |
| `TWILIO_AUTH_TOKEN` | yes | Shared the same way |
| `TWILIO_MESSAGING_SERVICE_SID` | for RCS | Starts with `MG`, needs an RCS Sender attached |
| `TWILIO_RCS_HOME_CHANNEL` | no | Destination E.164 number for cron `deliver=twilio` jobs (RCS) |
| `TWILIO_EMAIL_FROM` | for Email | Default sender — must be a verified sender identity for the Email product in the Twilio Console |
| `TWILIO_EMAIL_FROM_NAME` | no | Sender display name (Email) |
| `TWILIO_EMAIL_API_BASE` | no | Override the Email API base (default `https://comms.twilio.com/v1/Emails`) |
| `TWILIO_EMAIL_HOME_CHANNEL` | no | Destination email address for cron `deliver=twilio` jobs (Email) — see "cron_deliver_env_var" caveat below |

Add to `~/.hermes/.env`; verify with `hermes status` (`Twilio ✓
configured (plugin)`). Only one channel needs to be configured for the
plugin to register as connected — `connect()`/`is_connected()` succeed
if *any* channel is ready.

## Sending plain text (RCS)

```bash
hermes send --to "twilio:+15551234567" "hello from Hermes"
```

Bare E.164 targets — this plugin declares its own
`parse_target_ref_fn`/`validate_target_ref_fn` since it isn't in core's
hardcoded phone-platform allowlist (`tools/send_message_tool._PHONE_PLATFORMS`).

Markdown-stripped, chunked at `MAX_RCS_LENGTH` (3072 — Twilio's
documented RCS limit; re-verify if messages start truncating).

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

### Known gaps (RCS)

Not covered: `webview_size`/`height`/`orientation`/`thumbnailImageAlignment`
on cards (Twilio echoes defaults like `height: "TALL"`; valid value sets
unexplored), and RCS delivery receipts / read status (send-only, no
inbound webhook).

## Sending email

```bash
hermes send --to "twilio:customer@example.com" $'Order shipped\nYour package is on its way.'

# with an attachment — same MEDIA:<path> convention every platform uses
hermes send --to "twilio:customer@example.com" "Report attached
MEDIA:/path/to/report.pdf"
```

Bare email-address targets, same `parse_target_ref_fn`/`validate_target_ref_fn`
mechanism as RCS's phone numbers.

**Subject/body convention** — every other channel here passes one plain
`content` string with no subject concept. By convention: the **first
line of `content` is the subject**, the remainder is the body. A
single-line message gets a generic default subject
("Message from Hermes Agent"). A `--subject` CLI flag also works —
`hermes_cli/send_cmd.py` prepends it onto the body with a blank line,
landing on this same first-line convention, not a separate code path.

**Attachments** — local files are attached directly (base64, inline in
the request; ~7 MB cap, headroom under the API's 10 MB total-request
limit); remote `http(s)://` image URLs are linked in the body text
instead of downloaded (matches the built-in `email` plugin's own
convention). Reachable via `MEDIA:<path>` (the `hermes send`/cron path)
or, for direct-Python callers only, `metadata={"attachments": [...]}`
on `send()` — nothing in this repo actually calls `send()` with
`metadata` today.

**Async by design, not a delivery guarantee** — a successful call
returns `202` with an `operationId`: accepted for processing, not
confirmed delivered. This channel doesn't poll the Email Operation
resource (`GET .../Operations/{operationId}`); the `operationId` comes
back as the send's `message_id` for anyone who wants to check later.

### Known gaps (Email)

No cc/bcc, no scheduled send (`schedule.sendAt`), no inline `cid` image
references, no HTML via `hermes send`/cron (only via the direct-Python
`metadata={"html": True}` escape hatch) — their exact request shapes
either weren't confirmed against a live account or aren't wired up to
any reachable caller yet.

**Two payload quirks confirmed live, contradicting the docs** (see
`channels/email.py` for detail): `from.name` must always be present, or
the API returns a generic `Invalid value provided for field 'from'`
that masks the actual validation error (e.g. domain authorization); and
`content.html` is required even for a plain-text send — the docs
describe auto-generating a `text` fallback *from* `html`, not the
reverse.

## Architecture notes

Three layers so a new channel never touches another channel's code:

- **`adapter.py`** — thin `BasePlatformAdapter` glue, no channel logic.
  Holds `_CHANNELS = [RcsChannel(), EmailChannel()]`, dispatches to
  whichever matches the target format (`_channel_for_target()`). Also
  dispatches `send_image()`/`send_document()`/`send_multiple_images()`
  to the matched channel's own method when it has one (Email does; RCS
  doesn't — falls back to `BasePlatformAdapter`'s default).
- **`channels/`** — one file per channel. `channels/base.py` declares:
  - `Channel` — minimal shape every channel implements
    (`check_requirements`, `connect_requirements_ok`, `is_connected`,
    `parse_target_ref`, `validate_target_ref`, `send`, `standalone_send`).
  - `MessagingChannel(Channel)` — for Messages-API channels. Implements
    `send()`/`standalone_send()` generically via `core/messages_api.py`;
    subclasses only need `format_message()` + `build_send_requests()`.
    `channels/rcs.py` is this shape.

  A channel with its own transport (not Twilio's Messages.json resource
  — Email, Voice) implements `Channel` directly instead and owns its
  `send()`/`standalone_send()` from scratch. `channels/email.py` is
  this shape — its whole POST/response-parsing/attachment-building
  transport lives in that one file, since it's the only consumer of
  that particular JSON/REST shape (extract to `core/` if a second one
  ever needs it — not preemptively).
- **`core/`** — shared across every channel: `credentials.py` (Account
  SID/Auth Token, Basic Auth header, scoped-secret read) and
  `messages_api.py` (the Messages.json POST loop — RCS/SMS/MMS/WhatsApp
  only, not Email/Voice, which need their own transport).

### Channel dispatch

Dispatch is by target format, decided in `adapter.py`
(`_channel_for_target()`). RCS (E.164 phone numbers) and Email (email
addresses) have mutually exclusive target shapes, so no disambiguation
was needed adding the second channel — but keep this constraint in mind
for the next one: SMS/MMS/WhatsApp would all *also* be phone-number
targets, colliding with RCS's format. Format-sniffing only works while
every channel's target shape is unique. Adding a same-shaped channel
needs an explicit disambiguation scheme (e.g. a channel prefix) instead
— decide deliberately, don't guess which channel a bare phone number
"really" means.

Other notes:

- `connect()`/`check_requirements()`/`is_connected()` succeed if **any**
  channel is ready — e.g. Email alone configured with no
  `TWILIO_MESSAGING_SERVICE_SID` still connects.
- `_standalone_send()` is the primary path in practice — `hermes send`
  and cron usually run in a separate process from any live gateway. It
  forwards `media_files`/`force_document`/`thread_id` to whichever
  channel's `standalone_send(pconfig, chat_id, message, **kwargs)` is
  dispatched to; `MessagingChannel`'s ignores them via its own
  `**kwargs`, Email's reads `media_files` for attachments.
- `max_message_length` is registered as the largest across channels
  (currently Email's 200,000, not RCS's 3,072) — matters because
  `send_message_tool.py` pre-chunks by this single value before any
  channel sees the content.
- `cron_deliver_env_var` is one static env var per platform in Hermes
  core (`cron/scheduler.py._resolve_home_env_var`) — no per-channel
  hook. With two channels now, only RCS's `TWILIO_RCS_HOME_CHANNEL` is
  wired into `register()`; `EmailChannel.cron_deliver_env_var`
  (`TWILIO_EMAIL_HOME_CHANNEL`) exists on the class for interface
  completeness but isn't reachable via cron yet — not solved
  generically, same as before.

### Adding a new channel

1. Create `channels/<name>.py`. Messages-API-based (SMS, MMS, WhatsApp):
   extend `MessagingChannel`, implement `format_message()` +
   `build_send_requests()`. Own-transport (Voice, and Email already):
   extend `Channel` directly.
2. Don't edit `rcs.py`/`email.py` to do this — shared logic belongs in
   `core/`.
3. Add an instance to `_CHANNELS` in `adapter.py`. If its target format
   could collide with an existing channel's, design explicit
   disambiguation first (see "Channel dispatch").
4. Decide what to do about `cron_deliver_env_var` — not solved generically yet.
5. If it needs attachment support beyond `send()`/`standalone_send()`
   (`send_image`/`send_document`/`send_multiple_images`), implement
   those as extra methods on the channel — `adapter.py`'s
   `_dispatch_attachment_call()` picks them up automatically via
   `getattr`, no adapter changes needed.

## Files

```
twilio/
  __init__.py           # re-exports register() for plugin discovery
  plugin.yaml            # kind: platform, env var declarations
  adapter.py              # BasePlatformAdapter glue + channel dispatch
  core/
    credentials.py        # Account SID/Auth Token, Basic Auth header, scoped-secret read
    messages_api.py       # shared POST loop against the Messages API resource (RCS/SMS/MMS/WhatsApp)
  channels/
    base.py                # Channel + MessagingChannel interfaces
    rcs.py                  # RCS — CONTENT: directive, E.164 targets, MAX_RCS_LENGTH
    email.py                 # Email — comms.twilio.com transport, attachments, subject/body convention
  scripts/
    manage_content.py   # Content API template create/list/get helper
```
