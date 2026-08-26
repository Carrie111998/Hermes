# Twilio Email platform plugin

This plugin lets Hermes Agent send email through **Twilio's Email API**
(the newer One Console API at `comms.twilio.com`) — not the older SendGrid
`api.sendgrid.com` v3 Mail Send API. Auth is the **same core Twilio
credentials as SMS/Voice**: `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` over
HTTP Basic Auth. If you already have Twilio SMS working on this agent,
those same two env vars work here too — you only need to add a verified
sender address.

Docs: https://www.twilio.com/docs/email/api/overview

## For AI agents reading this file

If a user asks you (the agent) to "email customer@x.com", there is **no
agent-callable tool for this** — `send_message` exists as a tool schema in
`tools/send_message_tool.py` but is intentionally never registered into any
toolset (outbound platform messaging is deliberately kept outside the
model's own initiative; see `toolsets.py` around `_HERMES_CORE_TOOLS`, which
states this explicitly). The only way to actually deliver a message from a
chat session is to use your `terminal` tool to run the CLI command
yourself:

```bash
hermes send --to "twilio_email:customer@example.com" "Order shipped
Your package is on its way."
```

Same convention as every other outbound platform plugin in this repo —
`send_message` is deliberately excluded from every agent toolset (see
`toolsets.py`), so any plugin's send path is only reachable via `hermes
send`, cron delivery, or the gateway's own notifier.

To attach a local file, use the same `MEDIA:<path>` convention every other
platform uses — see **Attachments** below.

## Architecture

Outbound-only, no inbound channel, no webhook, no polling. A message goes
out exactly when something calls `send()` — the `hermes send` CLI (used
directly by a human, or by an agent via its own `terminal` tool, per
above), or a cron job with `deliver=twilio_email`.

```
 hermes send (human or agent's terminal tool)   Twilio Email API
 / cron deliver=twilio_email
┌────────────────────────────────┐   HTTPS    ┌───────────────────────┐
│  --to twilio_email:            │  Basic     │  comms.twilio.com     │
│    customer@x.com              │ ─────────► │  /v1/Emails           │
│  "Subject\nBody"                │            │  (Twilio Email API)   │
└────────────────────────────────┘            └───────────┬───────────┘
                                                            │ 202 + operationId
                                                            ▼
                                                   queued for delivery
```

Because there's no inbound side, `connect()` is just a readiness check
(are credentials + a sender configured?) and `disconnect()` just tears
down the HTTP session — there's no persistent connection to hold open.

**Async by design — 202 is not "delivered".** Every successful call
returns `202 Accepted` with an `operationId`: the send was accepted for
processing, not delivered to the recipient's inbox. Real delivery status
lives behind the Email Operation resource
(`GET /v1/Emails/Operations/{operationId}`), which this plugin does not
poll. `SendResult.message_id` (live path) and the standalone dict's
`message_id` (CLI/cron path) both carry the `operationId` if you want to
check status yourself via the Twilio Console or a direct API call.

## First-time setup

```bash
hermes gateway setup
# select "Twilio Email" from the platform list
```

This prompts for:

1. **Twilio Account SID / Auth Token** — from the Twilio Console dashboard.
   Already set for SMS on this agent? Reuse the same values.
2. **Sender email** (`TWILIO_EMAIL_FROM`) — see **Sender verification**
   below before picking this.

If the interactive wizard doesn't cooperate, add the same variables to
`~/.hermes/.env` directly — see **Credentials** below for the exact keys.

## Sender verification

Twilio's Email product requires a verified sending identity before
`TWILIO_EMAIL_FROM` will actually send anything — check the **Email**
section of the Twilio Console for the current sender/domain verification
flow. (This plugin's auth and request shape are confirmed live against
`comms.twilio.com` — see **Architecture notes** — but a real send with a
verified sender hasn't been exercised yet; confirm the exact console steps
when you test that part.)

## Credentials

All in `${HERMES_HOME:-~/.hermes}/.env`:

```bash
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_EMAIL_FROM=you@yourdomain.com
```

## Configuration knobs

All env vars are documented in `plugin.yaml`. The full set:

| Env var                     | Required | Default                          | Meaning |
|------------------------------|----------|-----------------------------------|---------|
| `TWILIO_ACCOUNT_SID`         | Yes      | —                                  | Twilio Account SID. Shared with the `sms` plugin if already configured. |
| `TWILIO_AUTH_TOKEN`          | Yes      | —                                  | Twilio Auth Token. Shared with the `sms` plugin if already configured. |
| `TWILIO_EMAIL_FROM`          | Yes      | —                                  | Default sender. Must be a verified sender identity for the Email product. |
| `TWILIO_EMAIL_FROM_NAME`     | No       | (none)                             | Sender display name. |
| `TWILIO_EMAIL_API_BASE`      | No       | `https://comms.twilio.com/v1/Emails` | Override the API base (e.g. for a non-public test environment). |
| `TWILIO_EMAIL_HOME_CHANNEL`  | No       | (none)                             | Default recipient address for cron / notification delivery (`deliver=twilio_email`). |

## Usage

```bash
hermes send --to "twilio_email:customer@example.com" $'Order shipped\nYour package is on its way.'
```

A subject can also be set explicitly on the CLI:

```bash
hermes send --to "twilio_email:customer@example.com" --subject "Order shipped" "Your package is on its way."
```

`--subject` works through the CLI's generic text-prepending (`hermes_cli/send_cmd.py`
joins it onto the message body with a blank line), which then lands on the
same first-line convention described below — it isn't a separate code path
in this plugin.

