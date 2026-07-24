# Telephony — Provider Decision Tree

Use this logic instead of hardcoded provider routing:

### 1) "I want Hermes to own a real phone number"
Use **Twilio**.

Why:
- easiest path to buying and keeping a number
- best SMS / MMS support
- simplest inbound SMS polling story
- cleanest future path to inbound webhooks or call handling

Use cases:
- receive texts later
- send deployment alerts / cron notifications
- maintain a reusable phone identity for the agent
- experiment with phone-based auth flows later

### 2) "I only need the easiest outbound AI phone call right now"
Use **Bland.ai**.

Why:
- quickest setup
- one API key
- no need to first buy/import a number yourself

Tradeoff:
- less flexible
- voice quality is decent, but not the best

### 3) "I want the best conversational AI voice quality"
Use **Twilio + Vapi**.

Why:
- Twilio gives you the owned number
- Vapi gives you better conversational AI call quality and more voice/model flexibility

Recommended flow:
1. Buy/save a Twilio number
2. Import it into Vapi
3. Save the returned `VAPI_PHONE_NUMBER_ID`
4. Use `ai-call --provider vapi`

### 4) "I want to call with a custom prerecorded voice message"
Use **Twilio direct call** with a public audio URL.

Why:
- easiest way to play a custom MP3
- pairs well with Hermes `text_to_speech` plus a public file host or tunnel

