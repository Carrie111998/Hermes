# Hermes Agent integration

Hermes Agent can use Sonar encrypted DMs as a **native gateway platform** (plugin in `hermes-agent`, not in this repo).

## For Hermes users

1. Install/build `sonar-cli` from this repository.
2. `sonar-cli init && sonar-cli publish` with `SONAR_CLI_HOME=~/.sonar-agent`.
3. Enable in Hermes: `gateway.platforms.sonar` (see Hermes PR / `plugins/platforms/sonar/README.md`).

## Stable CLI contract (do not break without versioning)

`sonar-cli listen` emits one JSON object per line:

| Field | Notes |
|-------|--------|
| `type` | `"message"` for inbound DMs |
| `sender` | npub (**not** `from`) |
| `content` | body (**not** `text`) |
| `id` | dedupe key |
| `mine` | skip when true |

Send: `sonar-cli send --to <npub> --text "..."`

## What belongs in this repo vs Hermes

| bitchat-to-sonar | hermes-agent |
|------------------|--------------|
| sonar-cli, protocol, apps | `plugins/platforms/sonar/` |
| This doc + CLI reference | `hermes gateway`, cron `deliver=sonar` |

Optional: minimal `examples/echo-bridge` (agent-agnostic) to demonstrate listen/send without Hermes.