# Hermes Signal Note-to-Self Design

Date: 2026-07-29

## Objective

Connect the existing Hermes installation to the operator's existing Signal
account without replacing the phone as the primary device. Hermes will respond
only in the operator's Signal Note-to-Self conversation and will start
automatically after Windows login.

## Decisions

- Link Hermes as a secondary Signal device named `HermesAgent`.
- Use the `bbernhard/signal-cli-rest-api` container image as the maintained
  signal-cli runtime package.
- Run the image's bundled signal-cli in native HTTP daemon mode because the
  installed Hermes adapter consumes `/api/v1/check`, `/api/v1/events`, and
  `/api/v1/rpc`, not the wrapper's `/v1/receive` and `/v2/send` interface.
- Do not patch or fork Hermes' Signal adapter.
- Bind the Signal daemon to `127.0.0.1` only.
- Allow only the operator's own Signal identifier.
- Leave Signal group access disabled.
- Start the bridge automatically through Docker restart policy and start the
  Hermes messaging gateway through its supported Windows Scheduled Task.

## Components

### Standalone bridge

Create a small infrastructure unit at:

`D:\AI-Foundry\Infrastructure\hermes-signal-bridge`

It owns:

- a Docker Compose definition;
- a minimal container launcher for the bundled signal-cli daemon;
- a local environment file containing the operator's E.164 Signal number;
- persistent Signal device data;
- a short operator README with start, stop, health, relink, and recovery
  commands.

The unit remains outside the Hermes source tree because it is host
infrastructure, not a Hermes product change.

### Hermes configuration

Configure the active Hermes home at:

`D:\AI-Foundry\Infrastructure\hermes\.hermes`

with:

- `SIGNAL_HTTP_URL=http://127.0.0.1:8080`
- `SIGNAL_ACCOUNT` set to the operator's E.164 Signal number
- `SIGNAL_ALLOWED_USERS` set to that same E.164 Signal number
- `SIGNAL_ALLOW_ALL_USERS=false`

Do not set `SIGNAL_GROUP_ALLOWED_USERS`. Its absence keeps all Signal group
messages disabled.

### Automatic startup

The bridge container uses a restart policy so Docker restores it with Docker
Desktop. Docker Desktop must start at Windows login.

Hermes uses `hermes gateway install`, which creates the supported Windows
Scheduled Task. The gateway reconnects to the local Signal daemon after both
services are available. Startup verification must confirm that the task and
container recover after a restart rather than merely proving a foreground
session.

## Linking Flow

1. Start a temporary linking instance using the same persistent Signal data
   location as the final daemon.
2. Open the local QR-link endpoint.
3. The operator scans the QR code from Signal on the phone under
   **Settings > Linked Devices > Link New Device**.
4. Confirm that the linked account appears in the persistent Signal data.
5. Stop the temporary linking instance.
6. Start the native HTTP daemon using the linked account and the same data.

The phone remains the primary Signal device. Re-linking is required only if the
operator removes the linked device, Signal invalidates it, or the persistent
device data is lost.

## Message Flow

1. The operator sends a message to Signal Note to Self.
2. Signal synchronizes it to the linked `HermesAgent` device.
3. signal-cli emits the message through its local event stream.
4. Hermes' Signal adapter authenticates the sender against the single-entry
   allowlist and opens or resumes the Signal-scoped Hermes session.
5. Hermes uses the existing Anthropic Opus 5 primary route and the configured
   OpenAI Codex fallback.
6. Hermes sends the response through signal-cli to the same Note-to-Self
   conversation.
7. Hermes' existing sent-timestamp tracking filters the linked-device echo so
   the response cannot recursively trigger itself.

## Security Boundaries

- Only loopback traffic can reach the Signal daemon.
- Only the operator's Signal identifier is authorized.
- Pairing for unknown direct-message senders is not used because an explicit
  allowlist is present.
- Signal groups remain disabled.
- The persistent Signal data contains account credentials and must remain
  local, untracked, and readable only by the operator and its container.
- Phone-number values and Signal device data must never be committed.
- The existing `.install_method` file and unrelated Hermes worktree changes
  remain untouched.

Hermes' Signal toolset includes terminal access. The allowlist and loopback
binding are therefore required security controls, not optional convenience
settings.

## Failure Handling

- If Docker is unavailable, the Hermes gateway stays running but reports the
  Signal adapter unavailable and retries according to its normal reconnect
  behavior.
- If the linked device expires, stop the daemon and repeat only the linking
  flow against the preserved data location.
- If port 8080 is occupied, choose another loopback port and update both the
  bridge mapping and `SIGNAL_HTTP_URL`.
- Never run two signal-cli listeners for the same linked account. Hermes and
  signal-cli both guard against duplicate processing, but duplicate daemons can
  still consume or duplicate inbound events.
- Do not expose the bridge port to the LAN as a troubleshooting shortcut.

## Verification

Completion requires all of the following:

1. The bridge health endpoint returns HTTP 200.
2. The linked account is visible to signal-cli.
3. Hermes reports the Signal adapter running.
4. A Note-to-Self message receives one Hermes response.
5. A Hermes response does not trigger an echo loop.
6. Signal group messages are ignored.
7. The daemon is reachable only through loopback.
8. After restarting the bridge and Hermes gateway, another Note-to-Self
   round trip succeeds.
9. The Docker container and Hermes Scheduled Task are configured for automatic
   startup.

## Non-Goals

- Allowing contacts or unknown users to message Hermes.
- Responding in Signal groups.
- Registering a separate bot phone number.
- Exposing the Signal API outside the Windows host.
- Modifying Hermes' Signal adapter or adding a new provider.
- Adding voice transcription or text-to-speech configuration as part of this
  setup.
