# SimpleX Chat

[SimpleX Chat](https://simplex.chat/) is a private, decentralised messaging platform where users own their contacts and groups. SimpleX has no global user identifiers; the local daemon assigns each connection an opaque numeric `contactId`. Hermes uses that identity-local, rename-stable ID for authorization.

> Run `hermes gateway setup` and pick **SimpleX** for a guided walk-through.

## Prerequisites

- The **simplex-chat** CLI installed and running as a daemon
- Python package **websockets** (`pip install websockets`)

## Install simplex-chat

Download the latest release from the [simplex-chat GitHub releases](https://github.com/simplex-chat/simplex-chat/releases) page:

```bash
# Linux / macOS binary
curl -L https://github.com/simplex-chat/simplex-chat/releases/latest/download/simplex-chat-ubuntu-22_04-x86_64 -o simplex-chat
chmod +x simplex-chat
```

The SimpleX Chat project does not publish a prebuilt Docker image for the chat client; to run it under Docker, build from source from the [simplex-chat repository](https://github.com/simplex-chat/simplex-chat).

## Start the daemon

```bash
install -d -m 0700 /absolute/path/simplex/files /absolute/path/simplex/temp
simplex-chat -p 5225 \
  --files-folder /absolute/path/simplex/files \
  --temp-folder /absolute/path/simplex/temp
```

The daemon listens on WebSocket at `ws://127.0.0.1:5225` by default. Keep the
files and temporary folders on the same filesystem: SimpleX completes an XFTP
download with a filesystem rename.

## Configure Hermes

### Via setup wizard

```bash
hermes gateway setup
```

Select **SimpleX Chat** and follow the prompts.

### Via environment variables

Add these to `~/.hermes/.env`:

```
SIMPLEX_WS_URL=ws://127.0.0.1:5225
SIMPLEX_FILES_FOLDER=/absolute/path/simplex/files
SIMPLEX_ALLOWED_USERS=<contact-id-1>,<contact-id-2>
SIMPLEX_HOME_CHANNEL=<contact-id>
SIMPLEX_AUTO_ACCEPT=false
```

| Variable | Required | Description |
|---|---|---|
| `SIMPLEX_WS_URL` | Yes | WebSocket URL of the simplex-chat daemon |
| `SIMPLEX_FILES_FOLDER` | Required for inbound files | Exact absolute path passed to `simplex-chat --files-folder`. Keep it on the same filesystem as the daemon's `--temp-folder`; SimpleX completes XFTP downloads with a filesystem rename. |
| `SIMPLEX_ALLOWED_USERS` | Recommended | Comma-separated numeric `contactId` allowlist. Display names are mutable labels and are not authorization identities. |
| `SIMPLEX_ALLOW_ALL_USERS` | Optional | Set `true` to allow every contact (use carefully) |
| `SIMPLEX_AUTO_ACCEPT` | Optional | Auto-accept incoming contact requests (default: `true`). Keep this `false` for production identities unless unattended enrollment is intentional. |
| `SIMPLEX_GROUP_ALLOWED` | Optional | Comma-separated group IDs the bot participates in, or `*` for any group. Omit to ignore group messages entirely |
| `SIMPLEX_HOME_CHANNEL` | Optional | Default contact/group ID for cron job delivery |
| `SIMPLEX_HOME_CHANNEL_NAME` | Optional | Human label for the home channel |
| `HERMES_SIMPLEX_TEXT_BATCH_DELAY` | Optional | Quiet-period seconds (default: `0.8`) used to concatenate rapid-fire inbound text messages into one event |
| `platforms.simplex.extra.files_folder` | Alternative to `SIMPLEX_FILES_FOLDER` | Absolute daemon `--files-folder` path used to resolve relative received-file paths |
| `platforms.simplex.extra.file_transfer_timeout` | Optional | Seconds before a stalled inbound transfer is discarded and its caption is delivered without the attachment (default: `300`) |
| `platforms.simplex.extra.retain_received_files` | Optional | Keep adapter-created inbound transfer files after the turn. Defaults to `false`; unrelated daemon/user files are never removed. |
| `platforms.simplex.extra.media_cleanup_timeout` | Optional | TTL backstop for adapter-created inbound files and converted outbound previews (default: `3600`, minimum: `60`) |

## Find your contact ID

After starting the daemon, open a conversation with your agent contact and run `/contacts` in the SimpleX CLI. Copy the numeric `contactId`. A display name shown in the UI may be renamed or collide with another contact, so Hermes deliberately ignores display-name entries in `SIMPLEX_ALLOWED_USERS` and logs a migration warning.

> **Upgrade note:** Older adapter builds accepted display-name entries in
> `SIMPLEX_ALLOWED_USERS`. They no longer authorize direct messages. Replace
> every such entry with the numeric `contactId` from `/contacts` before
> restarting an existing deployment.

## Authorization

By default **all contacts are denied**. You must either:

1. Set `SIMPLEX_ALLOWED_USERS` to a comma-separated list of numeric `contactId`s (for example, `SIMPLEX_ALLOWED_USERS=4,7`), or
2. Use **DM pairing** — send any message to the bot and it will reply with a pairing code. Enter that code via `hermes pairing approve simplex <CODE>`.

## Group chats

By default the adapter ignores group messages — a bot in a group otherwise
processes every member's traffic. Opt-in explicitly:

```
SIMPLEX_GROUP_ALLOWED=12,34          # specific group IDs
# or
SIMPLEX_GROUP_ALLOWED=*              # any group the bot is in
```

Group messages still pass the sender allowlist. Hermes uses a member's numeric
`memberContactId` when the group member is also a direct contact. For an
unlinked member, add that membership's exact opaque `memberId` to
`SIMPLEX_ALLOWED_USERS`; a display name never authorizes either form.

Address groups by prefixing the chat ID with `group:`, e.g.
`simplex:group:12` as a cron `deliver=` target or in a `hermes send` call.

## Sending with `hermes send`

SimpleX works as a standalone send target — the daemon must be running,
but a live gateway is not required for plain text:

```bash
hermes send --to simplex:4 "hello"              # DM by numeric contactId
hermes send --to simplex:group:12 "hello"       # group by numeric ID
hermes send --to simplex "hello"                # SIMPLEX_HOME_CHANNEL
```

While the gateway is running, the adapter enumerates your contacts and
allowed groups into the channel directory (refreshed every 5 minutes), so
`hermes send --list` shows them by name. Before the first gateway run the
platform still appears in `--list` with a "no channels discovered yet"
hint — direct targets like the ones above work regardless.

## Attachments

The adapter supports native SimpleX attachments in both directions:

- **Inbound** — attachments from authorized senders are accepted via
  the daemon's XFTP flow (`rcvFileDescrReady` → `/freceive` → wait for
  `rcvFileComplete`) and surfaced as `MessageEvent.media_urls` with the
  appropriate `MessageType` (`PHOTO`, `VOICE`, or `DOCUMENT`). Unknown senders
  cannot trigger a download. Cancelled, failed, or timed-out transfers release
  their state and preserve any text caption without presenting a missing file.
  A caption-less failure becomes an explicit attachment-unavailable notice.
  By default Hermes deletes only the unique receive path it created after the
  consuming turn, with a TTL backstop for dropped/abandoned turns. Set
  `retain_received_files: true` only when persistent local copies are wanted.
- **Outbound** — `send_image_file`, `send_voice`, `send_document`, and
  `send_video` all use the structured `/_send` form with `filePath`, so
  the receiving SimpleX client renders images inline and plays voice
  notes inline rather than offering them as downloads.

Agent replies can also embed `MEDIA:/path/to/file` tags in plain text —
the adapter strips the tag from the body and sends the file as either a
voice note (audio extensions) or a document.

## Using SimpleX with cron jobs

```python
cronjob(
    action="create",
    schedule="every 1h",
    deliver="simplex",          # uses SIMPLEX_HOME_CHANNEL
    prompt="Check for alerts and summarise."
)
```

Or target a specific contact via the cron job's `deliver:` field, or from a shell script with the [`hermes send` CLI](/guides/pipe-script-output):

```bash
hermes send simplex:<contact-id> "Done!"
```

## Privacy notes

- SimpleX never reveals phone numbers or email addresses — contacts use opaque IDs
- The connection between Hermes and the daemon is local WebSocket (`ws://127.0.0.1:5225`) — no data leaves your machine
- Messages are end-to-end encrypted by the SimpleX protocol before reaching the daemon
- For a long-lived production identity, disable automatic contact acceptance and approve new contacts deliberately

## Troubleshooting

**"Cannot reach daemon"** — Ensure `simplex-chat -p 5225` is running and the port matches `SIMPLEX_WS_URL`.

**"websockets not installed"** — Run `pip install websockets`.

**Messages not received** — Check that the contact's ID is in `SIMPLEX_ALLOWED_USERS` or approve them via DM pairing.
