# Security & Privacy Toggles

Common "why is Hermes doing X to my output / tool calls / commands?" toggles — and the exact commands to change them. Most of these need a fresh session (`/reset` in chat, or start a new `hermes` invocation) because they're read once at startup.

### Secret redaction in tool output

Secret redaction is **on by default** — tool output (terminal stdout, `read_file`, web content, subagent summaries, etc.) is scanned for strings that look like API keys, tokens, and secrets before it enters the conversation context and logs. Leave it enabled for normal use:

```bash
hermes config set security.redact_secrets true       # keep enabled globally
```

**Restart required.** `security.redact_secrets` is snapshotted at import time — toggling it mid-session (e.g. via `export HERMES_REDACT_SECRETS=false` from a tool call) will NOT take effect for the running process. Tell the user to change it in config from a terminal, then start a new session. This is deliberate — it prevents an LLM from flipping the toggle on itself mid-task.

Disable only when you deliberately need raw credential-like strings for debugging or redactor development:
```bash
hermes config set security.redact_secrets false
```

### PII redaction in gateway messages

Separate from secret redaction. When enabled, the gateway hashes user IDs and strips phone numbers from the session context before it reaches the model:

```bash
hermes config set privacy.redact_pii true    # enable
hermes config set privacy.redact_pii false   # disable (default)
```

### Exact terminal authorization

Hermes does not classify command text with keywords, regular expressions, or a semantic router. The model is the sole semantic authority. The runtime verifies only structural authority: an isolated backend, an exact once-only owner capability, an exact bounded-plan capability, an explicit whole-surface session/cron grant, or `approvals.mode: off`.

By default (`approvals.mode: manual`), Hermes asks the owner before issuing an exact terminal capability. The modes are:

- `manual` — require structural authority for each exact invocation (default)
- `off` — skip the owner prompt while retaining invocation-bound, non-replayable capabilities (equivalent to `--yolo`)

```bash
hermes config set approvals.mode manual      # owner-driven exact authorization
hermes config set approvals.mode off         # bypass the owner prompt (not recommended)
```

Legacy `smart` values are migrated to `manual`; no auxiliary model can grant
or deny authorization.

Per-invocation bypass without changing config:
- `hermes --yolo …`
- `export HERMES_YOLO_MODE=1`

Note: YOLO / `approvals.mode: off` does NOT turn off secret redaction. They are independent.

### "Reset permissions" / "make Hermes ask again"

There is no accumulated terminal command-pattern grant to clear. Exact terminal capabilities are consumed after one use, expire, and are bound to their session epoch. To restore owner prompts, confirm `hermes config get approvals.mode` is `manual`, remove `--yolo` from the launch command, and start a fresh session to invalidate any still-pending capability.

Shell-hook consent is a separate integration state. If the user explicitly wants to reset that consent too, remove `~/.hermes/shell-hooks-allowlist.json`.

### Shell hooks allowlist

Some shell-hook integrations require explicit allowlisting before they fire. Managed via `~/.hermes/shell-hooks-allowlist.json` — prompted interactively the first time a hook wants to run.

### Disabling the web/browser/image-gen tools

To keep the model away from network or media tools entirely, open `hermes tools` and toggle per-platform. Takes effect on next session (`/reset`). See `references/configuration.md` for the toolset list.
