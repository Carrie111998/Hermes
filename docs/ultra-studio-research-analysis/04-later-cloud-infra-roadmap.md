# Later Cloud Infra Roadmap

Status: deferred architecture roadmap  
Date: 2026-06-11

## Rule

Do not implement these before the P0 media loop is real. Keep the seams in P0, but defer the infrastructure.

## P1: Hardening After P0 Works

| Area | Trigger | Work |
|---|---|---|
| TokenRouter service | P0 server env credentials become risky or multi-user | Move Atlas credential access behind service boundary. |
| Durable job worker | Video jobs need refresh-safe background progress | Move blocking provider polling out of chat tool loop. |
| Event replay | Refresh/resume loses status | Add persistent event cursor or DB-backed event replay. |
| Asset ACL | More than one project/user profile uses same server | Add project/user checks around asset APIs. |
| Router enforcement | Prompt-only router still causes wrong tool calls | Add deterministic route tool and evals. |
| Basic policy tests | More workflows or providers are added | Add allow/deny tests for providers, assets, and hidden skills. |

## P2: Cloud / Supercomputer Stage

| Area | Keep from earlier infra docs | Why later |
|---|---|---|
| Keycloak | Identity and enterprise SSO | Not needed for local/single-user P0. |
| OPA | Deterministic policy engine | Useful after multiple services and roles exist. |
| OpenBao | Provider credential vault | P0 can use server env while preserving TokenRouter seam. |
| Temporal | Durable long-running workflows | P0 can use DB job rows first. |
| NATS JetStream | Realtime fanout and replay | Existing websocket/events can carry P0 events. |
| Kata / E2B-style sandbox | Untrusted execution isolation | Atlas tool loop does not require arbitrary code execution first. |
| Browserbase-style contexts | Persistent authenticated browser sessions | Not required for image/video generation. |
| CometAPI | Large media preprocessing and multimodal packaging | Useful for long video analysis, not P0 generation. |
| Kueue / GPU Operator | Multi-tenant GPU scheduling | Atlas external provider handles P0 compute. |
| Rook-Ceph / MinIO object plane | Production object storage | Local files can prove P0 first. |
| Argo CD / GitOps | Multi-service deployment | Adds operational load before product loop exists. |

## Reference Architecture Still Useful

The earlier infra documents are not wasted. They define where to go once P0 demonstrates demand:

- [Ultra Studio infra design](../ultra-studio-infra-design/00-index.md)
- [Open-source architecture selection](../open-source-architecture/00-index.html)
- [TokenRouter credential flow](../hermes-tokenrouter-credential-flow.md)
- [CometAPI media gateway](../hermes-cometapi-media-gateway.md)

Their role is now clear: future platform reference, not P0 task list.

## Promotion Criteria

Promote a later item into active implementation only when one of these happens:

- P0 cannot pass acceptance without it.
- A real security problem is observed, not imagined.
- A second user/project/tenant makes local assumptions unsafe.
- Media job volume makes blocking tool calls unacceptable.
- Assets need durable sharing across sessions or machines.
- Browser/account automation becomes a core workflow.

## Do Not Promote Because

- a reference product has it;
- the architecture looks more complete;
- the diagram feels more impressive;
- we can predict future scale;
- a provider has a more advanced API;
- the UI mockup has a button.

Implementation should follow observed product pressure.

