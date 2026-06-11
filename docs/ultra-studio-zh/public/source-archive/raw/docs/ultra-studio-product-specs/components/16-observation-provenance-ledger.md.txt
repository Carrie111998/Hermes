# Observation / Provenance Ledger

Status: spec-only as a unified component — no ledger service exists;
substantial adjacent machinery is implemented (insights DB, persisted tool
results, skill write-origin provenance, Langfuse tracing plugin), and the
asset-side audit/lineage tables are designed in the asset library doc.
Date: 2026-06-11

Sources:

- Docs: `docs/ultra-studio-product-specs/00-index.md` (product shape:
  "artifact/provenance ledgers"; §Top-Level Acceptance: "The agent cannot
  claim completion without an artifact, observation, or ledger record"),
  `02-agent-runtime-contract.md` (§Acceptance), `03-media-asset-contract.md`
  (§QA observed-vs-inferred, §Lineage), `06-delivery-plan.md` (P2 item 8,
  P2 gate "Browser/downloaded artifacts are captured with provenance"),
  `docs/hermes-asset-library-backend-design.md` (`asset_audit_events`,
  `asset_lineage`), `docs/hermes-tokenrouter-credential-flow.md`
  (§可观测性)
- Code (adjacent, verified this session): `agent/insights.py`
  (`InsightsEngine` over a sessions/tool/skill usage DB),
  `tools/tool_result_storage.py` (`maybe_persist_tool_result`),
  `tools/skill_provenance.py` (`set_current_write_origin`,
  `get_current_write_origin`, `is_background_review`),
  `agent/trajectory.py`, `plugins/observability/langfuse/`,
  `gateway/shutdown_forensics.py`

## Purpose & Scope

The ledger is the anti-fabrication backbone: a durable record of what the
agent actually observed and did, so completion claims are checkable. The
top-level product acceptance depends on it: "The agent cannot claim
completion without an event, artifact, or ledger record" (`00-index.md`).

Two record families:

1. Observations — verifiable facts captured at tool boundaries: file
   exists, media downloadable, duration/dimensions, job succeeded
   (`03-media-asset-contract.md` §QA "Observed" list).
2. Provenance — where things came from and who/what acted: artifact
   origins (browser downloads, generation lineage), actor attribution
   (user vs agent vs background process), decision references (approvals,
   TokenRouter decisions).

Scope: the ledger record model, write points, query surface, and retention.
Asset lineage tables stay owned by the Asset Service; this component
defines the cross-cutting record contract and the run-level ledger that
joins them.

## Implementation Status

| Status | Item | Citation |
|---|---|---|
| Implemented (adjacent) | Sessions / tool usage / skill usage analytics DB with queries | `agent/insights.py` (`_get_sessions`, `_get_tool_usage`, `_get_skill_usage`) |
| Implemented (adjacent) | Oversized tool results persisted as durable artifacts with previews | `tools/tool_result_storage.py` (`maybe_persist_tool_result`) |
| Implemented (adjacent) | Write-origin attribution for skill writes (user vs background) | `tools/skill_provenance.py` (`set_current_write_origin`, `is_background_review`) |
| Implemented (adjacent) | Trajectory capture | `agent/trajectory.py` |
| Implemented (adjacent) | External tracing integration | `plugins/observability/langfuse/` |
| Implemented (adjacent) | Gateway shutdown forensics (crash evidence) | `gateway/shutdown_forensics.py` |
| Specified, not built | Unified observation records at tool boundaries (typed observed facts) | `03-media-asset-contract.md` §QA; no observation store exists (rg `observation|provenance|ledger` — doc hits only) |
| Specified, not built | Run-level ledger joining events, artifacts, approvals, jobs | `00-index.md` product shape; `06-delivery-plan.md` P2 item 8 |
| Specified, not built | Browser/downloaded artifact provenance capture | `06-delivery-plan.md` P2 gate |
| Specified (asset-side) | `asset_audit_events` + `asset_lineage` tables | `hermes-asset-library-backend-design.md` §核心实体 — owned by `09-asset-service.md` |
| Specified (control-plane) | TokenRouter per-request decision logging (request/run/tool ids, policy reason, quota delta) | `hermes-tokenrouter-credential-flow.md` §可观测性 — owned by `17-tokenrouter.md`, joined here by ids |
| Gap | Completion-claim enforcement: nothing today programmatically blocks "done" claims lacking records | `00-index.md` acceptance has no enforcement point yet |

## User Entry Points

Not a primary surface; reached through:

- Inspector "QA result and observed evidence" panel for a selected
  job/asset (`01-product-surface.md` §Right) — reads observations.
- Asset detail audit tab (`08-asset-library-ui.md`) — reads asset audit
  events.
- "Why did this fail?" flows — repair planning reads provenance + provider
  evidence (`03-media-asset-contract.md` §Lineage purpose).
- Admin/debug: insights/analytics pages (implemented:
  `web/src/pages/AnalyticsPage.tsx` consumes the insights engine) and
  Langfuse traces where configured.

## Feature List

| Feature | Status |
|---|---|
| Tool/session/skill usage records queryable by time window | Implemented (insights DB) |
| Durable artifacts for large tool outputs | Implemented (`tool_result_storage`) |
| Actor attribution on skill writes | Implemented (`skill_provenance`) |
| Typed observation records (`file_exists`, `media_downloadable`, `dimensions`, `duration`, `thumbnail_present`, `job_succeeded`) | Planned |
| Observation-backed QA verdicts (observed vs inferred separation) | Planned (contract in `03-media-asset-contract.md` §QA) |
| Run ledger: ordered record of events/tools/jobs/approvals per run id | Planned |
| Browser/download artifact provenance (source URL, capture time, session) | Planned |
| Cross-component joins by `request_id` / `run_id` / `tool_call_id` / `job_id` | Planned (id fields already appear in MediaJob envelope and TokenRouter logging specs) |
| Completion-claim gate (agent claims checked against ledger) | Planned |
| Retention and export policy | Planned |

## State Machine

Ledger records are append-only facts; they do not transition. The
meaningful lifecycle is the QA verdict that consumes them:

```text
job/asset event
  -> observations captured (system, at tool boundary)
  -> verdict: observed_ok | observed_failed   (facts only)
  -> inferred quality layered separately      (model/user judgment)
```

Rules:

- Records are immutable once written; corrections append, never rewrite.
- An inferred judgment (prompt alignment, style fit) can never be stored as
  an observation — the two lists in `03-media-asset-contract.md` §QA are
  disjoint by construction.
- Absence of observation is reported as "unverified", not as failure or
  success.

## APIs & Events

Implemented (adjacent): insights queries (`InsightsEngine.generate(days,
source)`), tool-result persistence on the tool channel, Langfuse export.

Planned:

```http
GET /api/ledger/runs/{run_id}                 # ordered run ledger
GET /api/ledger/observations?job_id=&asset_id=
GET /api/assets/{asset_id}/audit              # asset-side (Asset Service)
```

Write points (internal, planned):

- Tool executor boundary: after each media-relevant tool completes, capture
  typed observations (sizes, durations, URLs verified fetchable).
- Job finalize: observations for outputs (exists, dimensions, thumbnail).
- Approval resolution: provenance record linking decision to payload hash
  (`15-human-approval-gateway.md`).
- Browser/download tools: source URL + capture context.

No new gateway event class: ledger writes are silent; surfaces read them on
demand.

## Data Model

Planned core records:

```text
observations
- observation_id
- run_id, session_id, tool_call_id, job_id?, asset_id?
- kind: file_exists | media_downloadable | dimensions | duration
        | thumbnail_present | job_succeeded | http_status | custom
- value_json            (measured value, e.g. {w:1280,h:720})
- evidence_ref          (artifact key / URL checked / command output ref)
- observed_at

provenance_records
- record_id
- subject: artifact | asset | message | decision
- subject_id
- origin: user_upload | generation | browser_download | tool_write
          | external_import
- actor: user | agent | background        (cf. skill_provenance origins)
- source_detail (url, provider job id, parent ids)
- run_id, session_id, created_at
```

Joins: `asset_lineage` and `asset_audit_events` (Asset Service) and
TokenRouter decision logs are linked by shared ids, not duplicated here.

## UI Behavior

- Inspector QA panel renders observations as a checklist with evidence
  links (download check, dimensions, first frame) and clearly separates the
  inferred section ("agent judgment"), matching the observed/inferred split.
- The run ledger view (debug-level) shows the ordered timeline:
  prompt -> route -> tools -> job -> approval -> asset, each row linking to
  its record.
- Unverified states render as "unverified", never as a green check.
- No raw secrets or full provider payloads in ledger UI; evidence refs
  point to sanitized artifacts.

## Permissions & Error Handling

- Ledger reads follow the parent object's scope (a user who can see the
  asset can see its observations/audit).
