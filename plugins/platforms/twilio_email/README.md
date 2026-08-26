# Twilio Email platform plugin

This plugin lets Hermes Agent send email through **Twilio's Email API**.
Under the hood, Twilio's Email product is SendGrid — a completely separate
credential surface from core Twilio (SMS, Voice, RCS): a bearer,
`SG.`-prefixed **SendGrid API key**, not `TWILIO_ACCOUNT_SID` /
`TWILIO_AUTH_TOKEN`. If you already have Twilio SMS working on this
agent, that credential will **not** work here — you need a separate
SendGrid API key.

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

## Architecture

Outbound-only, no inbound channel, no webhook, no polling. A message goes
out exactly when something calls `send()` — the `hermes send` CLI (used
directly by a human, or by an agent via its own `terminal` tool, per
above), or a cron job with `deliver=twilio_email`.

```
 hermes send (human or agent's terminal tool)   Twilio Email API
 / cron deliver=twilio_email
┌────────────────────────────────┐   HTTPS    ┌──────────────────┐
│  --to twilio_email:            │  Bearer    │  api.sendgrid.com │
│    customer@x.com              │ ─────────► │  /v3/mail/send     │
│  "Subject\nBody"               │            │  (SendGrid)        │
└────────────────────────────────┘            └─────────┬──────────┘
                                                          │
                                                          ▼
                                                 recipient's inbox
```

Because there's no inbound side, `connect()` is just a readiness check
(is a sender configured?) and `disconnect()` just tears down the HTTP
session — there's no persistent connection to hold open.

## First-time setup

```bash
hermes gateway setup
# select "Twilio Email" from the platform list
```

This prompts for:

1. **SendGrid API key** (`SENDGRID_API_KEY`) — from the SendGrid /
   Twilio Console, an API key with **Mail Send** permission.
2. **Sender email** (`SENDGRID_FROM_EMAIL`) — see **Sender verification**
   below before picking this. Sends from an address SendGrid hasn't
   verified fail with a 403.

If the interactive wizard doesn't cooperate, add the same variables to
`~/.hermes/.env` directly — see **Credentials** below for the exact keys.

## Sender verification

**This is the step people miss, and it fails silently-ish (a 403, not an
obvious "you forgot to verify your sender" message).** Before
`SENDGRID_FROM_EMAIL` will actually send anything, verify it in the
SendGrid dashboard under **Settings → Sender Authentication** — either:

- **Single Sender Verification** — verifies one exact address, fastest to
  set up.
- **Domain Authentication** — verifies an entire domain (any
  `@yourdomain.com` address), the right choice if you'll send from more
  than one address on that domain.

## Credentials

All in `${HERMES_HOME:-~/.hermes}/.env`:

```bash
SENDGRID_API_KEY=SG.xxxxxxxxxxxxxxxxxxxxxxxx
SENDGRID_FROM_EMAIL=you@yourdomain.com
```

## Configuration knobs

All env vars are documented in `plugin.yaml`. The full set:

| Env var                 | Required | Default                          | Meaning |
|--------------------------|----------|-----------------------------------|---------|
| `SENDGRID_API_KEY`       | Yes      | —                                  | SendGrid API key (`SG.`-prefixed). Not the Twilio Account SID/Auth Token. |
| `SENDGRID_FROM_EMAIL`    | Yes      | —                                  | Default sender. Must be a Verified Sender or on an authenticated domain. |
| `SENDGRID_FROM_NAME`     | No       | (none)                             | Sender display name. |
| `SENDGRID_API_BASE`      | No       | `https://api.sendgrid.com/v3`      | Override to a staging host (e.g. a `progenitor`/`go_user`-provisioned test account's staging endpoint) — a staging key does not authenticate against the public API. |
| `SENDGRID_HOME_CHANNEL`  | No       | (none)                             | Default recipient address for cron / notification delivery (`deliver=twilio_email`). |

## Usage

```bash
hermes send --to "twilio_email:customer@example.com" $'Order shipped\nYour package is on its way.'
```

The target-resolution plumbing (`parse_target_ref_fn`/`validate_target_ref_fn`)
is shared across every Twilio channel plugin — see **For AI agents reading
this file** above for how an agent actually reaches this in a live chat
session (it isn't a directly agent-callable tool).

**Subject/body convention** — every other platform this adapter sits
alongside (SMS, RCS, Discord, ...) passes one plain `content` string with
no concept of a subject line. To stay consistent with that shared
interface instead of inventing a special case, this plugin treats **the
first line of `content` as the subject** and everything after the first
newline as the body:

```
Order shipped
Your package is on its way and should arrive Thursday.
```

sends with subject `Order shipped` and that second line as the body. A
single-line message (no newline) gets a generic default subject
("Message from Hermes Agent") and the whole line as the body, rather than
guessing which part was meant to be which.

`send()` also accepts `metadata={"subject": "...", "html": True}` to skip
the first-line convention and optionally send HTML instead of plain text.
**Nothing in this repo calls `send()` with `metadata` today** — not
`hermes send`, not cron delivery, not an agent (there's no agent-callable
send tool at all; see above). This is a live-gateway-only escape hatch for
a caller instantiating `TwilioEmailAdapter` directly in Python. The
out-of-process path that `hermes send` and cron actually use
(`_standalone_send()`) has no `metadata` parameter and always sends plain
text via the first-line convention.

## Limitations

- **Outbound only.** No inbound polling or webhook — this plugin can't
  receive replies. (Hermes's separate, unrelated built-in `email` gateway
  plugin does generic personal-mailbox IMAP/SMTP; this plugin has nothing
  to do with that one.)
- **One recipient per `send()` call.** Sending to a list of N people is N
  separate `hermes send` invocations, not one batched SendGrid request.
  Each recipient still only ever sees their own address — there's no
  CC-all leak — it's just not a single API call for the whole list.
- **Plain text only via `hermes send`/cron.** HTML is only reachable
  through the direct-Python `metadata={"html": True}` escape hatch on
  `send()` described above — there's no CLI or agent path to it today.
- **No attachments yet.**

## Architecture notes

- `TwilioEmailAdapter.connect()`/`disconnect()` are readiness checks only
  (`_mark_connected`/`_mark_disconnected`) — there's nothing to actually
  hold open for an outbound-only channel. `connect()` fails fast (fatal,
  non-retryable) if either `SENDGRID_API_KEY` or `SENDGRID_FROM_EMAIL` is
  missing, rather than waiting for the first `send()` to hit a 401/403.
- `_standalone_send()` is the primary delivery path in practice, since
  `hermes send` and cron jobs usually run in a separate process from any
  live gateway.
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
