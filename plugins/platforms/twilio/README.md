# Twilio platform plugin

Outbound-only Hermes plugin for Twilio. Registered under one platform
name (`"twilio"`), currently hosting one channel:

- **RCS** — phone number target (`+15551234567`), sent via a Twilio
  **Messaging Service** (`MessagingServiceSid`); Twilio auto-falls-back
  to SMS/MMS for incapable recipients.

Built to host more channels (SMS, MMS, WhatsApp, Voice, Email) over
time — see "Architecture notes" for how to add one without touching
RCS's code. (Email was prototyped here and pulled back out to land as
its own PR — the `Channel`/`MessagingChannel` split below was shaped by
that work.)

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

| Env var | Required | Notes |
|---|---|---|
| `TWILIO_ACCOUNT_SID` | yes | Starts with `AC` — shared with the built-in `sms` platform and `telephony` skill |
| `TWILIO_AUTH_TOKEN` | yes | Shared with the built-in `sms` platform |
| `TWILIO_MESSAGING_SERVICE_SID` | yes | Starts with `MG`, needs an RCS Sender attached |
| `TWILIO_RCS_HOME_CHANNEL` | no | Destination E.164 number for cron `deliver=twilio` jobs |

Add to `~/.hermes/.env`; verify with `hermes status` (`Twilio ✓
configured (plugin)`).

## Sending plain text

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

### Known gaps

Not covered: `webview_size`/`height`/`orientation`/`thumbnailImageAlignment`
on cards (Twilio echoes defaults like `height: "TALL"`; valid value sets
unexplored), and RCS delivery receipts / read status (send-only, no
inbound webhook).

## Architecture notes

Three layers so a new channel never touches another channel's code:

- **`adapter.py`** — thin `BasePlatformAdapter` glue, no channel logic.
  Holds `_CHANNELS = [RcsChannel()]`, dispatches to whichever matches
  the target format (`_channel_for_target()`).
- **`channels/`** — one file per channel. `channels/base.py` declares:
  - `Channel` — minimal shape every channel implements
    (`check_requirements`, `connect_requirements_ok`, `is_connected`,
    `parse_target_ref`, `validate_target_ref`, `send`, `standalone_send`).
  - `MessagingChannel(Channel)` — for Messages-API channels. Implements
    `send()`/`standalone_send()` generically via `core/messages_api.py`;
    subclasses only need `format_message()` + `build_send_requests()`.
    `channels/rcs.py` is this shape.

  A channel with its own transport (not Twilio's Messages.json resource
  — Voice, Email) would implement `Channel` directly instead and own its
  `send()`/`standalone_send()` from scratch, the way the prototyped
  Email channel did before being pulled into its own PR.
- **`core/`** — shared across Messages-API channels: `credentials.py`
  (Account SID/Auth Token, Basic Auth header) and `messages_api.py` (the
  POST loop, reusable by RCS/SMS/MMS/WhatsApp; not Voice/Email, which
  need their own `core/` transport module).

### Channel dispatch

Dispatch is by target format, decided in `adapter.py`
(`_channel_for_target()`). Only RCS exists today, so this is a no-op in
practice — but the design constraint to keep in mind when adding the
next channel: SMS/MMS/WhatsApp would all *also* be phone-number
targets, colliding with RCS's format. Format-sniffing only works while
every channel's target shape is unique (as Email's would have been).
Adding a same-shaped channel needs an explicit disambiguation scheme
(e.g. a channel prefix) instead — decide deliberately, don't guess which
channel a bare phone number "really" means.

Other notes:

- `connect()`/`check_requirements()`/`is_connected()` succeed if **any**
  channel is ready — with only RCS today this is equivalent to "RCS is
  ready", but the check is written generically for when a second channel
  lands.
- `_standalone_send()` is the primary path in practice — `hermes send`
  and cron usually run in a separate process from any live gateway.
- `max_message_length` is registered as the largest across channels —
  matters once a channel with a different limit exists, since
  `send_message_tool.py` pre-chunks by this single value before any
  channel sees the content.
- `cron_deliver_env_var` is one static env var per platform in Hermes
  core (`cron/scheduler.py._resolve_home_env_var`) — no per-channel
  hook. A future channel needing its own cron target will need to share
  or contest RCS's `TWILIO_RCS_HOME_CHANNEL` slot; not solved generically.

### Adding a new channel

1. Create `channels/<name>.py`. Messages-API-based (SMS, MMS, WhatsApp):
   extend `MessagingChannel`, implement `format_message()` +
   `build_send_requests()`. Own-transport (Voice, Email): extend
   `Channel` directly.
2. Don't edit `rcs.py` to do this — shared logic belongs in `core/`.
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
  scripts/
    manage_content.py   # Content API template create/list/get helper
```
