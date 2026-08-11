# Briar Platform Plugin

Hermes Agent gateway adapter for Briar, aligned with the official
`briar-headless` REST/WebSocket API.

## What this plugin does

- Registers **Briar** as a messaging platform in Hermes
- Sends messages via `briar-headless` REST (`POST /v1/messages/{contact_id}`)
- Receives messages via `briar-headless` WebSocket (`/v1/ws`,
  `ConversationMessageReceivedEvent`)
- Provides an interactive setup wizard in `hermes setup gateway`

## Requirements

- Hermes Agent already installed and working
- `briar-headless` running and reachable at `BRIAR_API_URL`
- Java Runtime Environment 8+

## Setup

```bash
hermes setup gateway
# Select Briar
# The wizard will:
#   - Try to auto-detect a local briar-headless
#   - Pre-fill API URL and bearer token if found
#   - Let you pick a contact from /v1/contacts
#   - Fall back to manual entry with OS-specific install instructions
```

### Manual configuration

```bash
export BRIAR_API_URL="http://127.0.0.1:7000"
export BRIAR_CONTACT_ID="<contact-id>"
export BRIAR_API_TOKEN="$(cat ~/.briar/auth_token)"
export BRIAR_HOME_CHANNEL="<contact-id>"
export BRIAR_ALLOWED_USERS="<contact-id>,<other-id>"
```

## briar-headless

`briar-headless` is a headless Briar peer. It runs the Briar mesh without a
phone GUI and exposes a local REST/WebSocket API.

Official source: https://code.briarproject.org/briar/briar  
GitHub mirror: https://github.com/briar/briar

Build/run:

```bash
git clone git@code.briarproject.org:briar/briar.git
cd briar
./gradlew --configure-on-demand briar-headless:x86LinuxJar
java -jar briar-headless/build/libs/briar-headless-linux-x86_64.jar
```

First run asks for nickname + password, then listens on `127.0.0.1:7000`.
Data and the bearer token live in `~/.briar/`.

For detailed install steps, see [SETUP.md](./SETUP.md).

## Test

From the Hermes Agent repo root, using the project virtualenv:

```bash
.venv/bin/python -m pytest tests/gateway/test_briar_adapter.py -q
```

If the venv does not have `pytest` yet:

```bash
.venv/bin/python -m pip install pytest pytest-asyncio
```

The Briar adapter tests are mocked — they do not require a running
`briar-headless` instance.

## Contributing

1. Fork `NousResearch/hermes-agent` on GitHub
2. Create a feature branch from `main`
3. Make your changes under `plugins/platforms/briar/`
4. Run the tests above
5. Commit and push to your fork
6. Open a pull request against `NousResearch/hermes-agent:main`

PR target for this plugin: `NousResearch/hermes-agent` `feature/briar-platform`

## Contact

John Wyles / https://github.com/johnwyles / john@johnwyles.com
