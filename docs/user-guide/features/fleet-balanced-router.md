# Fleet-balanced task router

Fleet has two explicit admission surfaces backed by one deterministic policy
and state engine:

- `desktop_parent` selects a qualified native subscription route before a new
  Desktop agent is constructed. The selected route is durably pinned to that
  conversation lineage.
- `task_worker` is the `hermes fleet plan/run` CLI workflow for a separate
  bounded child task.

Fleet adds no model tool, never intercepts an individual LLM call, and never
migrates an active conversation.

## Safety contract

- Fleet is disabled by default under `fleet.enabled` in `config.yaml`.
- Desktop parent admission has a separate default-off gate at
  `fleet.parent_desktop_enabled`. Changing either gate affects new sessions;
  it does not rewrite an existing pin.
- Credential-shaped fleet config is rejected. API keys, unknown auth sources,
  pay-as-you-go/overage, and incomplete qualification all make a lane
  ineligible.
- Stale or missing capacity is ineligible by default. The explicit
  `fleet.rotation_without_fresh_capacity` opt-in permits an otherwise fully
  qualified lane to enter only the deterministic rotation fallback pool.
  Untrusted percentages never affect the 20-point override or reserve
  arithmetic, and selection is audited as
  `ROTATION_WITHOUT_FRESH_CAPACITY`.
- Selection defaults to persisted cyclic rotation across the eligible pool.
  Fresh, high-confidence capacity may override the cyclic lane only at a
  difference of at least 20 percentage points. A stale fallback can rotate
  when explicitly enabled, but can never trigger or win that override.
- `status`, `doctor`, `plan`, and `audit` are read-only. In particular, `plan`
  does not create the fleet database, acquire a lease, reserve capacity, or
  advance rotation.
- `run` selects once for a new task ID, then pins that task to its lane.
  Reusing `--task-id` never migrates the task to another lane.
- Desktop Fleet Auto admits exactly once before parent construction. Every
  turn, resume, and compression continuation resolves the original lineage
  pin. Provider failure is reported; it cannot silently select a replacement.
- The optional `C:/HermesBridge/usage-weekly.json` source is opened read-only.
  It is capacity evidence only, never authentication evidence.

## Parent route truth

Codex (`openai-codex`), Claude Opus 4.8 (`anthropic`), and Grok 4.5
(`xai-oauth`) are native-parent candidates only when their exact
subscription-only, no-paid-fallback, capability, model, effort, and fast-off
gates pass. The Claude route reads the live Claude Code OAuth credential
through the existing Anthropic adapter; it never substitutes
`ANTHROPIC_API_KEY`.

Antigravity is a distinct external parent driver, never a native `AIAgent`.
Hermes binds its `agy` conversation ID to the immutable Hermes lineage,
verifies a newly created conversation on the first turn, continues later turns
through `agy --conversation`, and requires consumer-subscription, Antigravity
Cloud Code, and exact served-model receipts for Gemini 3.1 Pro (High) on every
turn. Raw logs are reduced to secret-free evidence receipts. Fleet never passes
`GOOGLE_API_KEY`, `GEMINI_API_KEY`, or any other API-key environment variable
to this route. Kimi remains disabled for both surfaces.

Desktop settings show task-worker and parent matrices separately. A fresh
Fleet Auto draft displays selection as pending; after `session.create`, the
backend-provided lane, adapter kind, provider, and model label are
authoritative. Manual pre-session model selection bypasses Fleet Auto.

## Commands

```text
hermes fleet status [--json]
hermes fleet doctor [--lane LANE] [--json]
hermes fleet plan --task-file PATH [--cwd PATH] [--json]
hermes fleet run --task-file PATH [--cwd PATH] [--task-id UUID] [--json]
hermes fleet audit [--task-id UUID] [--reason CODE] [--jsonl]
hermes fleet release TASK_ID [--outcome completed|failed|cancelled] [--json]
```

On native Windows, Antigravity qualification also checks
`%LOCALAPPDATA%/agy/bin/agy.exe` when `agy` is absent from `PATH`; the resolved
file is used for both qualification and execution.

JSON output includes reason codes and, when available, the lane, adapter kind,
model/effort, bridge SHA-256 identity, capture/read/expiry timestamps,
freshness, confidence, and effective remaining capacity.

## Qualification limitation

The bundled CLI does not infer subscription qualification from the mere
presence of a credential or executable. Unless current, attributable auth,
billing, capability, and fast-off evidence has been supplied by a reviewed
integration, `doctor` and `plan` report the failed gates and `run` starts no
child process. Parent admission fails closed without constructing a session.
