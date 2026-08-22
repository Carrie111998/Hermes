# Global Repository Contracts

This reference is required when working in the areas it covers. Root `AGENTS.md` remains authoritative.

## Important Policies

### Prompt Caching Must Not Break

Hermes-Agent ensures caching remains valid throughout a conversation. **Do NOT implement changes that would:**
- Alter past context mid-conversation
- Change toolsets mid-conversation
- Reload memories or rebuild system prompts mid-conversation

Cache-breaking forces dramatically higher costs. The ONLY time we alter context is during context compression.

Slash commands that mutate system-prompt state (skills, tools, memory, etc.)
must be **cache-aware**: default to deferred invalidation (change takes
effect next session), with an opt-in `--now` flag for immediate
invalidation. See `/skills install --now` for the canonical pattern.

### Background Process Notifications (Gateway)

When `terminal(background=true, notify_on_complete=true)` is used, the gateway runs a watcher that
detects process completion and triggers a new agent turn. Control verbosity of background process
messages with `display.background_process_notifications`
in config.yaml (or `HERMES_BACKGROUND_NOTIFICATIONS` env var):

- `concise` — one-line status message on completion; failures append a short output tail (default)
- `all` — running-output updates + final raw-output message
- `result` — only the final raw-output completion message
- `error` — only the final raw-output message when exit code != 0
- `off` — no watcher messages at all

---

## Profiles: Multi-Instance Support

Hermes supports **profiles** — multiple fully isolated instances, each with its own
`HERMES_HOME` directory (config, API keys, memory, sessions, skills, gateway, etc.).

The core mechanism: `_apply_profile_override()` in `hermes_cli/main.py` sets
`HERMES_HOME` before any module imports. All `get_hermes_home()` references
automatically scope to the active profile.

### Rules for profile-safe code

1. **Use `get_hermes_home()` for all HERMES_HOME paths.** Import from `hermes_constants`.
   NEVER hardcode `~/.hermes` or `Path.home() / ".hermes"` in code that reads/writes state.
   ```python
   # GOOD
   from hermes_constants import get_hermes_home
   config_path = get_hermes_home() / "config.yaml"

   # BAD — breaks profiles
   config_path = Path.home() / ".hermes" / "config.yaml"
   ```

2. **Use `display_hermes_home()` for user-facing messages.** Import from `hermes_constants`.
   This returns `~/.hermes` for default or `~/.hermes/profiles/<name>` for profiles.
   ```python
   # GOOD
   from hermes_constants import display_hermes_home
   print(f"Config saved to {display_hermes_home()}/config.yaml")

   # BAD — shows wrong path for profiles
   print("Config saved to ~/.hermes/config.yaml")
   ```

3. **Module-level constants are fine** — they cache `get_hermes_home()` at import time,
   which is AFTER `_apply_profile_override()` sets the env var. Just use `get_hermes_home()`,
   not `Path.home() / ".hermes"`.

4. **Tests that mock `Path.home()` must also set `HERMES_HOME`** — since code now uses
   `get_hermes_home()` (reads env var), not `Path.home() / ".hermes"`:
   ```python
   with patch.object(Path, "home", return_value=tmp_path), \
        patch.dict(os.environ, {"HERMES_HOME": str(tmp_path / ".hermes")}):
       ...
   ```

5. **Gateway platform adapters should use token locks** — if the adapter connects with
   a unique credential (bot token, API key), call `acquire_scoped_lock()` from
   `gateway.status` in the `connect()`/`start()` method and `release_scoped_lock()` in
   `disconnect()`/`stop()`. This prevents two profiles from using the same credential.
   See `plugins/platforms/irc/adapter.py` for the canonical pattern.

6. **Profile operations are HOME-anchored, not HERMES_HOME-anchored** — `_get_profiles_root()`
   returns `Path.home() / ".hermes" / "profiles"`, NOT `get_hermes_home() / "profiles"`.
   This is intentional — it lets `hermes -p coder profile list` see all profiles regardless
   of which one is active.

