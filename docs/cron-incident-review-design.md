# Cron Incident Review (CIR) — Phase 0 Design & Discovery Lock

**Bead:** hermes-agent-dv9.1
**Branch:** palmi/cir-phase0-design
**Status:** Phase 0 — design lock; refreshed for current main on 2026-08-25. Slice 1 (durable incidents, signature dedup, ack, `hermes cron incidents` CLI) is proposed in **PR #94692 (open)** — see §8.
**Date:** 2026-06-26 (refreshed 2026-08-25)

---

## 1. Summary and Product Goal

**Problem:** When a Hermes cron job fails, the job owner learns about it — if at all — through a terse `last_error` field in `jobs.json` or a raw error message delivered to a chat platform. There is no structured post-mortem, no actionable options, and no way to dismiss, retry, or pause the job from the same alert. Repeated failures produce repeated noise with no escalation or deduplication.

**Goal:** Convert any failed cron run into a durable, redacted, actionable **Cron Incident Review** — a compact summary delivered to the owner's channel with one-touch action tokens (acknowledge, retry, pause, snooze), a deduplication window to suppress flapping, and optionally an owner-agent review conversation for deeper diagnosis.

**Non-goals (Phase 0):**
- No new core model tool (per Footprint Ladder in AGENTS.md)
- No external dependencies
- No mutations to live job config, gateway config, credentials, or profile state
- No implementation — this document locks design only

