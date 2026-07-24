# Telephony — Install, Provider Setup, and Persistent State

## Install

This is an official optional skill, so install it from the Skills Hub:

```bash
hermes skills search telephony
hermes skills install official/productivity/telephony
```
## Provider setup

### Twilio — owned number, SMS/MMS, direct calls, inbound SMS polling

Sign up at:
- https://www.twilio.com/try-twilio

Then save credentials into Hermes:

```bash
python3 "$SCRIPT" save-twilio ACXXXXXXXXXXXXXXXXXXXXXXXXXXXX your_auth_token_here
```

Search for available numbers:

```bash
python3 "$SCRIPT" twilio-search --country US --area-code 702 --limit 5
```

Buy and remember a number:

```bash
python3 "$SCRIPT" twilio-buy "+17025551234" --save-env
```

List owned numbers:

```bash
python3 "$SCRIPT" twilio-owned
```

Set one of them as the default later:

```bash
python3 "$SCRIPT" twilio-set-default "+17025551234" --save-env
# or
python3 "$SCRIPT" twilio-set-default PNXXXXXXXXXXXXXXXXXXXXXXXXXXXX --save-env
```

### Bland.ai — easiest outbound AI calling

Sign up at:
- https://app.bland.ai

Save config:

```bash
python3 "$SCRIPT" save-bland your_bland_api_key --voice mason
```

### Vapi — better conversational voice quality

Sign up at:
- https://dashboard.vapi.ai

Save the API key first:

```bash
python3 "$SCRIPT" save-vapi your_vapi_api_key
```

Import your owned Twilio number into Vapi and persist the returned phone number ID:

```bash
python3 "$SCRIPT" vapi-import-twilio --save-env
```

If you already know the Vapi phone number ID, save it directly:

```bash
python3 "$SCRIPT" save-vapi your_vapi_api_key --phone-number-id vapi_phone_number_id_here
```
## Files and persistent state

The skill persists telephony state in two places:

### `${HERMES_HOME:-~/.hermes}/.env`
Used for long-lived provider credentials and owned-number IDs, for example:
- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_PHONE_NUMBER`
- `TWILIO_PHONE_NUMBER_SID`
- `BLAND_API_KEY`
- `VAPI_API_KEY`
- `VAPI_PHONE_NUMBER_ID`
- `PHONE_PROVIDER` (AI call provider: bland or vapi)

### `~/.hermes/telephony_state.json`
Used for skill-only state that should survive across sessions, for example:
- remembered default Twilio number / SID
- remembered Vapi phone number ID
- last inbound message SID/date for inbox polling checkpoints

This means:
- the next time the skill is loaded, `diagnose` can tell you what number is already configured
- `twilio-inbox --since-last --mark-seen` can continue from the previous checkpoint

