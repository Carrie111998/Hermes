---
name: telephony
description: Give Hermes phone capabilities without core tool changes. Provision and persist a Twilio number, send and receive SMS/MMS, make direct calls, and place AI-driven outbound calls through Bland.ai or Vapi.
version: 1.0.0
author: Nous Research
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [telephony, phone, sms, mms, voice, twilio, bland.ai, vapi, calling, texting]
    related_skills: [maps, google-workspace, agentmail]
    category: productivity
---

# Telephony — Numbers, Calls, and Texts without Core Tool Changes

This optional skill gives Hermes practical phone capabilities while keeping telephony out of the core tool list.

It ships with a helper script, `scripts/telephony.py`, that can:
- save provider credentials into `${HERMES_HOME:-~/.hermes}/.env`
- search for and buy a Twilio phone number
- remember that owned number for later sessions
- send SMS / MMS from the owned number
- poll inbound SMS for that number with no webhook server required
- make direct Twilio calls using TwiML `<Say>` or `<Play>`
- import the owned Twilio number into Vapi
- place outbound AI calls through Bland.ai or Vapi

## What this solves

This skill is meant to cover the practical phone tasks users actually want:
- outbound calls
- texting
- owning a reusable agent number
- checking messages that arrive to that number later
- preserving that number and related IDs between sessions
- future-friendly telephony identity for inbound SMS polling and other automations

It does **not** turn Hermes into a real-time inbound phone gateway. Inbound SMS is handled by polling the Twilio REST API. That is enough for many workflows, including notifications and some one-time-code retrieval, without adding core webhook infrastructure.

## Safety rules — mandatory

1. Always confirm before placing a call or sending a text.
2. Never dial emergency numbers.
3. Never use telephony for harassment, spam, impersonation, or anything illegal.
4. Treat third-party phone numbers as sensitive operational data:
   - do not save them to Hermes memory
   - do not include them in skill docs, summaries, or follow-up notes unless the user explicitly wants that
5. It is fine to persist the **agent-owned Twilio number** because that is part of the user's configuration.
6. VoIP numbers are **not guaranteed** to work for all third-party 2FA flows. Use with caution and set user expectations clearly.

## Routing table — read the reference you need

| Intent | Do this |
|---|---|
| "Which provider should I use?" / picking Twilio vs Bland vs Vapi | read `references/decision-tree.md` |
| Install the skill, save provider credentials, buy/import a number, understand persisted state files | read `references/setup-and-state.md` |
| Buy an agent number, send SMS/MMS, poll the inbox, direct calls, prerecorded audio, IVR digits, AI calls, verification checklist | read `references/workflows.md` |

## Locate the helper script

After installing this skill, locate the script like this:

```bash
SCRIPT="$(find ~/.hermes/skills -path '*/telephony/scripts/telephony.py' -print -quit)"
```

If `SCRIPT` is empty, the skill is not installed yet.

## Shortest end-to-end skeleton

```bash
SCRIPT="$(find ~/.hermes/skills -path '*/telephony/scripts/telephony.py' -print -quit)"
python3 "$SCRIPT" diagnose
python3 "$SCRIPT" twilio-send-sms "+15551230000" "Your deployment completed successfully."
```

## Diagnose current state

At any time, inspect what the skill already knows:

```bash
python3 "$SCRIPT" diagnose
```

Use this first when resuming work in a later session.

## Suggested agent procedure

When the user asks for a call or text:

1. Determine which path fits the request via the decision tree.
2. Run `diagnose` if configuration state is unclear.
3. Gather the full task details.
4. Confirm with the user before dialing or texting.
5. Use the correct command.
6. Poll for results if needed.
7. Summarize the outcome without persisting third-party numbers to Hermes memory.

## What this skill still does not do

- real-time inbound call answering
- webhook-based live SMS push into the agent loop
- guaranteed support for arbitrary third-party 2FA providers

Those would require more infrastructure than a pure optional skill.

## Pitfalls

- Twilio trial accounts and regional rules can restrict who you can call/text.
- Some services reject VoIP numbers for 2FA.
- `twilio-inbox` polls the REST API; it is not instant push delivery.
- Vapi outbound calling still depends on having a valid imported number.
- Bland is easiest, but not always the best-sounding.
- Do not store arbitrary third-party phone numbers in Hermes memory.

## References

- Twilio phone numbers: https://www.twilio.com/docs/phone-numbers/api
- Twilio messaging: https://www.twilio.com/docs/messaging/api/message-resource
- Twilio voice: https://www.twilio.com/docs/voice/api/call-resource
- Vapi docs: https://docs.vapi.ai/
- Bland.ai: https://app.bland.ai/
