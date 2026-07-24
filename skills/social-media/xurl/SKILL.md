---
name: xurl
description: "X/Twitter via xurl CLI: post, search, DM, media, v2 API."
version: 1.1.1
author: xdevplatform + openclaw + Hermes Agent
license: MIT
platforms: [linux, macos]
prerequisites:
  commands: [xurl]
metadata:
  hermes:
    tags: [twitter, x, social-media, xurl, official-api]
    homepage: https://github.com/xdevplatform/xurl
    upstream_skill: https://github.com/openclaw/openclaw/blob/main/skills/xurl/SKILL.md
---

# xurl — X (Twitter) API via the Official CLI

`xurl` is the X developer platform's official CLI for the X API. It supports shortcut commands for common actions AND raw curl-style access to any v2 endpoint. All commands return JSON to stdout.

## When to use this skill

Use this skill for:
- posting, replying, quoting, deleting posts
- searching posts and reading timelines/mentions
- liking, reposting, bookmarking
- following, unfollowing, blocking, muting
- direct messages
- media uploads (images and video)
- raw access to any X API v2 endpoint
- multi-app / multi-account workflows

This skill replaces the older `xitter` skill (which wrapped a third-party Python CLI). `xurl` is maintained by the X developer platform team, supports OAuth 2.0 PKCE with auto-refresh, and covers a substantially larger API surface.

## Routing table — read the reference before you act

| To do this | Read |
|---|---|
| Install `xurl`, register an app, run the OAuth 2.0 flow, Docker HOME pitfall, multi-app/multi-account | `references/installation-and-auth.md` |
| Look up any command: posting, search, timeline, engagement, social graph, DMs, media, raw v2 API, global flags, streaming, JSON output shapes, common workflows | `references/command-reference.md` |
| Diagnose auth failures, 401/403, `CreditsDepleted`, media upload errors | `references/troubleshooting.md` |

---

## Secret Safety (MANDATORY)

Critical rules when operating inside an agent/LLM session:

- **Never** read, print, parse, summarize, upload, or send `~/.xurl` to LLM context.
- **Never** ask the user to paste credentials/tokens into chat.
- The user must fill `~/.xurl` with secrets manually on their own machine. In Docker, this must be the `~` seen by Hermes tool subprocesses; see `references/installation-and-auth.md`.
- **Never** recommend or execute auth commands with inline secrets in agent sessions.
- **Never** use `--verbose` / `-v` in agent sessions — it can expose auth headers/tokens.
- To verify credentials exist, only use: `xurl auth status`.

Forbidden flags in agent commands (they accept inline secrets):
`--bearer-token`, `--consumer-key`, `--consumer-secret`, `--access-token`, `--token-secret`, `--client-id`, `--client-secret`

App credential registration and credential rotation must be done by the user manually, outside the agent session. After credentials are registered, the user authenticates with `xurl auth oauth2` — also outside the agent session.

## Confirm Before Any Write (MANDATORY)

- **Every write is public or sent to a real person.** Confirm the target and the
  user's intent before `post`, `reply`, `quote`, `dm`, `like`, `repost`,
  `bookmark`, `follow`, `block`, `mute`, and any raw `-X POST/PUT/PATCH` call.
  Show the exact text you are about to publish and the resolved post ID / handle,
  and wait for approval.
- **`xurl delete POST_ID` is irreversible.** Read the post first (`xurl read`),
  show it to the user, and get explicit approval before deleting.
- Never invent or approximate a post ID or handle. Resolve it from a read first.
- Do not batch multiple write actions behind a single approval.

## Rate limits & scopes

- **Rate limits:** X enforces per-endpoint rate limits. A 429 means wait and retry — do not retry in a tight loop. Write endpoints (post, reply, like, repost) have tighter limits than reads.
- **Scopes:** OAuth 2.0 tokens use broad scopes. A 403 on a specific action usually means the token is missing a scope — have the user re-run `xurl auth oauth2`.
- **Token refresh:** OAuth 2.0 tokens auto-refresh. Nothing to do.

---

## Minimal end-to-end skeleton

```bash
# 1. Verify prerequisites and that the DEFAULT app actually has credentials.
xurl --help
xurl auth status        # default app is marked with ▸ — it must show an oauth2 user

# 2. Cheap read to confirm reachability.
xurl whoami

# 3. Read before you write — resolve the real target.
xurl read https://x.com/user/status/1234567890

# 4. Show the user the exact text + target, get approval, THEN write.
xurl reply 1234567890 "Here are my thoughts..."
```

Agent workflow rules layered on that skeleton:

1. If `auth status` shows the default app with `oauth2: (none)` but another app has a valid oauth2 user, tell the user to run `xurl auth default <that-app>`. This is the most common setup mistake — the user added an app with a custom name but never set it as default, so xurl keeps trying the empty `default` profile.
2. If auth is missing entirely, stop and direct the user to `references/installation-and-auth.md` — do NOT attempt to register apps or pass secrets yourself.
3. Use JSON output directly — every response is already structured.
4. Never paste `~/.xurl` contents back into the conversation.

---

## Attribution

- Upstream CLI: https://github.com/xdevplatform/xurl (X developer platform team, Chris Park et al.)
- Upstream agent skill: https://github.com/openclaw/openclaw/blob/main/skills/xurl/SKILL.md
- Hermes adaptation: reformatted for Hermes skill conventions; safety guardrails preserved verbatim.