7. **Multiplex profile-scoped env reads MUST fail closed — never borrow from `os.environ`**
   (`agent/secret_scope.py` contract; #72348, #86905). Under `gateway.multiplex_profiles`,
   `os.environ` holds the **default profile's** values; a secondary profile's `.env` lives
   only in its secret scope (installed per-turn by `_profile_runtime_scope`). Any
   profile-level env config — credentials (`app_secret`, tokens) AND authorization
   (`FEISHU_ALLOWED_USERS`, `{PLATFORM}_ALLOW_ALL_USERS`, `GATEWAY_ALLOW_ALL_USERS`,
   `group_policy`, `allow_bots`, ...) — must be read scope-aware:
   - Adapters: `_get_scoped_secret()` (canonical fail-closed copy in
     `plugins/platforms/feishu/adapter.py`, #86905).
   - Gateway authz: `_auth_env()` / `_platform_gate_env()` (`gateway/authz_mixin.py`).
   Rules:
   - Scope installed + multiplex active → a scoped miss returns the **default**.
     NEVER fall through to `os.environ` — that leaks another profile's value and
     silently breaks routing/admission (a leaked default allowlist skips the
     allow-all check and rejects every secondary-profile sender, #86905).
   - Unscoped default-profile path (`UnscopedSecretError`) and single-profile
     deployments keep the `os.environ` read — there it IS the profile's own value.
   - Authorization config is the sharpest edge: allowlist/allow-all leaks cause
     silent rejections (or worse, fail-open) that only show up as missing replies.
   - The `_get_scoped_secret` wrapper is copy-pasted across ~15 platform adapters —
     when touching any of them, make sure the fail-closed semantics are present;
     do not reintroduce the `except _UnscopedSecretError: val = os.getenv(...)`
     fallback-after-miss shape.

## Known Pitfalls

### DO NOT hardcode `~/.hermes` paths
Use `get_hermes_home()` from `hermes_constants` for code paths. Use `display_hermes_home()`
for user-facing print/log messages. Hardcoding `~/.hermes` breaks profiles — each profile
has its own `HERMES_HOME` directory. This was the source of 5 bugs fixed in PR #3575.

### All CLI menu-pickers MUST use curses.
Interactive menus must use `hermes_cli/curses_ui.py`. See `hermes_cli/tools_config.py` for an example.

### DO NOT use `\033[K` (ANSI erase-to-EOL) in spinner/display code
Leaks as literal `?[K` text under `prompt_toolkit`'s `patch_stdout`. Use space-padding: `f"\r{line}{' ' * pad}"`.

### `_last_resolved_tool_names` is a process-global in `model_tools.py`
`_run_single_child()` in `delegate_tool.py` saves and restores this global around subagent execution. If you add new code that reads this global, be aware it may be temporarily stale during child agent runs.

### DO NOT hardcode cross-tool references in schema descriptions
Tool schema descriptions must not mention tools from other toolsets by name (e.g., `browser_navigate` saying "prefer web_search"). Those tools may be unavailable (missing API keys, disabled toolset), causing the model to hallucinate calls to non-existent tools. If a cross-reference is needed, add it dynamically in `get_tool_definitions()` in `model_tools.py` — see the `browser_navigate` / `execute_code` post-processing blocks for the pattern.

### The gateway has TWO message guards — both must bypass approval/control commands
When an agent is running, messages pass through two sequential guards:
(1) **base adapter** (`gateway/platforms/base.py`) queues messages in
`_pending_messages` when `session_key in self._active_sessions`, and
(2) **gateway runner** (`gateway/run.py`) intercepts `/stop`, `/new`,
`/queue`, `/status`, `/approve`, `/deny` before they reach
`running_agent.interrupt()`. Any new command that must reach the runner
while the agent is blocked (e.g. approval prompts) MUST bypass BOTH
guards and be dispatched inline, not via `_process_message_background()`
(which races session lifecycle).

### Streaming delivery contract (stream-is-the-message adapters) — duplicate-final class
Adapters with `draft_stream_is_message = True` (relay Slack native streaming)
keep ONE cumulative native stream per turn; the stream IS the final message.
Four invariants, each learned from a live duplicate-final incident (NS-658
canary ledger, hermes#85796 / gateway-gateway#210). Violating any of them
re-creates a duplicate or a frozen stream:

1. **Draft frames must be prefix-stable.** The connector computes append-only
   deltas: frame N must be a string prefix of frame N+1. NEVER mutate draft
   frames per-tick — no fence-closing (`ensure_closed_code_fences`), no cursor
   suffix, no segment-state resets at tool boundaries, no mrkdwn conversion.
   Any non-prefix frame triggers a whole-snapshot re-append on the platform
   ("stacked copies"). The finalize path may still transform the real final.
2. **The consumer declares the final; the adapter never guesses.**
   `finish(final_text)` carries the completed `final_response` (verifier
   footer, completion explainer included) as the authoritative finalize
   payload. New post-stream response augmentation MUST ride this payload —
   if it mutates `final_response` after the stream sealed, it re-opens the
   #11 bug (`delivered_final_matches` mismatch → corrective duplicate send).
3. **Interim sends must carry `_interim_send` metadata.** Any consumer-side
   `adapter.send()` that is NOT the turn-final (commentary, segment-tail
   flushes) must set `metadata["_interim_send"] = True`, or the relay
   adapter's seal-interception will seal the live stream with interim text.
   Seal-interception exists at BOTH egress doors (`send()` AND
   `send_for_platform()`); a new egress door needs the same two checks.
4. **Reconcile by edit, never by plain send.** Any lane that delivers a final
   beside an already-sealed stream (queued follow-ups, media-accompanied
   finals, future lanes) must first try `edit_message` on the consumer's
   `message_id`; plain `send()` is the fallback only when no editable message
   exists. A sealed native stream is a regular message — `chat.update` on it
   works (live-verified).

Contract tests: `tests/gateway/test_stream_final_contract.py` (all four
invariants, mutation-checked). Slack streaming API ground truth (live-probed,
also encoded in connector comments/tests): `chat.*Stream` speaks STANDARD
markdown, not mrkdwn; `stopStream.markdown_text` APPENDS (never replaces);
`startStream`/`stopStream` are rate-limit Tier 2 (~20/min).

Guard style note: check `draft_stream_is_message` with `is True` — MagicMock
adapters in older tests auto-create truthy attributes.

### Squash merges from stale branches silently revert recent fixes
Before squash-merging a PR, ensure the branch is up to date with `main`
(`git fetch origin main && git reset --hard origin/main` in the worktree,
then re-apply the PR's commits). A stale branch's version of an unrelated
file will silently overwrite recent fixes on main when squashed. Verify
with `git diff HEAD~1..HEAD` after merging — unexpected deletions are a
red flag.

### Don't wire in dead code without E2E validation
Unused code that was never shipped was dead for a reason. Before wiring an
unused module into a live code path, E2E test the real resolution chain
with actual imports (not mocks) against a temp `HERMES_HOME`.

### Tests must not write to `~/.hermes/`
The `_isolate_hermes_home` autouse fixture in `tests/conftest.py` redirects `HERMES_HOME` to a temp dir. Never hardcode `~/.hermes/` paths in tests.

**Profile tests**: When testing profile features, also mock `Path.home()` so that
`_get_profiles_root()` and `_get_default_hermes_home()` resolve within the temp dir.
Use the pattern from `tests/hermes_cli/test_profiles.py`:
```python
@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home
```

---
