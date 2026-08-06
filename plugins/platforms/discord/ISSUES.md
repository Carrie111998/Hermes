# Nous Man (Discord Bot) — Issues & Ticketing

> **Living ticket log for the Hermes Discord adapter** (Nous Man#0498).
> File location: `plugins/platforms/discord/ISSUES.md`
> Add new tickets at the TOP of the "Active" section. Move resolved tickets to
> "Closed" with a resolution summary and RCA cross-reference. Update the
> TL;DR table whenever status changes.
>
> Cross-references:
> - System-wide critical issues: `d:\projects\CRITICAL-ISSUES.md`
> - Runtime patcher: `d:\projects\hermes-agent-hotreload\apply-runtime-patches.py`
> - Gateway logs: `C:\Users\<user>\AppData\Local\hermes\logs\{agent,errors,gateway}.log`

---

## TL;DR

| # | Severity | Status | Summary | Opened | Owner |
|---|----------|--------|---------|--------|-------|
| D-001 | RED | FIXED | Bot replies "Sorry, I encountered an unexpected error" — empty OPENROUTER_API_KEY in .env overrode config.yaml | 2026-07-20 | Purple Industries / BarnsL |
| D-002 | RED | FIXED | "API call failed after 3 retries: HTTP 429" on Z.AI — billing 429 misclassified as rate_limit | 2026-07-20 | Purple Industries / BarnsL |
| D-003 | RED | FIXED | Kimi "usage limit for this billing cycle" 403 misclassified as auth — surfaces as "Non-retryable client error" | 2026-07-20 | Purple Industries / BarnsL |
| D-004 | YELLOW | FIXED | GLM-5/5.2 "hangs without doing anything" — stale-stream detector kills mid-think | 2026-07-20 | Purple Industries / BarnsL |
| D-005 | YELLOW | MONITORING | Sessions loading blank — system prompt stored as null after session reset | 2026-07-21 | Purple Industries / BarnsL |
| D-006 | YELLOW | MONITORING | Compression loop fires every 5-10 min on long Discord sessions | 2026-07-20 | Purple Industries / BarnsL |
| D-007 | GREEN | READY | Image attachment processing — verify end-to-end on free OpenRouter | 2026-07-21 | Purple Industries / BarnsL |
| D-008 | YELLOW | FIXED | auth.json WinError 5 (Access denied) on atomic rename — Windows Defender real-time scan holds file during os.replace | 2026-07-21 | Purple Industries / BarnsL |

### RCA cross-cutting patterns

| ID | Pattern | Tickets | Severity |
|----|---------|---------|----------|
| [RCA-A](#rca-a--env-var-precedence-rule-the-silent-override-trap) | Empty `<PROVIDER>_API_KEY=` in .env overrides config.yaml | D-001 | RED |
| [RCA-B](#rca-b--provider-error-bodies-that-dont-match-the-classifiers-vocabulary) | Provider error body phrasing not in classifier's phrase lists | D-002, D-003 | RED |
| [RCA-C](#rca-c--reasoning-models-killed-mid-think-by-stale-stream-detector) | Reasoning models not in `_REASONING_STALE_TIMEOUT_FLOORS` table | D-004 | YELLOW |
| [RCA-D](#rca-d--compression-loop-on-long-sessions-target-not-sticky) | Compressor re-probes target upward after every cycle | D-006 | YELLOW |
| [RCA-E](#rca-e--system-prompt-null-after-session-reset) | session_reset path doesn't persist system_prompt | D-005 | YELLOW |

---

## Ticket template

```markdown
### D-XXX — <short title>  [STATUS]

**Severity:** RED / YELLOW / GREEN
**Opened:** YYYY-MM-DD by <user>
**Affected users/channels:** <e.g. all DMs, #bot-spam, specific user>
**Symptom:**
<1-2 sentence user-visible description>

**Reproduction:**
1. <step>
2. <step>
**Expected:** <behavior>
**Actual:** <behavior>

**Logs:**
- `errors.log` excerpt (last 1-3 lines):
  ```
  <paste>
  ```
- `agent.log` session ID: `<session_id>`

**RCA / Fix:**
<root cause + resolution, or link to CRITICAL-ISSUES.md section>

**Standing rule:**
<one-line rule for future operators>
```

---

## RCA — Root Cause Analysis (cross-cutting)

> This section captures the **systemic** patterns behind the tickets in this
> file. Each RCA block describes WHY a class of bug exists, not just what
> broke. Read this before opening a new ticket — many "new" bugs are
> re-appearances of the patterns below.

### RCA-A — Env-var precedence rule (the silent Override Trap)

**Pattern:** An empty `<PROVIDER>_API_KEY=` line in `.env` silently overrides
a working key hardcoded in `config.yaml`'s `model.api_key` field. Hermes'
config loader resolves env vars *before* reading config.yaml, so the runtime
sees the empty string and refuses to start with a confusing
`No LLM provider configured` error that points everywhere except the real
source.

**Why it bit us:** D-001 (2026-07-21).

**Reproduction (verified):**
```powershell
# 1. Confirm working state
hermes gateway status   # → running
# 2. Edit .env: add a single empty line
Add-Content $env:LOCALAPPDATA\hermes\.env "OPENROUTER_API_KEY="
# 3. Restart
hermes gateway restart
# 4. Send any Discord message → bot replies:
#    "Sorry, I encountered an unexpected error."
# 5. errors.log shows:
#    RuntimeError: No LLM provider configured. Run `hermes model` to select a
#    provider, or run `hermes setup` for first-time configuration.
```

**Resolution order (precedence high → low):**
1. `<PROVIDER>_API_KEY` environment variable (even if empty string)
2. `config.yaml` → `model.api_key` (or per-fallback `api_key`)
3. `auth.json` credential pool entries
4. Provider profile `env_vars` tuple (used for detection only)

**Standing rule:**
NEVER leave an empty `<PROVIDER>_API_KEY=` line in `.env`. Either delete the
line entirely or comment it out (`# OPENROUTER_API_KEY=`). The idempotent
resync script `apply-openrouter-key.py` is the source of truth — re-run it
after any `.env` edit.

**Cross-reference:** CRITICAL-ISSUES.md (none — Discord-specific),
D-001 in this file, `apply-openrouter-key.py`.

---

### RCA-B — Provider error bodies that don't match the classifier's vocabulary

**Pattern:** The `agent/error_classifier.py` `_classify_by_status()` function
uses a curated phrase list (`_BILLING_PATTERNS`, `_RATE_LIMIT_PATTERNS`, etc.)
to route errors to the correct recovery action. When a provider uses a
NOVEL phrasing for billing-exhaustion (or any other failure class) that
matches NONE of the lists, the error falls through to a generic bucket
(usually `rate_limit` or `auth`) with the WRONG recovery semantics.

The wrong recovery then causes a SECONDARY failure mode that is more
visible than the original:
- Billing 429 misclassified as rate_limit → 3 wasted retries on a dead key
- Billing 403 misclassified as auth → surfaces as "Non-retryable client
  error" instead of activating the fallback chain
- Billing 403 with permission_error type → same as above

**Why it bit us:** D-002 (Z.AI code 1113), D-003 (Kimi permission_error).

**Reproduction (verified, D-002):**
```
# 1. Set GLM_API_KEY to a key with zero account balance
# 2. Send any message to Nous Man with provider=zai, model=glm-5
# 3. errors.log shows 3 retries before failure:
2026-07-20 21:27:34 WARNING API call failed (attempt 1/3)
  provider=zai base_url=https://api.z.ai/api/paas/v4 model=glm-5
  summary=HTTP 429: Insufficient balance or no resource package. Please recharge.
2026-07-20 21:27:42 WARNING API call failed (attempt 2/3) ...
2026-07-20 21:27:52 ERROR    API call failed after 3 retries. ...
# 4. Each retry adds ~10s of latency before the bot can fall back.
```

**Reproduction (verified, D-003):**
```
# 1. Use a Kimi Coding Plan subscription until quota exhausts
# 2. Send any message with provider=kimi-coding, model=kimi-k2.7-code
# 3. errors.log shows immediate abort with no fallback:
2026-07-21 21:05:18 WARNING provider=kimi-coding model=kimi-k2.7-code
  summary=HTTP 403: You've reached your usage limit for this billing cycle.
  Your quota will be refreshed in the next cycle. To continue now, purchase
  extra usage or upgrade your plan: https://www.kimi.com/code/#pricing
2026-07-21 21:05:19 ERROR Non-retryable client error: Error code: 403 - {...}
# 4. No fallback activation, no "credits exhausted" status, bot just dies.
```

**Vocabulary mismatch table (as of 2026-07-21):**

| Provider | HTTP | Body shape | Classifier branch (pre-fix) | Match? | Wrong bucket |
|----------|------|-----------|----------------------------|--------|--------------|
| Z.AI | 429 | `{"code":"1113","message":"Insufficient balance or no resource package. Please recharge."}` | 429 → `_OVERLOADED_PATTERNS` → fallthrough → `rate_limit` | NO | rate_limit (burns 3 retries) |
| Kimi | 403 | `{"type":"permission_error","message":"You've reached your usage limit for this billing cycle. Your quota will be refreshed in the next cycle."}` | 403 → `"key limit exceeded"` / `"spending limit"` / `_BILLING_PATTERNS` | NO | auth (no fallback) |
| Kimi | 429 | `{"message":"The engine is currently overloaded, please try again later"}` | 429 → `_OVERLOADED_PATTERNS` (`"overloaded"`) | YES | overloaded (correct) |
| Anthropic | 429 | `{"type":"rate_limit_error","message":"This request would exceed your account's rate limit."}` | 429 → fallthrough → `rate_limit` | YES | rate_limit (correct) |
| Anthropic | 400 | `Invalid signature in thinking block` | 400 → thinking-sig handler | YES | thinking_signature (correct) |
| Anthropic | 429 | `"extra usage" + "long context"` | 429 → long-context-tier handler | YES | long_context_tier (correct) |

**The pattern in one sentence:**
> Each provider has its own error vocabulary, but the classifier's lists
> were built reactively from past failures — a new provider's first failure
> will always land in the wrong bucket until its phrases are added.

**Resolution:** For each new provider, search its API docs / error catalog
for billing / quota / overload phrasings and add them to the right list
in `agent/error_classifier.py`. The runtime patcher
(`apply-runtime-patches.py`) is the operational path for adding phrases
without editing the dev tree.

**Standing rule:**
When adding a new provider to Hermes:
1. Find the provider's error code reference in their docs
2. For each billing/quota/overload code, identify the body shape and add
   the phrasing to the matching list (`_BILLING_PATTERNS`, `_OVERLOADED_PATTERNS`,
   `_RATE_LIMIT_PATTERNS`)
3. If the provider uses non-standard HTTP status (e.g. Z.AI's 429 for
   billing), add a special-case check in `_classify_by_status` BEFORE the
   generic fallthrough
4. Test by deliberately exhausting a test key and confirming the error
   classifies correctly

**Cross-reference:** CRITICAL-ISSUES.md #16, #17; D-002, D-003 in this file;
`agent/error_classifier.py:954` (429 branch), `:883` (403 branch).

---

### RCA-C — Reasoning models killed mid-think by stale-stream detector

**Pattern:** Cloud reasoning models (those that emit extended thinking
blocks before their first content token — GLM-5.2, OpenAI o1/o3, Anthropic
Opus 4.x thinking, DeepSeek R1, NVIDIA Nemotron, xAI Grok reasoning) pause
for several minutes during the thinking phase. Hermes' stream stale
detector (`HERMES_STREAM_STALE_TIMEOUT`, default 180s) and the httpx
socket read timeout (default 120s) both fire BEFORE the model finishes
thinking, tearing down a healthy reasoning stream mid-think.

**User-visible symptom:**
> "GLM models just hang without even trying to do anything."

The user perceives this as a hang because no content tokens arrive within
the default window — then the connection is killed and the retry loops.

**Why it bit us:** D-004 (GLM-5/5.2 missing from `_REASONING_STALE_TIMEOUT_FLOORS`).

**Reproduction (verified, D-004):**
```
# 1. Set provider=zai, model=glm-5.2 (thinking mode is ON by default)
# 2. Send a non-trivial reasoning task (e.g. "write a Python function that
#    solves the N-queens problem and explain your approach")
# 3. Watch the bot's Discord reply — no streaming output for 180s
# 4. agent.log shows:
#    Stream stale for 180s (threshold 180s) - no chunks received.
#    model=glm-5.2 context=~N tokens. Killing connection.
# 5. Loop retries → 540s+ total before final failure
```

**Resolution mechanism (already in place):**
The `_REASONING_STALE_TIMEOUT_FLOORS` table in `agent/reasoning_timeouts.py`
exists exactly for this. The floor is applied as `max(default, floor)` so
the stale detector and httpx read timeout both scale up for known reasoning
models. **The bug was that GLM models were missing from the table** — a
contributing factor was that GLM-5.2's thinking mode is opt-out (ON by
default), unlike OpenAI's o1 which is opt-in.

**Reproduction of the fix (verified):**
```python
>>> from agent.reasoning_timeouts import get_reasoning_stale_timeout_floor
>>> get_reasoning_stale_timeout_floor("glm-5.2")
300.0    # was None before fix
>>> get_reasoning_stale_timeout_floor("glm-4-9b") is None  # non-thinking, correctly excluded
True
```

**Why GLM was missed when the table was originally populated:**
GLM-4.5-and-later shipped thinking-mode ON by default on the OpenAI-compat
endpoint. When the `_REASONING_STALE_TIMEOUT_FLOORS` table was built
(Fixes #52217), the test set focused on cloud reasoning models with
DOCUMENTED multi-minute TTFB (NVIDIA Nemotron, OpenAI o1, Anthropic Opus
4.x thinking, DeepSeek R1). GLM's thinking mode is opt-out and was added
later in `plugins/model-providers/zai/__init__.py` — the reasoning-timeout
table was never updated to include it.

**The systemic gap:**
The reasoning-timeout table and the provider profiles are maintained
separately. There's no automated check that says "this provider's model
emits thinking blocks → add it to the floor table". The result: any new
reasoning model added via a provider profile will hit this bug until
someone manually adds it to the table.

**Standing rule:**
When a provider profile emits `extra_body["thinking"] = {"type": "enabled"}`
or sets a top-level `reasoning_effort`, the model is a reasoning model.
ALSO add it to `_REASONING_STALE_TIMEOUT_FLOORS` with a floor of at least
180s (light) / 240s (medium) / 300s (heavy) based on its max thinking time.

**Cross-reference:** CRITICAL-ISSUES.md #18; D-004 in this file;
`agent/reasoning_timeouts.py:_REASONING_STALE_TIMEOUT_FLOORS`;
`plugins/model-providers/zai/__init__.py:ZaiProfile.build_api_kwargs_extras`.

---

### RCA-D — Compression loop on long sessions (target not sticky)

**Pattern:** Long-running Discord sessions repeatedly cross the ~200K token
threshold, trigger context compression (paid API call + ~30s latency),
shrink to ~200K, then climb back over the threshold within minutes. Each
cycle burns a paid API call.

**Why it bit us:** D-006 (compression loop on long Discord sessions).

**Reproduction (in-progress, D-006):**
```
# 1. Start a long coding session (read 2+ large files, edit them)
# 2. Leave the session running for >30 min
# 3. agent.log shows repeated compression events every 5-10 min:
2026-07-20 18:09:03 context compression started: tokens=~228,184
2026-07-20 18:21:03 context compression started: tokens=~315,967
2026-07-20 19:13:18 context compression started: tokens=~279,920
2026-07-20 19:19:07 context compression started: tokens=~311,851
```

**Hypothesis (pending verification):**
The target context length set after compression is being re-probed upward
by the context compressor on subsequent turns, so the effective threshold
drifts back toward the model's max (1M for nemotron, 256K for kimi). Each
probe re-triggers compression when the session grows past the probed value.

**Why this is a Discord-bot-specific problem:**
Discord sessions are long-lived (cron-driven `session_reset` only fires at
04:00 local). Telegram/CLI sessions typically reset more often. The bot's
uptime + heavy code-editing tool calls = the perfect storm for repeated
compression cycles.

**Standing rule (interim):**
If `agent.log` shows compression events more than once per 15 min on the
same session, the compressor is in a loop. Kill the session (`/reset` in
Discord) rather than burning paid API calls. Long-term fix is in
`agent/context_compressor.py` — make the post-compression target sticky.

**Cross-reference:** D-006 in this file; `agent/context_compressor.py`.

---

### RCA-E — System-prompt null after session reset

**Pattern:** After the daily `session_reset.at_hour: 4` fires, the first
message of each new session triggers a `Stored system prompt for session
<id> is null` warning. The bot recovers (rebuilds the prompt from scratch)
but the first turn is slower (no prefix cache hit).

**Why it bit us:** D-005 (system prompt null).

**Reproduction (verified, D-005):**
```
# 1. Wait for session_reset.at_hour: 4 to fire (or manually trigger)
# 2. Send a message to Nous Man in any channel
# 3. agent.log shows:
#    WARNING Stored system prompt for session <id> is null; rebuilding from
#    scratch this turn. Prefix cache will miss until the rebuild persists.
#    Investigate the previous turn's update_system_prompt write path.
```

**Hypothesis:**
The session_reset path writes a new session row but does not write the
`system_prompt` column. The first turn of the new session falls back to
the rebuild path.

**Standing rule:**
"System prompt null" warnings on the first turn of a session are not a
crisis — the bot will recover. Investigate only if it persists past the
first turn.

**Cross-reference:** D-005 in this file; `gateway/session.py` session_reset
handler.

---

## Active tickets

### D-005 — Sessions loading blank (system prompt null)  [MONITORING]

**Severity:** YELLOW
**Opened:** 2026-07-21 by Sleepy Cat / BarnsL
**Affected:** All sessions after a session_reset (cron at 04:00 local)

**Symptom:**
After the daily `session_reset` runs, the first message of each new session
triggers this warning:

```
WARNING Stored system prompt for session <id> is null; rebuilding from
scratch this turn. Prefix cache will miss until the rebuild persists.
```

The bot still works, but the first turn is slower (no prefix cache hit) and
the agent's identity instructions are re-built from scratch.

**Reproduction:**
1. Wait for `session_reset.at_hour: 4` to fire (or manually trigger)
2. Send a message to Nous Man in any channel
3. Check `agent.log` for the warning above

**Expected:** System prompt persists across session boundaries (or is rebuilt
and cached within seconds, not on the user's first message).

**Actual:** System prompt is null on first turn of every new session.

**RCA / hypothesis:**
The session_reset path writes a new session row but does not write the
`system_prompt` column. The first turn of the new session falls back to
the rebuild path. Non-blocking but adds ~3-5s latency to the first message
of each day.

**Fix (TODO):**
Investigate `gateway/session.py` `session_reset` handler — ensure it either
copies the previous system prompt or pre-builds the default one before the
first message arrives.

**Standing rule:**
"System prompt null" warnings on the first turn of a session are not a
crisis — the bot will recover. Investigate only if it persists past the
first turn.

---

### D-006 — Compression loop fires every 5-10 min on long sessions  [MONITORING]

**Severity:** YELLOW
**Opened:** 2026-07-20 by BarnsL
**Affected:** Discord sessions that run >200K tokens (long coding tasks)

**Symptom:**
Long Discord sessions repeatedly cross the ~200K token threshold, trigger
context compression (which itself uses a paid API call), shrink to ~200K,
then climb back over the threshold. Each cycle = 1 paid API call + ~30s
latency.

**Logs:**
```
2026-07-20 18:09:03 context compression started: tokens=~228,184 model=kimi-k2.7-code
2026-07-20 18:21:03 context compression started: tokens=~315,967 model=claude-haiku-4-5
2026-07-20 19:13:18 context compression started: tokens=~279,920 model=kimi-k2.7-code
2026-07-20 19:19:07 context compression started: tokens=~311,851 model=kimi-k2.7-code
2026-07-20 19:25:44 context compression started: tokens=~274,808 model=kimi-k2.7-code
```

**Reproduction:**
1. Start a long coding session with Nous Man (read 2+ large files, edit them)
2. Leave the session running for >30 min
3. Watch `agent.log` for repeated compression events

**Expected:** After compression, the context should stay below the threshold
for several turns. The target context length should be sticky.

**Actual:** Sessions re-cross the threshold within 5-10 min, triggering
another paid compression call.

**RCA / hypothesis:**
The target context length is being re-probed upward after every compression,
so the effective threshold drifts back toward the model's max (1M for
nemotron, 256K for kimi). Each probe costs a compression call.

**Fix (TODO):**
1. Check `agent/context_compressor.py` for the re-probe logic.
2. Make the post-compression target sticky — once compressed to X, stay at X
   until the user explicitly resets or switches models.
3. Consider raising `compression.threshold` from 0.7 → 0.85 so compression
   fires later (less aggressive).

**Standing rule:**
If `agent.log` shows compression events more than once per 15 min on the same
session, the compressor is in a loop. Kill the session (`/reset`) rather
than burning paid API calls.

---

### D-007 — Image attachment processing verification  [READY TO TEST]

**Severity:** GREEN
**Opened:** 2026-07-21 by BarnsL
**Affected:** All Discord channels where users send images to Nous Man

**Symptom:**
None currently reported. This ticket tracks end-to-end verification that
image attachments sent to Nous Man are processed correctly through the
free-tier OpenRouter fallback chain.

**Pipeline (current):**
1. Discord user uploads image attachment
2. `_cache_discord_image(att, ext)` in `plugins/platforms/discord/adapter.py:6049`
   - Primary: `att.read()` + `cache_image_from_bytes` (authenticated, no SSRF gate)
   - Fallback: `cache_image_from_url` (SSRF-gated)
3. `vision` toolset (see `platform_toolsets.discord` in config.yaml)
4. `auxiliary.vision` provider in config.yaml:
   ```yaml
   auxiliary:
     vision:
       provider: openrouter
       model: google/gemma-4-31b-it:free
       enabled: true
   ```
5. Vision model converts image to text description
6. Main model receives text description (`agent.image_input_mode: text`)

**Verification steps:**
1. Send a PNG/JPG image to Nous Man in a DM with a question like "what's in
   this image?"
2. Check `agent.log` for:
   - `auxiliary_client: vision task using openrouter (google/gemma-4-31b-it:free)`
   - No `image_too_large` errors
3. Confirm response describes the image correctly

**Known limits:**
- `image_input_mode: text` — main model sees a description, not raw pixels.
  Change to `native` if you switch to a vision-capable primary model.
- Free-tier gemma-4-31b may rate-limit on heavy image load. Fallback chain
  has no second vision-capable free model — if gemma rate-limits, image
  processing fails.
- Anthropic 5 MB per-image hard limit applies on anthropic-wire paths.

**Standing rule:**
If a user reports "bot can't see images", check `auxiliary.vision.provider`
in config.yaml first. If OPENROUTER_API_KEY is set, gemma-4-31b:free should
work. If not, switch to a vision-capable main model (e.g. claude-sonnet)
and set `agent.image_input_mode: native`.

---

### D-008 — auth.json WinError 5 on Windows (Defender scan race)  [FIXED 2026-07-21]

**Severity:** RED
**Opened / closed:** 2026-07-21 / 2026-07-21
**Affected:** All Hermes Desktop agents on Windows with Defender real-time protection enabled.

**Symptom:**
Agent init fails with:
```
ent init failed: [WinError 5] Access is denied: 'C:\Users\Burgboy\AppData\Local\hermes\auth.json.tmp.18300.76441fa549ea4dccb50b239689944442' -> 'C:\Users\Burgboy\AppData\Local\hermes\auth.json'
```
Repeats on every auth.json write (provider auth, token refresh, session persist).

**Logs:**
```
ERROR [hermes_cli.auth] auth.json: atomic_replace denied (attempt 1/3, likely AV scan); retrying in 0.05s: [WinError 5] Access is denied
ERROR [hermes_cli.auth] auth.json: atomic_replace denied (attempt 2/3, likely AV scan); retrying in 0.10s: [WinError 5] Access is denied
ERROR [hermes_cli.auth] auth.json: atomic_replace denied (attempt 3/3, likely AV scan); retrying in 0.20s: [WinError 5] Access is denied
CRITICAL ent init failed: [WinError 5] Access is denied: ...
```

**RCA:**
Windows Defender real-time protection opens `auth.json` for scanning **during** the `os.replace(tmp, auth.json)` atomic rename. Defender holds the file with `FILE_SHARE_DELETE=No`, so the rename fails with `PermissionError` (WinError 5). The scan window is typically 50-200ms. The original code had no retry — it crashed on first collision.

The advisory cross-process lock (`_auth_store_lock` using `msvcrt.locking`) does NOT protect against Defender because Defender ignores POSIX advisory locks.

**Fix (two layers):**

1. **Python retry (durable, no admin needed)** — `hermes_cli/auth.py:_save_auth_store`:
   - Added `_AUTH_REPLACE_MAX_RETRIES` (default 5, env `HERMES_AUTH_REPLACE_MAX_RETRIES`)
   - Added `_AUTH_REPLACE_BACKOFF_SECONDS` (default 0.15s, env `HERMES_AUTH_REPLACE_BACKOFF_SECONDS`)
   - Exponential backoff: 0.15s → 0.30s → 0.60s → 1.20s → 2.40s
   - Total worst-case added latency before re-raise: ~4.65s
   - Catches `PermissionError`, logs attempt, sleeps, retries

2. **Defender exclusion (prevents recurrence)** — `C:\Users\Burgboy\AppData\Local\hermes\add-defender-exclusions.ps1`:
   - Adds folder exclusion for `%LOCALAPPDATA%\hermes` to Defender real-time protection
   - Must be run **elevated** (Run as Administrator)
   - Idempotent — safe to re-run

**Verification:**
1. Run the exclusion script elevated (one-time)
2. Restart Hermes Desktop
3. Watch `agent.log` — no more "atomic_replace denied" warnings
4. If you cannot elevate, the Python retry alone handles typical scan windows

**Env overrides (if needed):**
```powershell
$env:HERMES_AUTH_REPLACE_MAX_RETRIES = "7"
$env:HERMES_AUTH_REPLACE_BACKOFF_SECONDS = "0.20"
```

**Standing rule:**
Any `auth.json` write failure with WinError 5 on Windows = Defender scan race. Do NOT chmod/change ACLs — that breaks Defender further. Add the folder exclusion or increase retry budget.

---

## Closed tickets

### D-001 — Bot replies "Sorry, I encountered an unexpected error"  [FIXED 2026-07-21]

**Severity:** RED
**Opened / closed:** 2026-07-20 / 2026-07-21
**Symptom:**
Every Discord message produced "Sorry, I encountered an unexpected error.
Try again or use /reset to start a fresh session." Bot unusable.

**Logs:**
```
RuntimeError: No LLM provider configured. Run `hermes model` to select a
provider, or run `hermes setup` for first-time configuration.
```

**RCA:**
The `.env` file contained an empty `OPENROUTER_API_KEY=` line that was
overriding the working key hardcoded in `config.yaml` (`model.api_key`).
Env vars take precedence over config.yaml, so the runtime saw an empty key
and refused to start the agent.

Separately, the auxiliary client was marking OpenRouter unhealthy for 60s
on a "payment / credit error" — but the actual issue was the empty env var,
not a real billing problem. The key works fine (verified via
`/api/v1/auth/key` and a live free-model call).

**Fix:**
Synced `OPENROUTER_API_KEY` in `.env` from `config.yaml model.api_key` via
`d:\projects\hermes-agent-hotreload\apply-openrouter-key.py` (idempotent).

Verification after `hermes gateway restart`:
- User "Sleepy Cat" sent "yo" at 18:14:24
- Agent processed on `nvidia/nemotron-3-ultra-550b-a55b:free`
- Response delivered in 29.6s, 76 chars, no errors

**Standing rule:**
If `errors.log` shows "No LLM provider configured", check `.env` for empty
`<PROVIDER>_API_KEY=` lines first — they override `config.yaml`. Run
`apply-openrouter-key.py` to resync.

---

### D-002 — Z.AI 429 "Insufficient balance" code 1113 misclassified  [FIXED 2026-07-20]

**Severity:** RED
**Opened / closed:** 2026-07-20 / 2026-07-20
**Symptom:**
Z.AI / GLM API key out of credits produces a 429 storm that wastes ~10s
before failing. Bot shows "Rate limited" status for minutes.

**Logs:**
```
provider=zai base_url=https://api.z.ai/api/paas/v4 model=glm-5
summary=HTTP 429: Insufficient balance or no resource package. Please recharge.
```

**RCA:**
Z.AI / Zhipu mislabels account-balance exhaustion as HTTP 429 (standards-
correct would be 402). Body: `{"error":{"code":"1113","message":"Insufficient
balance..."}}`. The `_classify_by_status(429)` in `agent/error_classifier.py`
checked `_OVERLOADED_PATTERNS` but not `_BILLING_PATTERNS`, so the error
fell through to `rate_limit` and burned 3 retries.

**Fix:**
See CRITICAL-ISSUES.md #16. Patched runtime `error_classifier.py`:
- Added `_BILLING_PATTERNS` check in the 429 branch
- Added code "1113" to `_BILLING_ERROR_CODES`

After fix: classifies as `billing`, rotates credential immediately, 0 wasted
retries. Regression: real rate-limit 429s and Z.AI overload 429s (code 1305)
still classify correctly.

**Standing rule:**
A 429 with "balance" / "recharge" / "quota" / "credits" in the body is NEVER
a transient rate limit — it's billing. Recharge Z.AI at https://z.ai/ or
remove GLM_API_KEY and rely on the fallback chain.

---

### D-003 — Kimi 403 "usage limit for this billing cycle" misclassified  [FIXED 2026-07-20]

**Severity:** RED
**Opened / closed:** 2026-07-20 / 2026-07-20
**Symptom:**
Kimi Coding Plan quota exhaustion produced a confusing "Non-retryable
client error" and never triggered the billing recovery path. Bot just died.

**Logs:**
```
provider=kimi-coding base_url=https://api.kimi.com/coding model=kimi-k2.7-code
summary=HTTP 403: You've reached your usage limit for this billing cycle.
Your quota will be refreshed in the next cycle. To continue now, purchase
extra usage or upgrade your plan: https://www.kimi.com/code/#pricing
```

**RCA:**
The 403 branch in `error_classifier.py` only matched `"key limit exceeded"`,
`"spending limit"`, and `_BILLING_PATTERNS`. Kimi's wording — "usage limit
for this billing cycle" / "quota will be refreshed" / "purchase extra
usage" — matched none of those, so it fell through to the `auth` branch.

**Fix:**
See CRITICAL-ISSUES.md #17. Added three Kimi-specific phrases to the 403
billing branch. After fix: classifies as `billing`, activates fallback
chain immediately.

**Standing rule:**
A 403 with "usage limit" + "billing cycle" / "quota will be refreshed" /
"purchase extra usage" is billing exhaustion, not auth. Upgrade the Kimi
plan at https://www.kimi.com/code/#pricing or wait for the cycle reset.

---

### D-004 — GLM-5 / GLM-5.2 "hangs without doing anything"  [FIXED 2026-07-20]

**Severity:** YELLOW
**Opened / closed:** 2026-07-20 / 2026-07-20
**Symptom:**
GLM models sometimes appear to hang indefinitely — no streaming output,
no error, eventually a "Stream stale for 180s — killing connection".

**RCA:**
GLM-5.2 ships with thinking ON by default and pauses minutes before its
first content token. The default `HERMES_STREAM_STALE_TIMEOUT` of 180s and
httpx read timeout of 120s both fired BEFORE GLM-5.2 finished thinking.
GLM models were missing from `_REASONING_STALE_TIMEOUT_FLOORS` in
`agent/reasoning_timeouts.py`.

**Fix:**
See CRITICAL-ISSUES.md #18. Added GLM-5.2 (300s), GLM-5 (240s), GLM-4.6/4.5
(180s) to the reasoning-timeout floors. Slug-anchored so `glm-4-9b`
(non-thinking) is excluded.

**Standing rule:**
If a reasoning model is added to Hermes and pauses minutes before its
first content token, add it to `_REASONING_STALE_TIMEOUT_FLOORS`. Slug
must be start-of-slug anchored (use `glm-5.2`, not `5.2`).

---

## Operational notes

### Restart sequence (when applying patches)

```powershell
# 1. Apply runtime patches (idempotent)
C:\Users\<user>\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe `
    d:\projects\hermes-agent-hotreload\apply-runtime-patches.py

# 2. Sync OPENROUTER_API_KEY if .env was reset
C:\Users\<user>\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe `
    d:\projects\hermes-agent-hotreload\apply-openrouter-key.py

# 3. Restart gateway
C:\Users\<user>\AppData\Local\hermes\hermes-agent\venv\Scripts\hermes.exe `
    gateway restart

# 4. Verify Discord connected
C:\Users\<user>\AppData\Local\hermes\hermes-agent\venv\Scripts\hermes.exe `
    gateway status
```

### Quick diagnostic checklist

When the bot misbehaves, run in this order:

1. **`errors.log` tail** — look for the most recent stack trace
   ```powershell
   Get-Content "$env:LOCALAPPDATA\hermes\logs\errors.log" -Tail 40
   ```
2. **`agent.log` tail** — find the inbound message + agent response
   ```powershell
   Get-Content "$env:LOCALAPPDATA\hermes\logs\agent.log" -Tail 50
   ```
3. **Gateway status** — confirm Discord is connected
   ```powershell
   hermes gateway status
   ```
4. **Provider key check** — confirm OPENROUTER_API_KEY is set
   ```powershell
   hermes auth status
   ```
5. **Provider health** — try a free-model call manually
   ```powershell
   hermes chat --provider openrouter --model nvidia/nemotron-3-super-120b-a12b:free
   ```

### Fallback chain (current)

Primary: `nvidia/nemotron-3-ultra-550b-a55b:free` via openrouter
Failover order (one-by-one, all free OpenRouter):
1. `nvidia/nemotron-3-ultra-550b-a55b:free`
2. `nvidia/nemotron-3-super-120b-a12b:free`
3. `qwen/qwen3-coder:free`
4. `google/gemma-4-31b-it:free`  ← vision-capable, preserves image input
5. `google/gemma-4-26b-a4b-it:free`
6. `deepseek/deepseek-r1:free`
7. `meta-llama/llama-4-70b-instruct:free`
8. `mistralai/mistral-small-3.2-24b-instruct:free`

Tier 2 (commented out in config.yaml, activate with OPENCODE_GO_API_KEY):
- `glm-5` via opencode-go
- `kimi-k2.5` via opencode-go

Tier 3 (commented out, activate with OPENCODE_ZEN_API_KEY):
- `gemini-3-flash` via opencode-zen

### Known account states (as of 2026-07-21)

| Provider | Key | Account state | Action |
|----------|-----|---------------|--------|
| openrouter | set | working (free tier) | none |
| zai (GLM) | set | OUT OF BALANCE | recharge at https://z.ai/ or ignore |
| kimi-coding | set | AT USAGE LIMIT (cycle) | wait for reset or upgrade at https://www.kimi.com/code/#pricing |
| deepseek | set | unknown | untested recently |
| sakana | set | unknown | untested recently |
| opencode-go | NOT set | n/a | get key at https://opencode.ai/auth |
| opencode-zen | NOT set | n/a | get key at https://opencode.ai/auth |

---

— End of document —