**Relationship to existing Beads:** dv9.1 is this discovery/design lock. dv9.2–dv9.10 are implementation slices; see §8 for phasing (dv9.2–dv9.4 + dv9.7a are proposed in PR #94692).

---

## 2. Current Code Path Map

### 2.1 Cron Model and Storage

**File:** `cron/jobs.py`

- `HERMES_DIR = get_hermes_home().resolve()` (line 80) and `CRON_DIR = HERMES_DIR / "cron"` (line 84) — always uses the profile-resolved Hermes home, never a raw cron path. Required for profile isolation.
- `JOBS_FILE = CRON_DIR / "jobs.json"` (line 85) — single-file JSON store; cross-process locking via `_jobs_lock()` (line 273) which combines `fcntl.flock` on Unix / `msvcrt.locking` on Windows with a threading `RLock` for in-process safety.
- `OUTPUT_DIR = CRON_DIR / "output"` (line 117) — per-run output stored at `OUTPUT_DIR/{job_id}/{timestamp}.md`.
- `save_job_output(job_id, output)` (line 3897) — atomic write + fsync, permissions 0600, validates `job_id` has no path traversal.
- `mark_job_run(job_id, success, error, delivery_error)` (line 2552) — independently tracks `last_error` (job execution failure) and `last_delivery_error` (channel delivery failure). These are already separate fields.
- `create_job()` (line 1915) — job fields include `last_error`, `last_delivery_error`, `last_status` ("ok"/"error"), `state` ("scheduled"/"paused"/"completed"/"error"), `repeat`, `deliver`, `no_agent`, `script`, `enabled_toolsets`.
- `advance_next_run(job_id)` (line 3064) — pre-advances before execution for at-most-once semantics on recurring jobs.
- `get_due_jobs()` (line 3314) — grace window handling; fast-forwards stale missed runs.

**Key invariant for CIR:** `last_error` and `last_delivery_error` are already tracked independently. CIR adds `last_incident_id` and `last_incident_at` to the job dict — no schema collision.

### 2.2 Scheduler and Runner

**File:** `cron/scheduler.py`

**Main execution path:**

```
tick()
  └─ get_due_jobs()         → jobs.py
  └─ advance_next_run()     → jobs.py
  └─ _process_job(job)      → scheduler.py thin wrapper
       └─ run_one_job(job)  → scheduler.py end-to-end pipeline
       ├─ run_job(job)       → (success, output_doc, final_response, error_msg)
       ├─ save_job_output()  → jobs.py
       ├─ _deliver_result()  → None (success) | str (error)
       └─ mark_job_run()     → jobs.py
```

`tick()` (line 7263): file-locked, partitions due jobs into parallel pool (ThreadPoolExecutor, for non-workdir jobs) and sequential pool (single thread, for workdir jobs — because `os.environ` mutation is not thread-safe).

`run_job(job) → tuple[bool, str, str, Optional[str]]` (line 5063):
- Returns `(success, full_output_doc, final_response, error_message)`
- `full_output_doc` is what gets stored; `final_response` is what gets delivered

`_process_job(job)` (line 7463) is only a thin wrapper that calls `run_one_job(job)`. `run_one_job(job)` (line 6663) is the shared firing body extracted from `tick`'s per-job closure, used by both the built-in ticker and external providers (e.g. Chronos). It delegates to `_run_one_job_body` (line 6726), which owns the execute → save → deliver → mark pipeline: it calls `run_job()` (line 6821), `save_job_output()` (line 6888), `_deliver_result()` (line 6996), and `mark_job_run()` (lines 7023, 7080). CIR hooks in `_run_one_job_body()` **after** `run_job()` returns and **before** `mark_job_run()` persists state, so it can see both job execution errors and delivery errors.

`_deliver_result(job, content, adapters=None, loop=None) → Optional[str]` (line 2856):
- Returns `None` on success, error string on failure
- Resolves delivery targets via `_resolve_delivery_targets(job)` → list of `{platform, chat_id, thread_id}` dicts
- Prefers live adapter (dispatches via asyncio event loop); falls back to standalone HTTP adapter
- CIR alert delivery follows the same path with a CIR-specific payload

### 2.3 Script / No-Agent Boundary

`run_job()` checks `job.get("no_agent")` near the top of the function. If `True`:
- Calls `_run_job_script(script_path)` directly
- `_run_job_script()`: validates script is under `get_hermes_home() / "scripts"` only; runs bash for `.sh`/`.bash`, Python otherwise; applies `redact_sensitive_text()` to stdout/stderr
- **No AIAgent is constructed** — this path produces a raw script output string, not an LLM-generated summary
- CIR must handle `no_agent=True` jobs: the failure incident still gets captured, but the "agent review" trigger (dv9.6) should be skipped or made opt-in

### 2.4 Agent-Run Boundary

When `no_agent` is false/absent, `run_job()`:
1. Assembles prompt via `_build_job_prompt(job, prerun_script)`
2. `_build_job_prompt()` calls `_scan_assembled_cron_prompt()` — raises `CronPromptInjectionBlocked` on hit; this is a failure CIR must catch and classify
3. Constructs `from run_agent import AIAgent` (lazy import inside `run_job`)
4. Instantiates `AIAgent` with `platform="cron"`, `skip_memory=True`, `quiet_mode=True`
5. Sets `os.environ["HERMES_CRON_SESSION"] = "1"` process-wide for the lifetime of the scheduler process, as the current scheduler comment states
6. Runs `agent.run_conversation(prompt)` in a thread with inactivity timeout (default 600s, polls every 5s, raises `TimeoutError`)
7. Agent failure detection: `result.get("failed") is True` or `result.get("completed") is False`
8. `SILENT_MARKER = "[SILENT]"` (line 554): agent can suppress delivery by prepending this to its response; CIR must recognize `[SILENT]` responses as success, not as incidents

### 2.5 Delivery Pipeline

`_deliver_result()` uses ContextVars set per-job:
- `HERMES_CRON_AUTO_DELIVER_PLATFORM`
- `HERMES_CRON_AUTO_DELIVER_CHAT_ID`
- `HERMES_CRON_AUTO_DELIVER_THREAD_ID`

`_KNOWN_DELIVERY_PLATFORMS` frozenset: telegram, discord, slack, whatsapp, signal, matrix, mattermost, homeassistant, dingtalk, feishu, wecom, weixin, sms, email, webhook, bluebubbles, qqbot, yuanbao

`_HOME_TARGET_ENV_VARS`: maps platform to env var naming the home channel (e.g., discord → `DISCORD_HOME_CHANNEL`).

CIR alerts use the same `_deliver_result()` path. The only extension needed is a CIR-specific content formatter that packages the incident card.

### 2.6 Discord Capabilities

**File:** `plugins/platforms/discord/adapter.py`

`async def send(chat_id, content, reply_to=None, metadata=None) → SendResult` — text-only, splits on `MAX_MESSAGE_LENGTH`, supports thread_id via metadata.

**Discord UI Views exist and are already used:** The adapter has full `discord.ui` infrastructure:
- `ExecApprovalView` (line 8787): `Allow Once`, `Allow Session`, `Always Allow`, `Deny` buttons → sent via `channel.send(embed=embed, view=view)` in `send_exec_approval()` (line 7457)
- `SlashConfirmView` (line 8950): `Approve Once`, `Always Approve`, `Cancel` → sent via `send_slash_confirm()` (line 7559)
- `UpdatePromptView` (line 9065): `Yes` / `No` buttons
- `ModelPickerView` (line 9161): Select dropdown

These are **separate methods** that call `channel.send(embed=embed, view=view)` directly — outside the plain `send()` path. This means a new `send_cron_review(chat_id, incident, token, metadata)` method following the same pattern is feasible without touching the main `send()` contract.

Authorization: `_allowed_user_ids: set` and `_allowed_role_ids: set` — the existing button authorization gates are reusable for CIR action buttons.

Slash commands such as `/reset`, `/status`, `/stop`, `/new`, and `/model` are registered via `app_commands`. A `/cron-review <token>` slash command follows the same slash-command dispatch pattern — this is the cross-platform fallback. `/ask` appears in adapter docs but is not currently registered as an `app_commands` slash command, so CIR should not model itself on `/ask`.

### 2.7 Command Fallback Feasibility

CIR action delivery has two paths (bot transport):

1. **Discord button path (preferred for Discord):** New `send_cron_review()` method on `DiscordAdapter` that sends a `discord.Embed` + `CronReviewView` (new `discord.ui.View`). Buttons: `Acknowledge`, `Retry`, `Pause`, `Snooze`. ActionToken lives in button `custom_id`. Follows `send_exec_approval()` pattern exactly.

2. **Command fallback (cross-platform):** `/cron-review <token>` slash command. The ActionToken is HMAC-signed and URL-safe; the user pastes it or Discord presents it as a copyable code block. Any Hermes-connected platform can handle it via the CLI `cron review <token>` command.

### 2.8 Profile-Scoped Launch Options

`hermes_cli/main.py` (dispatch: `build_cron_parser(subparsers, cmd_cron=cmd_cron)` at line 13033; `_apply_profile_override()` at line 520): `_apply_profile_override()` sets `HERMES_HOME` env var **before imports** and is process-global. This means:

- **In-process cross-profile launch is not viable** in the gateway: mutating `HERMES_HOME` mid-process would corrupt the gateway's profile context (and violate the thread-safety of workdir jobs in the sequential pool).
- **Canonical path for owner-agent review session (dv9.6):** Spawn a subprocess via `hermes -p <owner_profile> cron review-agent <incident_id>` with a controlled env. The subprocess inherits the correct `HERMES_HOME` for the owner profile.
- The subprocess must be allowlisted (by profile name, not open-ended) and loop-guarded to prevent recursion.
- Owner review runs as an ephemeral session: `AIAgent(skip_memory=True, platform="cron")` with a fresh `session_id` — **never injected into a live gateway conversation** (this preserves the prompt-cache invariant).

---

## 3. Failure Taxonomy and Stage Mapping

| Stage | Trigger | `success` | CIR Class | Source |
|---|---|---|---|---|
| **prompt-build** | `CronPromptInjectionBlocked` | False | `SECURITY_BLOCK` | `_build_job_prompt()`, `scheduler.py` |
| **prompt-build** | `context_from` chain failure | False | `CONTEXT_FETCH_FAIL` | `_build_job_prompt()` |
| **script-exec** | Non-zero exit / exception | False | `SCRIPT_ERROR` | `_run_job_script()`, `scheduler.py` |
| **agent-init** | `AIAgent` import / init error | False | `AGENT_INIT_ERROR` | `run_job()`, `scheduler.py` |
| **agent-run** | `TimeoutError` (600s inactivity) | False | `AGENT_TIMEOUT` | `run_job()`, `scheduler.py` |
| **agent-run** | `result.get("failed") is True` | False | `AGENT_FAILURE` | `run_job()` |
| **agent-run** | `result.get("completed") is False` | False | `AGENT_INCOMPLETE` | `run_job()` |
| **delivery** | `_deliver_result()` returns error str | True | `DELIVERY_FAIL` | `_deliver_result()`, `scheduler.py` |
| **delivery** | Platform adapter raises | True | `DELIVERY_FAIL` | `_deliver_result()` |
| **post-run** | success + delivered, but required artifacts/output absent | True | `DATA_MISSING` | post-run artifact check, `_run_one_job_body()` (line 6726) |
| **suppressed** | Agent emits `[SILENT]` prefix | True (silent) | — (not an incident) | `SILENT_MARKER`, `scheduler.py` |

**Notes:**
- `success=True` with `DELIVERY_FAIL` is already tracked via the separate `last_delivery_error` field.
- `DATA_MISSING` (artifact-invariant): the run reported `success=True` and delivery succeeded, but a required artifact (report file, output doc, HTML, etc.) was not produced. Field data (2026-07-09, `daily-health-report`) showed an 80-minute silent-ok: `last_status=ok`, delivered fine, but the report file and HTML were absent and the saved output doc contained only the prompt dump. This is a 4th `last_status` outcome beyond `ok` / `handoff` / `error`, and requires per-job `required_artifacts` config that the scheduler verifies post-run (see §10).
- `SECURITY_BLOCK` incidents must be flagged at elevated severity — they indicate prompt injection attempts or policy violations.
- `no_agent=True` jobs skip agent stages; only `SCRIPT_ERROR` and `DELIVERY_FAIL` apply.

### 3.1 Design Decision: Artifact Verification (DV-ARTIFACT)

**Decision:** A cron run is not considered fully healthy merely because it returned `success=True` and delivered. CIR adds an artifact-invariant check: after a successful run, verify that every configured required artifact exists (and is non-empty where a size floor is set). If any required artifact is missing or empty, record the run as a `DATA_MISSING` incident even though `last_status` is `ok`.

**Coverage:**
- Per-job config: `required_artifacts` (list of glob/paths relative to the job's output dir, `{HERMES_HOME}/cron/output/{job_id}/`, and optionally absolute artifact paths, plus an optional minimum size).
- Hook site: post-run, inside `_run_one_job_body()` (line 6726) after `run_job()` returns and after `save_job_output()`, before `mark_job_run()` — same window as the capture hook so `mark_job_run()` records the incident in the same run.
- This is a deliberate over-alert vs. the current silent-ok: a missing artifact after an "ok" run is exactly the case that is currently invisible to the operator (80-minute silent window in the 2026-07-09 field report).
- Out of scope for #94692; tracked as dv9.10 (see §8.2).

---

## 4. Proposed Data Contracts

### 4.1 CronIncident v1

```python
# Stored at: {HERMES_HOME}/cron/incidents/{incident_id}.json
# Permissions: 0600 (same as job output)

{
  "v": 1,
  "incident_id": str,        # ulid or uuid4 — stable handle for dedup/token binding
  "job_id": str,
  "profile": str,            # get_hermes_home() resolved at capture time
  "occurred_at": float,      # unix timestamp (UTC)
  "stage": str,              # one of the Stage values in §3
  "class": str,              # one of the CIR Class values in §3
  "severity": str,           # "low" | "medium" | "high" | "critical"
  "normalized_error": str,   # see §7 — deterministic string for dedup signature
  "stack_top": str | null,   # top frame from traceback, redacted
  "raw_error": str | null,   # redacted via redact_sensitive_text() from agent/redact.py
  "output_path": str | null, # relative path under OUTPUT_DIR if output was saved
  "action_tokens": {         # keyed by action name
    "acknowledge": str,      # HMAC-signed ActionToken
    "retry": str,
    "pause": str,
    "snooze": str
  },
  "state": str,              # lifecycle state — see §5
  "state_history": [         # append-only list of lifecycle transitions
    {"from": str, "to": str, "at": float, "by": str | null}
  ],
  "dedup_key": str,          # normalized signature used for window suppression (§7)
  "suppressed_count": int    # how many duplicate incidents were absorbed
}
```

**Storage decision:** The original Phase 0 proposal was one JSON file per incident under `{HERMES_HOME}/cron/incidents/{incident_id}.json`, with a lightweight `.index.json` for dedup lookups, avoiding rewrites of a global `incidents.json`, keeping individual writes atomic, and following the existing profile-local cron storage pattern (file locking as in `_jobs_lock()`). **PR #94692 (open) supersedes this layout** with a `cron_incidents` table in `executions.db` (same WAL-safe pattern as the executions ledger), keyed by `error_sig = sha256(job_id + normalized_error)`. Future slices build on the SQLite store; the JSON-file layout is retained here only as the Phase 0 provenance.

**Redaction:** `raw_error` and `stack_top` MUST pass through `redact_sensitive_text()` from `agent/redact.py` before storage. The `_REDACT_ENABLED` flag is snapshot at import — this is on by default and must not be bypassed. #94692 applies the same redactor and truncates fields before write.

### 4.2 ActionToken v1

```python
# Compact, URL-safe, HMAC-signed token — safe to embed in button custom_id or CLI arg

{
  # Encoded as: base64url(json_payload) + "." + base64url(hmac_sig)
  # HMAC key: per-profile secret stored at {HERMES_HOME}/cron/.token_secret (0600)
  #   Generated once on first use; never committed or logged

  # Payload (before encoding):
  "v": 1,
  "incident_id": str,
  "action": str,             # "acknowledge" | "retry" | "pause" | "snooze"
  "exp": float,              # unix timestamp expiry (default: 7 days)
  "nonce": str,              # random 8-byte hex — replay protection

  # Consumed nonces stored at: {HERMES_HOME}/cron/.token_nonces.json (with TTL cleanup)
}
```

**Replay protection:** Nonces stored in `{HERMES_HOME}/cron/.token_nonces.json` with TTL equal to token expiry. Expired nonces are pruned on each read. File is 0600.

**Scope:** One ActionToken per action per incident. Tokens are invalidated when an incident transitions to terminal state (acknowledged, resolved).

**Token-secret decision:** The per-profile HMAC key lives at `{HERMES_HOME}/cron/.token_secret` with 0600 permissions, generated on first use, and never included in logs, incident payloads, or cron listing output.

### 4.3 CronReviewRequest v1 (dv9.6 extension)

This contract is **not** part of the dv9.2 base incident schema. It is introduced by dv9.6 when owner-agent review is implemented. dv9.6 may also extend `CronIncident` with an optional `review_session_id` field or store that relation in a separate review-session record.

```python
# Passed to owner-agent subprocess (dv9.6) via stdin or temp file

{
  "v": 1,
  "incident_id": str,
  "incident": CronIncident,  # full incident object (redacted)
  "job": {                   # safe subset of job dict — no credentials, no script body
    "id": str, "name": str, "schedule": str, "last_status": str,
    "last_run": str | null, "repeat": bool, "no_agent": bool
  },
  "output_excerpt": str | null,  # last N lines of output_path, redacted
  "context": str | null          # additional owner-supplied context (future)
}
```

The full job dict is not passed to avoid leaking credential fields or raw script content. Output excerpt is truncated and redacted.

### 4.4 Lifecycle Transition Events

Appended to `state_history` on each transition (see §5). Also emitted as structured log entries for observability:

```python
{
  "event": "cir.transition",
  "incident_id": str,
  "from_state": str,
  "to_state": str,
  "at": float,
  "by": str | null,    # "system" | "owner:{action}" | "agent:{session_id}"
  "note": str | null
}
```

---

## 5. State Machine

```
                      ┌────────────────────────────────────────┐
                      │                                        │
  [failure detected]  ▼                                        │
  ────────────────► OPEN                                       │
                      │                                        │
                      │  duplicate within dedup window         │
                      │  updates occurred_at +                 │
                      │  suppressed_count in place             │
                      │  (no new incident/state transition)    │
                      │                                        │
               owner action                                    │
                      │                                        │
          ┌───────────┼──────────────────────┐                 │
          │           │                      │                 │
          ▼           ▼                      ▼                 │
    ACKNOWLEDGED   PAUSED             SNOOZED                  │
          │           │               (until snooze_until)     │
          │           │                      │                 │
          ▼           └──────────────────────┤                 │
       CLOSED                         re-open on next fail     │
    (terminal)                        or snooze expiry         │
                                                               │
  RETRY action → dispatches job re-run → OPEN or CLOSED ───────┘
```

**States:**

| State | Meaning |
|---|---|
| `OPEN` | Incident captured; alert delivered or pending delivery |
| `ACKNOWLEDGED` | Owner confirmed receipt; no further alerting |
| `PAUSED` | Owner paused the job; incident stays open until manually resumed |
| `SNOOZED` | Alert suppressed until `snooze_until` timestamp |
| `CLOSED` | Terminal — resolved, retried successfully, or manually dismissed |

`SUPPRESSED` is intentionally **not** a lifecycle state. A duplicate within the dedup window is an absorption event on the existing `OPEN` incident: it increments `suppressed_count`, updates `occurred_at`, and skips alert delivery. No new incident record is created.

**Transitions:**

| From | To | Trigger |
|---|---|---|
| — | `OPEN` | `run_one_job()` detects failure after `run_job()` returns |
| `OPEN` | `ACKNOWLEDGED` | `acknowledge` ActionToken redeemed |
| `OPEN` | `PAUSED` | `pause` ActionToken redeemed |
| `OPEN` | `SNOOZED` | `snooze` ActionToken redeemed |
| `OPEN` | `CLOSED` | `retry` succeeds OR manual resolve |
| `SNOOZED` | `OPEN` | Snooze window expires and next failure occurs |
| `ACKNOWLEDGED` | `OPEN` | Next failure on same job (new incident) |
| `PAUSED` | `OPEN` | Job re-enabled; next failure |
| Any | `CLOSED` | Manual dismiss or owner-agent resolves |

---

## 6. Security and Threat Model

### 6.1 CIR-Specific Attack Surfaces

**Prompt injection via error messages**
Failure output (stderr, agent error text) is included in CIR alerts and possibly in the owner-agent review prompt. The `_scan_assembled_cron_prompt()` scanner (already in `_build_job_prompt()`) does not cover CIR review prompts. Required: CIR review prompt must also pass through injection scanning before being sent to the review agent (dv9.6).

**Secret leakage in incident storage**
`raw_error` and `stack_top` may contain credentials from tracebacks. All fields derived from error output must go through `redact_sensitive_text()` from `agent/redact.py` before being written to the incident store (the `cron_incidents` table in `executions.db` per #94692, or `incidents/*.json` in the Phase 0 layout) or logged. `_REDACT_ENABLED` is set at import time from `HERMES_REDACT_SECRETS`; if an operator starts Hermes with redaction disabled, CIR must still redact incident fields with a force-redaction path or a dedicated CIR redactor rather than trusting the global opt-out.

**ActionToken forgery**
Tokens are HMAC-signed with a per-profile secret key (never shared across profiles). Key stored at `{HERMES_HOME}/cron/.token_secret` (0600, generated on first use). Token expiry is enforced; replay prevention via nonce store. Token scope is bound to `(incident_id, action)` — a `pause` token cannot be used to `retry`.

**ActionToken enumeration / brute-force**
`nonce` is 8-byte random; `incident_id` is a uuid4 or ulid. Combined with HMAC, the attack surface is effectively `2^64` for enumeration — infeasible.

**Cross-profile incident injection**
Each profile has its own `HERMES_HOME` and therefore its own `incidents/` directory. CIR must use `get_hermes_home()` (not a raw path) everywhere. The owner-agent subprocess launch (dv9.6) must be gated to an explicit `owner_profiles` allowlist in `config.yaml` — no open-ended profile routing.

**Discord button click by unauthorized user**
CIR's `CronReviewView` must reuse the existing `_allowed_user_ids` / `_allowed_role_ids` gates from `ExecApprovalView`. Button handler checks authorization before acting on the token. Unauthorized clicks are logged and silently rejected.

**Recursive review loops**
Owner-agent review (dv9.6) runs as a subprocess. If the review subprocess itself fails and triggers CIR, it must not trigger another review (infinite loop). Guard: `HERMES_CIR_REVIEW=1` env var on the subprocess; `_process_job()` skips CIR escalation when this var is set.

**File permission and path traversal**
`incident_id` is a uuid4/ulid — no user-controlled path component. In the Phase 0 JSON layout, incident files are written with 0600 permissions via the same `_secure_file()` pattern from `cron/jobs.py` and the `incidents/` directory is 0700 via `_secure_dir()`; the landed #94692 SQLite store follows the WAL-safe pattern of the existing `executions.db` ledger instead.

### 6.2 Invariants CIR Must Not Break

- No mutation of `jobs.json` except via `mark_job_run()` / `pause_job()` / `resume_job()` from `cron/jobs.py` (existing lock-protected functions)
- No mutation of live gateway config or credentials
- No cross-profile data access without explicit allowlist
- `HERMES_REDACT_SECRETS` is read at module import time, but CIR incident storage still treats redaction as mandatory even if the wider runtime was started with redaction disabled

---

## 7. Deduplication and Alert Policy

### 7.1 Failure Signature (Normalized Dedup Key)

**Landed in #94692 (open):** signature dedup ships as `error_sig = sha256(job_id + normalized_error)` on the `cron_incidents` table; a changed error mints a new incident and ack is per-signature. The Phase 0 key below is the fuller normalization proposal (adds stage/stack_top/profile into the signature); #94692 implements the `job_id + normalized_error` core.

```python
# Normalized failure signature for dedup window comparison
dedup_key = sha256(f"{profile}|{job_id}|{stage}|{normalized_error}|{stack_top}").hexdigest()[:16]
```

**Normalization rules:**
- `normalized_error`: strip memory addresses (`0x[0-9a-f]+`), strip UUIDs, strip timestamps, lowercase, collapse whitespace. Goal: two instances of the same underlying error produce the same normalized string.
- `stack_top`: top frame of traceback only (file + function, not line number — line numbers shift with code changes). Null for non-exception failures.
- `profile`: resolved from `get_hermes_home()` at capture time.

### 7.2 Dedup Window

Default: **1 hour** (configurable in `config.yaml` under `cron.cir.dedup_window_seconds`). Config key, not env var (per AGENTS.md convention).

- On each new failure, compute `dedup_key`
- Look up open incidents with matching `dedup_key` and `occurred_at > now - dedup_window`
- If found: increment `suppressed_count`, update `occurred_at`, skip new alert delivery
- If not found: create new `CronIncident`, deliver alert

### 7.3 Alert Policy

| Condition | Action |
|---|---|
| First failure (no open incident) | Create incident, deliver alert immediately |
| Repeated failure within dedup window | Absorb (suppressed_count++), no new alert |
| Repeated failure after window expires | New incident, new alert |
| `SECURITY_BLOCK` class | Always alert immediately, ignore dedup window |
| `DELIVERY_FAIL` only (job itself succeeded) | Alert at reduced severity, once per job per day |
| Incident in `SNOOZED` state | Skip alert until snooze expires |

### 7.4 Alert Content Format

```
[CRON INCIDENT] {job_name} · {stage}/{class}
Occurred: {occurred_at ISO}
Error: {normalized_error} (redacted)
Incident ID: {incident_id}

Actions: Acknowledge · Retry · Pause · Snooze 1h
  [Discord: buttons]  [Other: /cron-review <token>]
```

The alert omits raw stack traces and full output from the notification. `hermes cron incidents list` / `ack` are already proposed in #94692; a per-incident detail view (`hermes cron incidents show <incident_id>`) remains roadmap (dv9.7b).

---

## 8. Implementation PR Slicing — dv9.2 to dv9.10

Slice 1 of the plan (durable incidents, signature dedup, ack, and the `hermes cron incidents` CLI list/ack) has landed as **PR #94692 (open, not merged)** — it introduced `cron/incidents.py`, the scheduler failure-path upsert, and the incidents CLI. The table below marks what that PR already proposes; everything else is the remaining roadmap that builds on it.

### 8.1 Landed in PR #94692 (open)

| Bead | Title | Module(s) | Key Test |
|---|---|---|---|
| **dv9.2** | Incident model + store | `cron/incidents.py` (new in #94692): `cron_incidents` table in `executions.db`, lifecycle `detected → alerted → reviewed → closed` | Unit: create/read/update incident; 0600 / DB write; path traversal rejection |
| **dv9.3** | Redaction + normalization + dedup | `cron/incidents.py` (#94692): `error_sig = sha256(job_id + normalized_error)`, per-signature dedup | Unit: normalization idempotency; secret-containing errors redacted before storage; dedup key stability across re-runs |
| **dv9.4** | Scheduler integration — capture hook + ack-gated alerting | `cron/scheduler.py`: failure path upserts incident and gates the per-run delivery alert on ack state | Integration: fake-job run that fails → incident written; `[SILENT]` runs → no incident; `no_agent=True` failures → incident with correct stage |
| **dv9.7a** | CLI surface — `hermes cron incidents` list/ack | `hermes_cli/subcommands/cron.py` (`build_cron_parser`, line 15) + `hermes_cli/cron.py` (`cron_command`) | CLI: `incidents list` (with `--state` filter), `incidents ack <id>` |

### 8.2 Remaining roadmap (builds on #94692)

| Bead | Title | Module(s) | Key Test |
|---|---|---|---|
| **dv9.5** | ActionToken generation + validation | `cron/action_token.py` (new) | Unit: token roundtrip; expired token rejected; wrong action rejected; replayed nonce rejected; forged HMAC rejected |
| **dv9.6** | Owner-agent review session | `cron/review_agent.py` (new); subprocess launch | Integration: `HERMES_CIR_REVIEW=1` blocks recursive CIR; subprocess env isolation; `CronReviewRequest` schema validation |
| **dv9.7b** | CLI token redemption — `cron review <token>` | `hermes_cli/subcommands/cron.py` (extend the parser landed in #94692) | CLI: `cron review <valid_token>` → prints confirmation, incident ACKed; expired token → clear error, no state change |
| **dv9.8** | Discord CIR delivery — `send_cron_review()` + `CronReviewView` | `plugins/platforms/discord/adapter.py` | Integration: button click → token redeemed → incident state transitions; unauthorized click rejected |
| **dv9.9** | Intelligence — auto-classify + summary | `cron/incidents.py` + `cron/scheduler.py` | Integration: classification labels on incidents; agent-generated summary stored; summary quality (no secret leakage) |
| **dv9.10** | Artifact verification — `data_missing` | `cron/scheduler.py` (post-run artifact check in `_run_one_job_body`, line 6726) + `cron/incidents.py` | Integration: success + delivered but required artifact absent → incident class `DATA_MISSING` (see §3, §10) |

**Dependency chain (hard ordering required):**
- dv9.3 depends on dv9.2 (needs incident model)
- dv9.4 depends on dv9.3 (needs dedup engine)
- dv9.5 is independent after dv9.2
- dv9.6 depends on dv9.5 (needs token), dv9.4 (needs incident state)
- dv9.7b depends on dv9.5 (token redemption CLI); the list/ack half is already in #94692
- dv9.8 depends on dv9.5 and dv9.7b (reuses token validation, Discord auth gates)
- dv9.9 can parallelize with dv9.6–dv9.8 in a separate branch if needed
- dv9.10 depends on dv9.4 (hooks into the captured incident path)

**New files to create (remaining roadmap):**
- `cron/action_token.py` — ActionToken generation/validation (dv9.5)
- `cron/review_agent.py` — owner-agent subprocess orchestration (dv9.6)

**Already landed in #94692 (extend, do not re-plan):**
- `cron/incidents.py` — model, storage, dedup, normalization, lifecycle, ack (dv9.2–dv9.4)
- `cron/scheduler.py` — failure path upserts incident + ack-gated alerting (dv9.4)

**Files to extend (targeted changes only):**
- `cron/scheduler.py` — add `_maybe_capture_incident()` call in `_run_one_job_body()` after `run_job()` returns and before `mark_job_run()` (dv9.4; extend the #94692 failure path); add post-run artifact check for `data_missing` (dv9.10)
- `cron/jobs.py` — add `last_incident_id`, `last_incident_at` fields to `mark_job_run()` (dv9.4)
- `plugins/platforms/discord/adapter.py` — add `send_cron_review()` and `CronReviewView` (dv9.8)
- `hermes_cli/subcommands/cron.py` + `hermes_cli/cron.py` — extend the incidents CLI landed in #94692 with token redemption (dv9.7b)

**Zero new dependencies.** `hmac`, `hashlib`, `uuid`, `json`, `fcntl` are stdlib. Discord View infrastructure already exists in adapter.

---

## 9. Test Matrix

Tests for the incident store, dedup, lifecycle, and CLI list/ack are proposed in **PR #94692 (open)** (marked `[#94692]`). The rest remain roadmap (dv9.5+).

| Test | Type | What it Proves |
|---|---|---|
| `test_incident_create_read_roundtrip` `[#94692]` | Unit | CronIncident serializes/deserializes; fields preserved; 0600 perms / DB write |
| `test_incident_dedup_absorbs_within_window` `[#94692]` | Unit | Identical normalized error within 1h increments `suppressed_count`, does not create new incident |
| `test_incident_dedup_new_after_window` `[#94692]` | Unit | Same error after dedup_window creates new incident |
| `test_incident_security_block_bypasses_dedup` | Unit | `SECURITY_BLOCK` class always creates new incident |
| `test_normalization_strips_addresses_and_timestamps` `[#94692]` | Unit | `0x7f...` addresses, ISO timestamps stripped; same underlying error → same key |
| `test_redact_applied_before_storage` `[#94692]` | Unit | `raw_error` containing `«redacted:sk-…»` is stored redacted |
| `test_artifact_verification_data_missing` | Integration | Success + delivered but `required_artifacts` absent → `DATA_MISSING` incident created (dv9.10) |
| `test_artifact_verification_all_present_no_incident` | Integration | Success + delivered + all `required_artifacts` exist → no incident (dv9.10) |
| `test_action_token_roundtrip` | Unit | Token created and validated for each action |
| `test_action_token_expired_rejected` | Unit | Token with `exp < now` raises `TokenExpired` |
| `test_action_token_wrong_action_rejected` | Unit | `pause` token cannot authorize `retry` |
| `test_action_token_replay_rejected` | Unit | Nonce stored after first use; second use raises `TokenReplayed` |
| `test_action_token_forged_hmac_rejected` | Unit | Mutated payload raises `TokenInvalid` |
| `test_process_job_failure_creates_incident` `[#94692]` | Integration | Fake job with error → `_process_job()` writes incident to the store |
| `test_process_job_silent_no_incident` `[#94692]` | Integration | Job returning `[SILENT]` response → no incident created |
| `test_process_job_no_agent_incident_stage` `[#94692]` | Integration | `no_agent=True` failure → incident stage is `SCRIPT_ERROR`, not `AGENT_FAILURE` |
| `test_cir_review_env_blocks_recursion` | Integration | Subprocess with `HERMES_CIR_REVIEW=1` → no incident on failure |
| `test_discord_cron_review_view_authorized_click` | Integration | Authorized user clicks button → token redeemed → incident state transitions to `ACKNOWLEDGED` |
| `test_discord_cron_review_view_unauthorized_click` | Integration | Unauthorized user click → interaction rejected, incident unchanged |
| `test_cli_cron_review_token_redeems` | CLI | `hermes cron-review <valid_token>` → prints confirmation, incident ACKed |
| `test_cli_cron_review_expired_token_error` | CLI | Expired token → clear error message, no state change |
| `test_incidents_list_output` `[#94692]` | CLI | `hermes cron incidents list` → tabulated output, open incidents shown |
| `test_incidents_ack_state_gate` `[#94692]` | CLI | Acknowledged signature stops re-pinging; changed error mints a new incident |

---

## 10. Open Questions and Blockers

### Blockers (must resolve before the remaining roadmap starts)

No open blockers. The incident storage layout and token secret path are decided in §4.1 and §4.2. **Note:** the incident store, dedup, ack, and `hermes cron incidents` CLI are already proposed in PR #94692 (open) — the remaining slices build on that, not on the Phase 0 JSON-file layout. The `data_missing` artifact-verification decision is captured as DV-ARTIFACT in §3.1 (scheduled for dv9.10). Snooze-duration UX is intentionally deferred because it does not affect the incident model or storage contract.

### Open Questions (non-blocking for the remaining roadmap)

**Q0: Snooze duration options (must resolve before dv9.7/dv9.8)**
ActionToken for snooze ultimately needs a duration. Proposed fixed options: 1h, 4h, 24h, 7d. Discord button approach: separate buttons or a Select dropdown (following `ModelPickerView` pattern). CLI approach: `hermes cron-review <token> --snooze 4h`. This does not block dv9.2 because the base incident model, dedup engine, and token signing contract do not encode a duration decision.

**Q1: Cross-profile owner review (dv9.6)**
If the failing job belongs to profile `work` but the owner's home channel is in profile `personal`, the review subprocess must launch in `work` context. The allowlist in `config.yaml` must be explicit. Is there a case where the review session should run in a different profile from the failing job? If yes, the `CronReviewRequest` needs a `review_profile` field.

**Q2: `no_agent=True` job review depth**
For script-only jobs, there is no agent summary to review. The owner-agent in dv9.6 would only see the raw (redacted) script stderr. Should dv9.6 skip review entirely for `no_agent=True` incidents, or is a lightweight "read the output and recommend next steps" review still valuable?

**Q3: Delivery failure incidents — alert channel**
If `_deliver_result()` fails, the intended delivery channel is unavailable. Where should the `DELIVERY_FAIL` incident alert go? Options: (a) fallback to a configured secondary channel; (b) log only, no alert; (c) write to `{HERMES_HOME}/cron/incidents/pending_alerts.json` for next successful delivery. No good answer without owner input.

**Q4: Alert content verbosity threshold**
The proposed alert format is compact. For `SECURITY_BLOCK` incidents, should the alert include the blocked prompt substring (redacted), or only the job name and stage? Showing the blocked fragment helps owners identify injection attacks; hiding it reduces noise. Security-by-default preference: hide the fragment, show only the class.

**Q5: Discord embed vs. plain text alert**
`send_cron_review()` uses `discord.Embed` (as `send_exec_approval()` does). Embeds are better UX but platform-specific. For non-Discord platforms, the alert is plain text. Should the CIR alert content formatter have platform-aware variants, or always produce plain text and let the Discord method wrap it in an embed?

**Q6: Artifact verification semantics (dv9.10)**
The `data_missing` case (see §3.1, DV-ARTIFACT) needs decisions: should `required_artifacts` be declared per job in `config.yaml`, or inferred from the run? Which path roots are allowed (job output dir only, or absolute paths)? Is a missing artifact always an incident, or only when a size floor exists? These do not block the landed #94692 store and are scoped to dv9.10.

---

## 11. MVP Recommendation

**Core store/CLI slice already landed as PR #94692 (open):** durable incidents, signature dedup, ack, and `hermes cron incidents` list/ack (dv9.2–dv9.4 + dv9.7a). This is the backbone; the remaining slices below build on it.

**Ship dv9.5 as the next step**, then dv9.7b + dv9.8 (action surface).

These deliver the full token infrastructure and the action surface. The result after dv9.5 + dv9.7b:
- Every cron failure creates a structured, redacted incident on disk (already in #94692)
- Dedup window prevents alert floods (already in #94692)
- ActionTokens exist for all four actions (dv9.5)
- The CLI `hermes cron incidents list/ack` command lets owners inspect and acknowledge incidents immediately (already in #94692); `cron review <token>` adds redemption (dv9.7b)

**dv9.6–dv9.8 add UI and review surface — ship after dv9.5 stabilizes.**

For Discord specifically: `send_cron_review()` + `CronReviewView` is the right call over command-fallback-only. The infrastructure (`discord.ui.View`, `_allowed_user_ids`, `embed+view send pattern`) is already present in `adapter.py` (`send_exec_approval()` at line 7457, `ExecApprovalView` at line 8787). A `CronReviewView` is a 60–80 line addition following `ExecApprovalView` exactly. Shipping button UI in the same PR as the Discord delivery hook (dv9.8) is correct.

**For all other platforms:** The `/cron-review <token>` command (dv9.7b) is the universal fallback and ships before dv9.8. It covers Telegram, Slack, Signal, WhatsApp, and all other configured platforms.

**dv9.9 (intelligence/classification) and dv9.10 (artifact verification / `data_missing`) are optional for the action-surface MVP.** The incident is actionable without an AI summary, and artifact verification can follow as a hardening slice. Ship them last; gate dv9.9 behind `cron.cir.agent_review: true` in `config.yaml`.

**MVP does not require:**
- Cross-profile owner-agent review (dv9.6 is additive)
- Any new dependency
- Any new core model tool
- Any mutation of live gateway or profile config

**Footprint ladder position:** CIR extends existing cron storage (`cron/jobs.py` pattern, now `cron/incidents.py` from #94692) + CLI surface (`hermes_cli/subcommands/cron.py` + `hermes_cli/cron.py`) + Discord adapter (existing View pattern). It does not introduce a new gateway tool, plugin, MCP server, or core model capability. This is the lowest feasible rung for the feature.
