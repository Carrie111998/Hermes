# Telephony — Common Workflows

### A. Buy an agent number and keep using it later

1. Save Twilio credentials:
```bash
python3 "$SCRIPT" save-twilio AC... auth_token_here
```

2. Search for a number:
```bash
python3 "$SCRIPT" twilio-search --country US --area-code 702 --limit 10
```

3. Buy it and save it into `${HERMES_HOME:-~/.hermes}/.env` + state:
```bash
python3 "$SCRIPT" twilio-buy "+17025551234" --save-env
```

4. Next session, run:
```bash
python3 "$SCRIPT" diagnose
```
This shows the remembered default number and inbox checkpoint state.

### B. Send a text from the agent number

```bash
python3 "$SCRIPT" twilio-send-sms "+15551230000" "Your deployment completed successfully."
```

With media:

```bash
python3 "$SCRIPT" twilio-send-sms "+15551230000" "Here is the chart." --media-url "https://example.com/chart.png"
```

### C. Check inbound texts later with no webhook server

Poll the inbox for the default Twilio number:

```bash
python3 "$SCRIPT" twilio-inbox --limit 20
```

Only show messages that arrived after the last checkpoint, and advance the checkpoint when you're done reading:

```bash
python3 "$SCRIPT" twilio-inbox --since-last --mark-seen
```

This is the main answer to “how do I access messages the number receives next time the skill is loaded?”

### D. Make a direct Twilio call with built-in TTS

```bash
python3 "$SCRIPT" twilio-call "+15551230000" --message "Hello! This is Hermes calling with your status update." --voice Polly.Joanna
```

### E. Call with a prerecorded / custom voice message

This is the main path for reusing Hermes's existing `text_to_speech` support.

Use this when:
- you want the call to use Hermes's configured TTS voice rather than Twilio `<Say>`
- you want a one-way voice delivery (briefing, alert, joke, reminder, status update)
- you do **not** need a live conversational phone call

Generate or host audio separately, then:

```bash
python3 "$SCRIPT" twilio-call "+155****0000" --audio-url "https://example.com/briefing.mp3"
```

Recommended Hermes TTS -> Twilio Play workflow:

1. Generate the audio with Hermes `text_to_speech`.
2. Make the resulting MP3 publicly reachable.
3. Place the Twilio call with `--audio-url`.

Example agent flow:
- Ask Hermes to create the message audio with `text_to_speech`
- If needed, expose the file with a temporary static host / tunnel / object storage URL
- Use `twilio-call --audio-url ...` to deliver it by phone

Good hosting options for the MP3:
- a temporary public object/storage URL
- a short-lived tunnel to a local static file server
- any existing HTTPS URL the phone provider can fetch directly

Important note:
- Hermes TTS is great for prerecorded outbound messages
- Bland/Vapi are better for **live conversational AI calls** because they handle the real-time telephony audio stack themselves
- Hermes STT/TTS alone is not being used here as a full duplex phone conversation engine; that would require a much heavier streaming/webhook integration than this skill is trying to introduce

### F. Navigate a phone tree / IVR with Twilio direct calling

If you need to press digits after the call connects, use `--send-digits`.
Twilio interprets `w` as a short wait.

```bash
python3 "$SCRIPT" twilio-call "+18005551234" --message "Connecting to billing now." --send-digits "ww1w2w3"
```

This is useful for reaching a specific menu branch before handing off to a human or delivering a short status message.

### G. Outbound AI phone call with Bland.ai

```bash
python3 "$SCRIPT" ai-call "+15551230000" "Call the dental office, ask for a cleaning appointment on Tuesday afternoon, and if they do not have Tuesday availability, ask for Wednesday or Thursday instead." --provider bland --voice mason --max-duration 3
```

Check status:

```bash
python3 "$SCRIPT" ai-status <call_id> --provider bland
```

Ask Bland analysis questions after completion:

```bash
python3 "$SCRIPT" ai-status <call_id> --provider bland --analyze "Was the appointment confirmed?,What date and time?,Any special instructions?"
```

### H. Outbound AI phone call with Vapi on your owned number

1. Import your Twilio number into Vapi:
```bash
python3 "$SCRIPT" vapi-import-twilio --save-env
```

2. Place the call:
```bash
python3 "$SCRIPT" ai-call "+15551230000" "You are calling to make a dinner reservation for two at 7:30 PM. If that is unavailable, ask for the nearest time between 6:30 and 8:30 PM." --provider vapi --max-duration 4
```

3. Check result:
```bash
python3 "$SCRIPT" ai-status <call_id> --provider vapi
```

## Verification checklist

After setup, you should be able to do all of the following with just this skill:

1. `diagnose` shows provider readiness and remembered state
2. search and buy a Twilio number
3. persist that number to `${HERMES_HOME:-~/.hermes}/.env`
4. send an SMS from the owned number
5. poll inbound texts for the owned number later
6. place a direct Twilio call
7. place an AI call via Bland or Vapi