The target-resolution plumbing (`parse_target_ref_fn`/`validate_target_ref_fn`)
is shared across every Twilio channel plugin — see **For AI agents reading
this file** above for how an agent actually reaches this in a live chat
session (it isn't a directly agent-callable tool).

**Subject/body convention** — every other platform this adapter sits
alongside (SMS, Discord, ...) passes one plain `content` string with no
concept of a subject line. To stay consistent with that shared interface
instead of inventing a special case, this plugin treats **the first line
of `content` as the subject** and everything after the first newline as
the body:

```
Order shipped
Your package is on its way and should arrive Thursday.
```

sends with subject `Order shipped` and that second line as the body. A
single-line message (no newline) gets a generic default subject
("Message from Hermes Agent") and the whole line as the body, rather than
guessing which part was meant to be which.

`send()` also accepts `metadata={"subject": "...", "html": True,
"attachments": ["/path/to/file"]}` to skip the first-line convention,
optionally send HTML instead of plain text, and attach local files.
**Nothing in this repo calls `send()` with `metadata` today** — not
`hermes send`, not cron delivery, not an agent (there's no agent-callable
send tool at all; see above). This is a live-gateway-only escape hatch for
a caller instantiating `TwilioEmailAdapter` directly in Python. The
out-of-process path that `hermes send` and cron actually use
(`_standalone_send()`) has no `metadata` parameter, always sends plain
text via the first-line convention, and gets its attachments from
`media_files` instead (see below).

## Attachments

Local files are attached directly (base64, inline in the request); remote
`http(s)://` image URLs are **not** downloaded — they're linked in the
body text instead, same as the built-in `email` plugin's own convention.

- **Via chat / `hermes send`**: use the standard `MEDIA:<path>` tag in the
  message text — the same mechanism every platform uses. Hermes extracts
  it into `media_files` before dispatch, and `_standalone_send()` attaches
  it.
- **Via direct Python**: `send_image()`, `send_document()`, and
  `send_multiple_images()` all attach local (`file://`) paths natively;
  `send()` accepts `metadata={"attachments": [...]}` for the same effect.
- **Size limit**: the Email API caps the whole request (JSON + base64
  attachments) at 10 MB. This plugin refuses locally above ~7 MB of raw
  attachment bytes (leaving headroom for base64's ~4/3 inflation plus JSON
  overhead) rather than let a near-the-limit send 400 server-side.
- **No inline images.** The API's `attachments[].cid` field (for
  `<img src="cid:...">` references inside HTML) isn't wired up yet — every
  attachment arrives as a regular download, not inline in the body.

## Limitations

- **Outbound only.** No inbound polling or webhook — this plugin can't
  receive replies. (Hermes's separate, unrelated built-in `email` gateway
  plugin does generic personal-mailbox IMAP/SMTP; this plugin has nothing
  to do with that one.)
- **One recipient per `send()` call.** Sending to a list of N people is N
  separate `hermes send` invocations, not one batched request. Each
  recipient still only ever sees their own address — there's no CC-all
  leak — it's just not a single API call for the whole list.
- **No cc/bcc, no scheduled send.** The Email API supports both
  (`schedule.sendAt` for future delivery), but this plugin doesn't expose
  either yet — their exact request shape needs confirming against a live
  account before wiring them up.
- **Plain text only via `hermes send`/cron.** HTML is only reachable
  through the direct-Python `metadata={"html": True}` escape hatch on
  `send()` described above — there's no CLI or agent path to it today.
- **202 means queued, not delivered** — see **Architecture** above.

## Architecture notes

- `TwilioEmailAdapter.connect()`/`disconnect()` are readiness checks only
  (`_mark_connected`/`_mark_disconnected`) — there's nothing to actually
  hold open for an outbound-only channel. `connect()` fails fast (fatal,
  non-retryable) if the Twilio credentials or `TWILIO_EMAIL_FROM` are
  missing, rather than waiting for the first `send()` to hit a 401/403.
- `send()`, `send_image()`, `send_document()`, and `send_multiple_images()`
  all funnel through one shared `_send_email_request()` helper that builds
  the request and interprets the response — kept private to the adapter
  instance since `_standalone_send()` is deliberately a separate,
  self-contained module-level function (same split the `sms` plugin uses
  between its live adapter and its standalone sender).
- `_standalone_send()` is the primary delivery path in practice, since
  `hermes send` and cron jobs usually run in a separate process from any
  live gateway.
- Auth and request construction are confirmed against the live API: a
  fake Account SID gets a clean `401` with Twilio's standard error
  envelope (`{"code":20003,"message":"Authentication Error - invalid
  username",...}`) — see the `@pytest.mark.integration` tests in
  `tests/plugins/platforms/twilio_email/test_adapter.py`.
- `register(ctx)` in `adapter.py` wires everything into
  `gateway.platform_registry` — no core Hermes files were touched to add
  this plugin.

## Files

```
twilio_email/
  __init__.py    # re-exports register() for plugin discovery
  plugin.yaml    # kind: platform, env var declarations
  adapter.py     # TwilioEmailAdapter, _standalone_send, register(ctx)
  README.md      # this file
```
