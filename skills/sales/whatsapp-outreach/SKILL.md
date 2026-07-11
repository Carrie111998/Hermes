---
name: whatsapp-outreach
description: Cold B2B outreach over the WhatsApp Business Platform (Cloud API) with research-first composition, per-country language selection, mandatory user approval before send, media bundles, send-window discipline, and duplicate-send prevention.
version: 1.0.0
author: Interfaze
metadata:
  hermes:
    tags: [sales, whatsapp, outreach, cold-outreach, messaging, approval-flow]
    category: sales
    config:
      - key: sales.whatsapp_daily_limit
        description: Maximum initial-reach messages per day per connected WhatsApp Business number. Start low and ramp; new numbers are flag-prone in the first two weeks.
        default: 30
        prompt: How many WhatsApp outreach messages may be sent per day?
      - key: sales.media_bundle_enabled
        description: Whether an approved reach also sends the company's media bundle (catalogue, product sheets, videos) after the text message.
        default: true
        prompt: Should approved messages include the company media bundle?
      - key: sales.whatsapp_send_windows
        description: Allowed send windows in the RECIPIENT's local time, as comma-separated HH:MM-HH:MM ranges.
        default: "09:00-12:00,13:00-15:00"
        prompt: During which local-time windows may messages be sent?
      - key: sales.recontact_cooldown_days
        description: Minimum days before a contact with no reply may be considered for a new campaign. 0 disables re-contact entirely (initial reach only).
        default: 0
        prompt: After how many days may an unanswered contact be re-approached (0 = never)?
      - key: sales.default_language
        description: Fallback message language when the recipient's country has no mapped language.
        default: en
        prompt: What is the fallback outreach language?
---

# WhatsApp Outreach Skill

Run compliant, human-approved cold outreach to B2B prospects over the
**WhatsApp Business Platform (Cloud API)**. Every message follows the product
flow: generate → user approves → send via the connected WhatsApp Business
integration (`/api/v1/whatsapp/messages/*`). This skill never automates a
personal WhatsApp account or WhatsApp Web session.

## When to Use

- The user asks to reach a lead list or batch of prospects on WhatsApp.
- The user wants outreach messages drafted for approval.
- Tracking replies or opt-outs on previously sent WhatsApp outreach.

In an `outreach_generation` agent run, compose and QA only, then return the
JSON output — the server owns approval and delivery; never call the
generate/approve/send APIs from inside a generation run.

## Prerequisites

- A connected WhatsApp Business integration (`GET /api/v1/integrations/whatsapp`
  shows status; approved templates required for business-initiated sends).
- A lead list with phone numbers and countries.
- Company templates and (optionally) a media bundle registered as company assets.

## Pipeline (per contact)

1. **Filter** — skip any contact already reached, already replied, marked
   opt-out, or excluded by company rules (no-outreach market, wrong segment).
   Re-contact only if `sales.recontact_cooldown_days` > 0, the cooldown has
   elapsed, and there was no reply or salesperson takeover.
2. **Research** — brief web check of the company (what they sell, market fit).
   Record a one-line research note on the lead. Never message blind.
3. **Validate** — phone number in strict E.164 (`+` country code, no spaces,
   no leading zeros). Invalid number → skip, log, move on.
4. **Compose** — use the company's template in the recipient's language
   (see Language Selection). Keep the body short and to the template's
   pattern rules; personalize only from verified research.
5. **Approve** — submit as a generated message (`POST .../messages/generate`)
   and wait for explicit user approval (`.../approve`). **Never send an
   unapproved message.** Batch approval by the user counts, silence does not.
6. **Send** — via the Business API (`.../send`) inside an allowed send window.
   If `sales.media_bundle_enabled`, send text first (it returns the fastest,
   most reliable ack and proves the channel is healthy), then the bundle
   assets one message each, in order, spaced a few seconds apart.
7. **Record** — mark the contact as reached with today's date and the
   message IDs. This record is what step 1 filters on — write it immediately,
   before starting the next contact.

## Language Selection

Message in the recipient's language, chosen by country — never default to
English for markets with a mapped language. The company template file defines
the country → language map and one template variant per language. Signature
block (name, phone, email) stays identical across languages. Greeting adapts
to the recipient's local time of day. Unmapped country →
`sales.default_language`.

## Send-Window Discipline

Send only inside `sales.whatsapp_send_windows`, computed in the **recipient's
timezone** (derive from country), never the sender's. A batch spanning several
countries is scheduled per-country. Respect `sales.whatsapp_daily_limit`;
when a batch exceeds it, queue the remainder for following days rather than
bursting.

## Media Bundle

- The bundle is a fixed, named set of company assets. Send it complete or not
  at all — if any asset is missing, hold the reach and alert the user.
- Large files (long videos) are sent as documents to avoid platform
  re-compression; one asset per send call.
- Bundle sends follow the approved text message; the bundle itself is covered
  by the message approval and needs no separate approval.

## Duplicate-Send Prevention (critical)

Duplicates go to real customers and cannot be recalled. Hard-won rules:

- **A transport/gateway timeout is not a delivery failure.** Large media
  uploads routinely outlive the client's ack window; the platform usually
  completes the send anyway.
- **Never blind-retry after a timeout or ambiguous error.** First verify via
  `GET .../messages/:messageId/status` (or the integration's delivery
  webhook/log) whether the message reached sent/delivered. Retry only on a
  confirmed terminal failure.
- One media file per send call — oversized multi-attachment calls stall and
  deliver partially, which is the main source of ambiguous states.
- Before any reach, re-check the contact's reached-flag (step 1). The flag
  written in step 7 is the last line of defense against double-sends across
  runs and agents.

## Response Tracking

- **No auto-replies, ever.** Inbound replies belong to a human salesperson.
- When the user or a salesperson reports a reply, call
  `POST .../messages/:messageId/mark-replied` and record the date on the lead.
- A contact who replied or was taken over by sales is permanently excluded
  from automated outreach.
- Any opt-out signal ("stop", "remove me", or equivalent in any language) →
  `POST .../messages/:messageId/mark-opt-out` immediately; the contact is
  never messaged again.

## Quality Gates

- Integration unhealthy (`.../test` fails or webhook stale) → pause the whole
  batch and alert the user; do not skip-and-continue.
- Template variables unresolved in a composed body → do not submit for approval.
- Watch delivery/quality-rating feedback from Meta during the first weeks of a
  new number; if quality drops, halve the daily limit and alert the user.

## Pitfalls

- Do not port legacy CLI or WhatsApp Web habits: no session pairing, no
  browser tabs, no personal-number sending. Business Cloud API only.
- Business-initiated conversations require Meta-approved message templates;
  free-form text is only valid inside a 24-hour customer-service window.
  Verify `template_status` on the integration before a campaign.
- "Sent" is not "read". Track replies, not delivery, as the success signal.

## Verification

- After a batch: every processed contact has exactly one reached-record with
  date and message IDs; message statuses are sent/delivered; no contact
  appears twice; all sends fell inside their local send window.