- Ledger writes are system-internal; failures on high-consequence paths
  fail closed: a finalize that cannot write its observation records must
  not report the job as verified-complete (mirrors TokenRouter's
  audit-write fail-closed rule, `hermes-tokenrouter-credential-flow.md`
  §失败行为).
- Errors: `ledger_unavailable` (degrades claims to "unverified" — visible,
  not silent), `observation_evidence_missing` (record without evidence ref
  rejected at write time).

## Acceptance Criteria

- For any completed media job, the inspector can show at least: output
  exists, downloadable, dimensions/duration, thumbnail — each backed by a
  stored observation with evidence (`03-media-asset-contract.md`
  §Acceptance).
- The agent's completion message for a generation links to a job/asset/
  observation record; a claim with no record is reproducibly flaggable
  (P2 enforcement point).
- A browser-downloaded artifact carries source URL provenance
  (`06-delivery-plan.md` P2 gate).
- Failed jobs have failure observations and remain inspectable.
- Records survive gateway restarts; immutability holds (no update path).
- TokenRouter decision ids on jobs resolve to decision log entries
  (once both exist).

## Non-Goals

- Replacing analytics (insights) or external tracing (Langfuse) — the
  ledger is product evidence, not telemetry; they may share storage but not
  contracts.
- Full conversation archival (transcripts are session data).
- Automated visual quality scoring as "observation" — defects/style are
  inferred judgments by definition.
- Tamper-proof cryptographic audit (append-only DB discipline suffices for
  MVP; signing is future work).

## Open Questions

1. Storage: extend the existing insights DB schema vs a separate ledger
   store — one DB with two contracts risks coupling analytics churn to
   evidence durability.
2. Enforcement mechanism for the completion-claim gate: prompt-level rule,
   gateway check on final messages, or eval-time audit?
3. Observation capture cost: synchronous at tool boundary (adds latency) vs
   async with eventual evidence — which kinds must be synchronous
   (`job_succeeded`)?
4. Retention: do observations expire with sessions, with assets, or never?
   Export format for compliance/debugging?
5. How much of Langfuse's trace model can back the run ledger before a
   native store exists (it is plugin-optional, so the ledger cannot
   require it)?
6. Who writes browser-artifact provenance — the browser tools directly, or
   a download-intake path shared with Files (`06-files-task-file-browser.md`
   Open Question 4)?
