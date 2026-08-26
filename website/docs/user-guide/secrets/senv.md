# Messenger `/senv`

`/senv` (secure env) lets you send a secret from Telegram, Discord, Slack, or any other gateway chat **without putting the value in the model, session transcript, or tool pipeline**. The gateway intercepts the command before the agent runs.

Local interactive users should still prefer the masked secure prompt in the CLI, TUI, or Desktop app. A [vault](./index) (1Password, Bitwarden) is better than storing a password in `.env` at all. Browser session auth is better than storing an account password.

## When to use `/senv`

- You only have the messenger available and the agent needs a key to continue.
- You want the value written to the **active profile** `.env` (or a skill `.env`) and a value-free confirmation.

## When not to use it

- You can open the CLI/TUI/Desktop and use the masked prompt.
- The secret belongs in 1Password, Bitwarden, or another manager — store it there and let Hermes pull it at startup.
- The flow is a website login — use the browser session, not a stored password.

`/senv` is safer than pasting the secret as a normal chat message. It does **not** protect a compromised phone, a compromised messenger account, notification previews, or platform-side retention before you delete the original message.

## Usage

```text
/senv main BOOKING_PASSWORD=...
/senv OPENROUTER_API_KEY=...
/senv skill travel-manager BOOKING_ACCOUNT_EMAIL=...
/senv delete main BOOKING_PASSWORD
/senv list main
```

- Keys must look like `BOOKING_PASSWORD` (`A-Z`, digits, underscore).
- Values may be quoted. Multi-line values are rejected.
- `list` shows **key names only**.
- `delete` removes a key without printing the old value.
- After a successful set, delete your original messenger message. Hermes does not auto-delete it by default.

The write lands in the **active profile** home (`get_hermes_home()/.env`), including under gateway profile multiplexing. Skill scope writes `{profile}/skills/<name>/.env` only when that skill already exists.

The current process updates `os.environ` for the saved key. If another Hermes process is already running, restart it or run `/reload` in that process.