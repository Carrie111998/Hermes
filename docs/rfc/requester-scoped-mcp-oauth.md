# RFC (Revised): Requester-Scoped MCP OAuth Isolation

- **Status:** Revised after adversarial code review
- **Date:** 2026-08-26
- **Upstream issue:** [NousResearch/hermes-agent#78174](https://github.com/NousResearch/hermes-agent/issues/78174)
- **Related (out of scope):** [#78169](https://github.com/NousResearch/hermes-agent/issues/78169) headless consent UX
- **Do not rebase:** [#79449](https://github.com/NousResearch/hermes-agent/pull/79449)

This document **supersedes** the draft RFC where they disagree. The draft's
threat model, invariants I1–I18, and "fresh native scope" decision stand.
The sections below are the review findings and the locked implementation
decisions.

## Adversarial findings (must change)

1. **`scope_id` is empty on most adapters.** Telegram, WhatsApp, Feishu,
   Mattermost, SMS, IRC, and Discord DMs do not populate tenant scope.
   Requiring a non-empty `scope_id` would fail-closed almost every
   non-Slack/Guild path. Empty bound `scope_id` is canonicalized to `"~"`.
   `_UNSET` (not bound) is still a hard miss.

2. **`get_session_env` falls back to `os.environ` when `_UNSET`.** A
   per-user principal MUST use a dedicated bound-only getter. Env vars
   are not a credential selector.

3. **Gateway/CLI/cron call `discover_mcp_tools()` at process start** with
   no human principal, and will connect OAuth servers from
   `mcp-tokens/<server>.json` under `suppress_interactive_oauth`. In
   `per_user`, OAuth-protected servers MUST NOT pick a human credential at
   startup. They are implicitly lazy until a request with a bound
   principal arrives. Tool *names* may still be registered from the
   unscoped schema cache so the model can see them.

4. **`handle_401` / unpinned `HermesTokenStorage` re-resolve ambient
   `get_hermes_home()`.** Scope and `hermes_home` MUST be captured at
   provider/connection construction and passed explicitly into refresh,
   401, reconnect, and disk-watch.

5. **`_lazy_server_configs` is popped on first connect.** In `per_user`,
   Alice's first use must not delete the lazy config Bob still needs.

6. **In-memory Hermes tool registry is process-global.** Closing the
   confused-deputy hole does not require per-session tool schemas (that
   would also fight prompt caching if done mid-conversation). Live calls
   use the requester's connection; a tool Bob does not actually have fails
   at the MCP server. Disk `cacheScope=private` entries are still scoped.

7. **No metrics/telemetry in this change** (project policy: no outbound
   telemetry without a user-facing opt-in). Structured log fields only,
   with opaque principal keys, never tokens.

8. **Do not implement #78169** (consent URL delivery, paste-back, gateway
   message injection). Do not add `hermes mcp login --user`.

9. **OAuth isolation applies to `auth: oauth` servers.** Stdio and static
   header servers keep process-level connections. Stdio env credentials
   remain shared; that is an explicit non-goal.

10. **Subagents inherit the parent's bound principal** via ContextVar copy.
    Cron blanks identity and therefore fail-closes in `per_user`.

11. **`_run_on_mcp_loop` copies the MCP loop thread's ContextVars, not the
    agent's.** `run_coroutine_threadsafe` creates the task inside the loop
    thread. Without an explicit wrap, `_capture_oauth_identity` (and any
    `get_bound_session_principal()` inside connect) would fail closed on a
    live gateway request, or worse inherit a stale loop-thread principal.
    The scheduling thread's bound principal MUST be re-applied inside the
    scheduled task (same hop as `HERMES_HOME` override). Identity is also
    pinned on `MCPServerTask` in `start()` before `ensure_future(run())`
    so reconnects never re-resolve ambient identity.

## Locked decisions

| Topic | Decision |
|---|---|
| Default | `mcp.oauth.identity_mode: shared` (absent key = shared) |
| Invalid mode | Reject (`per-user`, typos). Never downgrade to shared |
| Principal | `(v1, platform, scope_id, user_id)` from bound ContextVars only |
| Empty `scope_id` | Canonical `"~"` when the field is bound-or-absent-as-empty |
| Persistence key | `u-v1-` + SHA-256 of canonical JSON array; never raw IDs in paths |
| Shared layout | Unchanged: `$HERMES_HOME/mcp-tokens/<server>.*` |
| Per-user layout | `$HERMES_HOME/mcp-tokens/by-user/<persistence_key>/<server>.*` |
| Migration | Never assign a legacy shared token to a requester |
| Registry key | `server_name` in shared; `server_name + \\x1f + persistence_key` in per_user |
| Lookup | Exact key only. No "any connection named github" fallback |
| CLI / TUI / desktop / cron in `per_user` | Fail closed with an actionable error when no bound principal |
| MCP loop hop | `_run_on_mcp_loop` re-binds the caller's principal; `start()` pins it on the connection |
| `hermes mcp remove` | Admin: may delete that server's artifacts across `by-user/*` |
| Idle eviction / per-server override / HMAC path keys | Deferred |

## Core invariant

A request authenticated as principal A MUST NOT read, refresh, select,
reuse, reconnect, disconnect, or otherwise affect any credential-bearing
object belonging to principal B.
