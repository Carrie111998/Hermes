# Validation Roadmap

Status: implementation roadmap  
Scope: P0/P1/P2 infrastructure work and evidence required before claiming completion.

## Launch Gate Rule

No cloud capability is considered done until it has:

- a typed API or event contract.
- a persisted record or explicit no-data state.
- a policy decision path.
- an audit/log trace path.
- a failure test from this repo or deployment harness.

## P0: Real Chat Agent With Media Jobs

Goal: the web panel can chat with the real Hermes agent, upload assets, create real Atlas image/video jobs, stream progress, and show final assets.

Tasks:

| Work | Owner layer | Validation |
|---|---|---|
| Web session API for create/resume/prompt/events | Gateway / Session | submit prompt returns `run_id`; refresh replays events |
| Upload-to-asset flow | Data plane | uploaded image creates asset row and preview |
| Workflow router runtime contract | Agent runtime | greeting does not auto-generate; media request selects skill or asks missing field |
| Atlas tool job records | Media workers | image/video job creates `media_job` and output `asset` |
| Streaming event model | Event bus | UI shows thinking/creating/rendering/reviewing without freezing |
| Inspector state | UI consumer | inspector reads current job/asset/QA/download from real events |
| TokenRouter shim | Control plane | provider calls use scoped env/config boundary; no keys in UI/sandbox logs |
| Basic audit records | Security/ops | session, prompt, tool, job, asset events are traceable |

Exit criteria:

- Generate a real image from chat and see the image rendered in the UI.
- Generate a basic real video from chat and see the video asset rendered in the UI.
- Refresh mid-generation and recover state.
- No hardcoded demo job or fake output path remains in the normal flow.

## P1: Cloud Isolation and Durable Jobs

Goal: move from local/dev assumptions to controlled multi-tenant execution.

Tasks:

| Work | Owner layer | Validation |
|---|---|---|
| OPA policy bundle for assets, tools, provider routes | Policy | allow/deny unit tests |
| TokenRouter service backed by vault | Control plane | sandbox cannot read provider key; expired token denied |
| Temporal job orchestration | Execution | kill worker; job resumes or fails recoverably |
| NATS JetStream replay | Execution | reconnect from cursor gets missed events |
| Sandbox runtime POC | Execution | no hostPath, no metadata service, scoped workspace only |
| Workspace volume mounter | Execution/data | cross-tenant mount denied |
| Postgres RLS | Data | cross-tenant SQL access denied in tests |
| Object storage scoped URLs | Data | guessed object key denied |

Exit criteria:

- Tenant A cannot see Tenant B session, job, asset, file, or browser context.
- Worker crash does not lose job truth.
- Provider credentials never enter sandbox env, mounted files, prompt, or logs.

## P2: Supercomputer-Grade Operations

Goal: production-grade GPU/media fabric, browser contexts, CometAPI, marketplace, and operations.

Tasks:

| Work | Owner layer | Validation |
|---|---|---|
| Kueue + GPU Operator scheduling | Execution | tenant quota, queue depth, priority, preemption tests |
| Browser context service | Execution/security | create/attach/revoke/delete with audit |
| CometAPI media gateway | Data | bounded analysis of long video; tenant-safe cache |
| Smart Groups and semantic search | Data | new matching asset appears automatically |
| Character/Element lifecycle | Data | create, reuse, lineage, permission checks |
| Marketplace install boundary | Product/data/security | installed skill/template has scoped permissions |
| Service mesh and egress gateway | Security/ops | sandbox/provider/vault route policies verified |
| Full OTel + audit dashboards | Ops | run_id -> tool_call -> job -> asset trace works |

Exit criteria:

- Multi-user media workloads can queue without corrupting state.
- Browser and CometAPI actions are auditable and revocable.
- Operations can answer: who generated this asset, with what model, from which inputs, and how much did it cost?

## Validation Matrix

| Scenario | Expected result |
|---|---|
| User says "你好" | assistant replies normally; no media job is created |
| User asks "帮我生成猫的图片" | router selects image workflow; Atlas image job is created |
| User asks for video but gives no aspect/subject | router asks one missing-field question or applies documented default |
| User uploads an image and asks for a video | upload asset is role-classified as reference/input; video job uses asset ID |
| Provider returns queue status slowly | UI remains responsive and streams job status |
| Provider fails | structured error; no fake asset; retry policy visible |
| User refreshes page | session resumes and current jobs/assets reappear |
| Tenant A guesses Tenant B asset ID | API and RLS deny |
| Agent tries to print provider key | key is unavailable and output is denied/redacted |
| Sandbox tries direct provider URL | egress denied unless routed through TokenRouter |
| Worker dies mid-job | Temporal retries or marks recoverable failure |
| Event bus loses connection | client reconnects with cursor; projection remains durable |

## Implementation Order

1. Normalize web Gateway/Session event contracts against existing TUI gateway.
2. Add durable `media_job` and `asset` records for current Atlas tool paths.
3. Wire UI to real event stream and asset rendering.
4. Add router runtime contract and no-hardcode tests.
5. Add TokenRouter shim and secret redaction checks.
6. Add OPA policy tests for assets/tools/provider routes.
7. Move long media jobs behind durable workflow orchestration.
8. Add sandbox/volume isolation once local media flow is proven.
9. Add browser context service.
10. Add CometAPI only when large media analysis needs it.

## Failure Injection Tests

P0:

- provider timeout.
- invalid uploaded file.
- refresh during job.
- missing required router fields.
- asset publish failure.

P1:

- expired scoped token.
- cross-tenant asset lookup.
- worker crash.
- event bus disconnect.
- vault unavailable.

P2:

- GPU queue saturation.
- browser context revoke while active.
- CometAPI resolver failure.
- policy bundle rollback.
- object storage partial upload.

## Open Questions

- Which Atlas route is the first production video model: `wan-2.6-flash` or another default?
- Should P0 use SSE only, or WebSocket plus SSE fallback?
- Is Keycloak required for the first private deployment, or can local auth be a short-lived bridge?
- Which object storage target is first: local MinIO, Rook-Ceph, or existing Atlas OSS?
- Which workflows require explicit approval in the first release?
- How much of existing Hermes skill loading should be removed versus hidden behind Ultra-only registry filters?

