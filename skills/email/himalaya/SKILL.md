---
name: himalaya
description: "Himalaya CLI: IMAP/SMTP email from terminal."
version: 1.1.0
author: community
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Email, IMAP, SMTP, CLI, Communication]
    homepage: https://github.com/pimalaya/himalaya
prerequisites:
  commands: [himalaya]
---

# Himalaya Email CLI

Himalaya is a CLI email client that lets you manage emails from the terminal using IMAP, SMTP, Notmuch, or Sendmail backends.

## When to use this skill

Use it when the user asks the agent to operate their mailbox from the terminal:
list/search/read email, reply, forward, compose and send, move or delete
messages, manage flags, or download attachments — over IMAP/SMTP (or Notmuch).

This skill is separate from the Hermes Email gateway adapter. The gateway
adapter lets people email the agent and uses Hermes' built-in IMAP/SMTP
adapter; this skill lets the agent operate a mailbox from terminal tools and
requires the external `himalaya` CLI.

For a Google account that also needs Calendar/Drive/Sheets/Docs, use the
`google-workspace` skill instead. For email only, himalaya + a Gmail App
Password is the faster path.

## Routing table — read the reference before you act

| To do this | Read |
|---|---|
| Install himalaya, write `config.toml`, set up IMAP/SMTP auth, Gmail/iCloud/Notmuch/OAuth2 settings, folder aliases, signatures | `references/configuration.md` |
| Look up any command: folders, envelope list/search, read, reply, forward, write, move/copy, delete, flags, accounts, attachments, output formats, debugging | `references/command-reference.md` |
| Compose rich mail with MML — multipart, attachments, inline images, header syntax | `references/message-composition.md` |

## Prerequisites

1. Himalaya CLI installed (`himalaya --version` to verify)
2. A configuration file at `~/.config/himalaya/config.toml`
3. IMAP/SMTP credentials configured (password stored securely)

Both 1 and 2 are covered step by step in `references/configuration.md`.

## Red lines

**Credentials**
- **Never store a raw password in `config.toml` for a real account.** Use
  `backend.auth.cmd` (e.g. `pass show email/imap`) or the system keyring.
  `backend.auth.raw` is for throwaway testing only.
- Never print, `cat`, or summarize `config.toml`, the output of the
  `backend.auth.cmd` command, or any keyring value into the conversation or logs.
- Never ask the user to paste their email password or App Password into chat —
  have them store it in `pass` / the keyring themselves.
- Gmail and iCloud need an app-specific password when 2FA is on; that is still a
  secret and must go through `auth.cmd`/keyring.
- Don't use `RUST_LOG=debug|trace` on a command that carries credentials unless
  the user asks, and never paste that output back verbatim.

**Confirm before sending**
- **Never send, reply to, or forward an email without showing the user the exact
  From, To/Cc, Subject, and body first, and getting explicit approval.** Resolve
  recipients from a real message you read — never from a guessed address.
- Send exactly once. `himalaya message send` can exit non-zero *after* SMTP
  delivery already succeeded (see the folder-alias trap below) — do **not** blind
  retry on a non-zero exit, or recipients get duplicate emails. Verify what
  actually happened first.

**Confirm before destroying or moving user mail**
- **`himalaya message delete`, `message move`, and flag changes mutate the user's
  real mailbox.** Show the message (ID, from, subject) and get explicit approval
  before deleting, moving/archiving, or clearing flags. Deletion is not reversible
  beyond whatever the server's Trash offers.
- Message IDs are relative to the current folder — re-list before acting so you
  never operate on a stale ID.

**Folder alias trap (must-know)**
- Always use the plural, dotted `folder.aliases.X` keys directly under
  `[accounts.NAME]`. The pre-v1.2.0 singular `[accounts.NAME.folder.alias]`
  sub-section is silently ignored by v1.2.0: TOML parses fine, the resolver never
  reads it, and every lookup falls through to the canonical name. On Gmail
  (`sent` is really `[Gmail]/Sent Mail`) save-to-Sent then fails *after* SMTP
  delivery succeeded and `message send` exits non-zero — the duplicate-email
  hazard above. Details and the Gmail mapping: `references/configuration.md`.

**Rate/volume**
- Don't loop over large mailboxes with one command per message; page with
  `--page`/`--page-size` and keep IMAP round-trips modest. Providers throttle
  aggressive IMAP/SMTP clients and may temporarily lock the account.

## Minimal end-to-end skeleton

```bash
# 1. Prereq gate
himalaya --version
himalaya account list          # confirms config.toml is readable

# 2. Read first — JSON is easiest to parse
himalaya envelope list --output json --page-size 10
himalaya message read 42

# 3. Show the user the full draft, get approval, THEN send exactly once.
cat << 'EOF' | himalaya template send
From: you@example.com
To: sender@example.com
Subject: Re: Original Subject
In-Reply-To: <original-message-id>

Your reply here.
EOF
```

Everything else — search, forward, move, delete, flags, attachments, MML
attachments — is in the two command/composition references above.
