---
name: cold-email-outreach
description: Research-driven B2B cold email outreach with preflight QA, draft/approved-send modes, CC rules, and reply-aware re-reach limits.
version: 1.0.0
author: Interfaze
metadata:
  hermes:
    tags: [sales, outreach, cold-email, leads, campaigns, b2b]
    category: sales
    config:
      - key: sales.sender_name
        description: Full name of the connected salesperson whose mailbox sends outreach.
        default: ""
        prompt: "What is the sender's full name as it should appear in email signatures?"
      - key: sales.sender_email
        description: Connected salesperson mailbox address (must match the email integration).
        default: ""
        prompt: "Which mailbox address sends outreach emails?"
      - key: sales.sender_phone
        description: Optional signature phone number in E.164 format.
        default: ""
        prompt: "Signature phone number (E.164, e.g. +90...)? Leave empty to omit."
      - key: sales.internal_notify_email
        description: Internal address that receives a post-send research/outreach summary per company.
        default: ""
        prompt: "Which internal address should receive per-company outreach notifications?"
      - key: sales.daily_email_limit
        description: Maximum outreach emails sent per day from the connected mailbox.
        default: "50"
        prompt: "How many cold emails may be sent per day? (Keep conservative for deliverability.)"
      - key: sales.re_reach_days
        description: Minimum days since last contact before a non-responding company may be re-reached.
        default: "30"
        prompt: "After how many days may a silent company be contacted again?"
      - key: sales.send_windows
        description: Allowed send windows in recipient local time, comma-separated HH:MM-HH:MM ranges.
        default: "09:00-12:00,13:00-15:00"
        prompt: "During which recipient-local hours may emails be sent?"
      - key: sales.excluded_industries
        description: Comma-separated industry keywords that disqualify a lead (skip, never send).
        default: "industrial kitchen,commercial kitchen,HORECA,catering equipment,professional kitchen equipment"
        prompt: "Which industries should be excluded from outreach?"
      - key: sales.subject_line
        description: Fixed outreach subject line (canonical language); translated per target language, never personalized.
        default: ""
        prompt: "What is the fixed subject line for outreach emails?"
      - key: sales.default_cc_rule
        description: Default CC rule applied when no market/campaign rule matches.
        default: "market_default"
        prompt: "Default CC rule for outreach sends?"
      - key: sales.default_mode
        description: Default outreach mode, "draft" or "send". Draft is the safe default.
        default: "draft"
        prompt: "Should outreach default to draft mode or approved send mode?"
---

# Cold Email Outreach Skill

Run research-first B2B cold email outreach for the company's leads. Every send
goes through the product's email provider adapter (`create_draft`, `send_email`,
`send_draft`, `list_recent_replies`) on the connected salesperson mailbox —
never through raw SMTP/EWS calls. All lead and message state lives in the
product database (leads, outreach messages, campaigns), never in spreadsheets.

## When to Use

- Generating and sending cold outreach for discovered or custom leads.
- Composing a campaign batch or a single custom-lead email.
- Deciding whether a company may be contacted (exclusions, re-reach rules).

## Modes

- **Generation run (`outreach_generation`):** compose and run preflight QA
  only, then return the JSON output. The server owns approval and delivery —
  never call draft/send/approve APIs from inside a generation run.
- **Draft mode (default):** compose and `create_draft` in the connected
  mailbox; a human reviews and sends. Use unless the message is explicitly
  approved.
- **Approved send mode:** `send_email` only after explicit user approval of the
  exact message. Required for custom-lead cold emails.

## Workflow (per company)

1. **Eligibility check** (query the product database, in order):
   - Skip if the company's country is in the client's `no_outreach_markets`
     (market preferences). Any market not excluded is allowed.
   - Skip if the company was touched on ANY channel (email or WhatsApp) today
     — one channel per customer per day.
   - Skip if industry matches any `sales.excluded_industries` keyword. Record
     the skip reason on the lead (e.g. "SKIP — industrial kitchen").
   - Skip if the company has replied on any channel — a replied thread belongs
     to the salesperson; never auto re-reach it.
   - Skip if last contact was fewer than `sales.re_reach_days` days ago.
   - Skip if today's sends have reached `sales.daily_email_limit`.
2. **Research:** web-research the company and write a short bridge — one
   personalized opening about the customer's market/business, in the target
   language, within the configured length bounds. If research reveals an
   excluded industry, stop and record the skip.
3. **Compose:** fill the company's language-specific template with the bridge.
   - **One language per email.** Greeting through disclaimer in the target
     market's language only. Brand/product names, award names, partner names,
     and technical units stay untranslated.
   - **Fixed subject.** Subject is exactly the translation of
     `sales.subject_line` for the target language — nothing appended or
     prepended.
   - **No internal text.** Research notes, skip markers, and pipeline metadata
     never appear in the customer email.
   - **Never use a double dash (`--`)** anywhere in an email.
4. **Preflight QA gate** (must pass before draft or send — see checklist below).
5. **Recipients:** one send per company. To = primary contact email; CC = all
   other known contact emails for that company plus the CC addresses from the
   matching CC rule (per country/market/product/campaign, falling back to
   `sales.default_cc_rule`).
6. **Timing:** schedule delivery inside `sales.send_windows` in the
   *recipient's* local time zone. Hold messages that fall outside a window.
7. **Deliver:** `create_draft` (draft mode) or `send_email` (approved send
   mode) via the provider adapter.
8. **Log:** record the outreach message in the product database with status,
   channel, recipients, language, and send timestamp. This timestamp drives
   the re-reach rule.
9. **Notify:** send one internal email to `sales.internal_notify_email` —
   subject `[Internal] {Company} — {Country} — researched`, body = research
   summary, what was sent, to which addresses, when. Internal mail may be in
   the team's own language; it is never customer-facing.
10. **Track:** poll `list_recent_replies` and message status to mark replies
    and bounces on the outreach message. Replies freeze the lead for the
    salesperson; bounces mark the contact invalid.

## Preflight QA Checklist

Run against every composed message. Any failure blocks the send and returns
the message to compose.

- **Language purity:** no words or characters from another language (including
  the operator's internal language) anywhere in subject or body; exactly one
  disclaimer, in the target language.
- **Internal-marker scan:** no `[SKIP]`, `[EXCEPTION]`, `[NOT RELEVANT]`, or
  similar pipeline markers.
- **Placeholder scan:** no leftover `{{...}}` tokens, bracketed template
  instructions, or the word "unknown" standing in for a fact.
- **Double-dash scan:** no `--` sequence anywhere.
- **Bridge quality:** bridge within configured length bounds; country/market
  not mentioned redundantly.
- **Subject check:** exactly the fixed subject translation, nothing else.
- **Link check:** every link resolves (HTTP 200) and matches an approved
  company asset; no personal or internal URLs.

## Pitfalls

- Draft mode is the default for a reason — never call `send_email` without an
  explicit approval for that message.
- One send per company: never loop over contact addresses sending copies.
- The re-reach clock resets on every send; a company with any recorded reply
  is permanently off the automated list.
- Respect provider rate limits and the daily cap even mid-campaign; pause and
  resume rather than burst.
- Large inline images (e.g. base64 photo blocks) hurt deliverability; use
  hosted assets referenced by the template pack instead.

## Verification

- The outreach message row exists in the database with correct status
  (`draft`/`sent`), recipients, and timestamp.
- Provider `get_message_status` confirms delivery for sent messages.
- The internal notification email was sent for every researched company,
  including skipped ones.
