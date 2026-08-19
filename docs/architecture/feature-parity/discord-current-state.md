# Discord Feature Parity & Alignment — current-state authority

> This is a source-of-truth reconciliation, not a completion claim.

## Snapshot

- Upstream main: `7b25941b0ecd1a2d367edc7b6ef89a0958c10822` at `2026-08-19T22:06:57Z`.
- Main Discord package: six files. `adapter.py` is 475,891 bytes.
- `tools/discord_api/` does not exist on main.
- No canonical capability row has terminal release evidence in this snapshot.

## Root finding

The campaign produced substantial candidate code and a locally green packet, but it did not preserve one executable semantic authority from the approved specification through publication and release. The packet's implementation map silently reassigned W-row meanings: canonical W1 is rejected native webhook administration, W3 is multiplex routing, W4 is proactive/home/cron delivery, and W5 is deferred/rejected OAuth. A packet that calls those IDs something else is a different contract, regardless of test count.

PR #90307 makes this class fail closed by digest-locking `(id, name, product_state)`, separating artifact evidence from delivery, and requiring one publication authority, runtime consumers, and terminal receipts.

## Delivery state at this snapshot

- `candidate_unwired`: **19** — M1, M2, M4, M5, M6, M7, T1, T2, T3, T4, T5, I1, I2, I3, I6, V1, W3, W4, R1
- `candidate_blocked`: **16** — M3, I4, I5, V2, V5, V6, A1, A2, A3, A4, A5, A6, W2, R2, R3, R4
- `gap`: **7** — I7, I8, I9, V3, V4, W1, W5
- `candidate_open`: **0**
- `on_main_unverified`: **0**
- `released`: **0**

## Capability ledger

| ID | Canonical capability | Product state | Delivery state | Authority / decisive gap |
|---|---|---|---|---|
| M1 | Structured inbound message model — full doc alignment | `accepted` | `candidate_unwired` | #86440: Candidate projection is not on main and no accepted ingress consumer is named in the live repository. |
| M2 | Agent-facing edit/delete | `accepted` | `candidate_unwired` | #86449: Request builder is not connected to the model-callable Discord transport on main. |
| M3 | Outbound reaction actions | `accepted` | `candidate_blocked` | #89405: #89405 has the real consumer path and now includes the remove-all builder with attribution preserved. #86419 was closed as superseded after verification; the remaining blocker is the unmerged G-lane/consumer seam. |
| M4 | Rich embeds — typed outbound + ingress projection | `accepted` | `candidate_unwired` | #86324: Typed embed builder/projection is not wired through the production send and ingress paths on main. |
| M5 | Poll read-projection | `accepted` | `candidate_unwired` | #86451: Poll projection has no production ingress consumer on main. |
| M6 | Attachment contract — routing, preflight, bounded reads | `accepted` | `candidate_unwired` | #86499: Attachment routing/preflight/bounded-read candidate has no accepted end-to-end consumer on main. |
| M7 | Streaming delivery correctness | `accepted` | `candidate_unwired` | #86501: Streaming delivery candidate is not connected to the delivered Discord path on main. |
| T1 | Thread lifecycle actions | `accepted` | `candidate_unwired` | #86454: Thread request builders are not connected to the production Discord tool/adapter on main. |
| T2 | Thread session isolation + history | `accepted` | `candidate_unwired` | #86503: Thread context/session candidate is not on the production session path. |
| T3 | Forum starter/tag/lifecycle | `accepted` | `candidate_unwired` | #86458: Forum request builders are not connected to a production consumer. |
| T4 | Forum partial-delivery truth | `accepted` | `candidate_unwired` | #86505: Forum partial-delivery truth is not connected to the delivery ledger/effect path. |
| T5 | Thread permission correctness | `accepted` | `candidate_unwired` | #86541: Thread permission evaluator is not connected to creation/fallback behavior on main. |
| I1 | Command sync + registry parity | `accepted` | `candidate_unwired` | #86550: Registry candidate is not the live command-sync authority. |
| I2 | Guild-scope + installation contexts | `accepted` | `candidate_unwired` | #86475: Scope candidate is not wired into guild/global sync and installation-context normalization. |
| I3 | Options, autocomplete, selected-value fidelity | `accepted` | `candidate_unwired` | #86542: Autocomplete/value-fidelity candidate is not the live option/callback path. |
| I4 | Component authorization seam | `accepted` | `candidate_blocked` | #86543: Depends on the accepted component/view seam from #81388 and must keep authorization ownership separate from unrelated UI work. |
| I5 | Clarify lifecycle + UI + modal | `pair_gap` | `candidate_blocked` | #72742: Only delayed-release cleanup is accepted; modal/free-form behavior is still contract-deferred and the current owner touches the monolith. |
| I6 | Interaction ACK + error discipline | `accepted` | `candidate_unwired` | #86485: ACK discipline candidate is not the production interaction boundary. |
| I7 | Sensitive-system-prompt privacy routing | `conditional` | `gap` | none: No implementation until an explicit sensitive-prompt privacy policy and paired confirmation define routing and fallback. |
| I8 | Deliverable approval | `pair_gap` | `gap` | none: Preserve #74471/#68789; choose one owner only after the approval contract and FILE-LIST gate are explicit. |
| I9 | Modals / context menus / cron buttons | `conditional` | `gap` | none: No modal/context-menu/cron-button implementation until a concrete consumer, callback state, authorization, timeout/restart, and accessibility contract exists. |
| V1 | Native voice-message container | `accepted` | `candidate_unwired` | #86544: Native voice-message container is not the production adapter boundary. |
| V2 | Waveform + duration | `existing` | `candidate_blocked` | #11359: Must retarget #11359 after V1 establishes the native voice-message container contract. |
| V3 | Pinned private receive transport seam | `accepted` | `gap` | none: No terminal evidence. |
| V4 | Hermes voice binding restoration | `accepted` | `gap` | none: No terminal evidence. |
| V5 | Unknown-SSRC encrypted-frame safety | `accepted` | `candidate_blocked` | #77998: #77998 and #75078 require one arbitration/composition owner with both contributors preserved. |
| V6 | STT/TTS reliability train | `pair_gap` | `candidate_blocked` | #78196: Collision owners #78196/#78180 and the missing paired addendum block one authoritative integrated train. |
| A1 | Channel/category CRUD | `pair_gap` | `candidate_blocked` | #86460: Target guild/profile/requester authority unresolved. |
| A2 | Permission overwrites | `pair_gap` | `candidate_blocked` | #86429: Target guild/profile/requester authority unresolved. |
| A3 | Role CRUD + assignment | `pair_gap` | `candidate_blocked` | #86462: Target guild/profile/requester authority unresolved. |
| A4 | Moderation primitives | `pair_gap` | `candidate_blocked` | #86464: Target authority and destructive-action approval unresolved. |
| A5 | Scalar guild settings | `pair_gap` | `candidate_blocked` | #86432: Target guild/profile/requester authority unresolved; candidate must prove the model-callable consumer and execution boundary. |
| A6 | Audit retrieval + scheduled events | `pair_gap` | `candidate_blocked` | #86466: Target authority unresolved; read-only audit retrieval must remain separate. |
| W1 | Discord-native webhook operations | `rejected` | `gap` | none: Rejected absent a concrete Hermes consumer. A speculative tools/discord_api/webhooks.py is forbidden. |
| W2 | Generic Hermes webhook → Discord delivery | `pair_gap` | `candidate_blocked` | #70608: Authenticated route/profile metadata and event-thread delivery acceptance unresolved. |
| W3 | Multiplex routing acceptance matrix | `accepted` | `candidate_unwired` | #86545: Profile routing candidate is not the one inbound/outbound/slash/view/webhook/cron authority. |
| W4 | Proactive/home/cron delivery | `accepted` | `candidate_unwired` | #86487: Proactive/home/cron candidate is not wired through profile-owned adapter selection and delivery receipts. |
| W5 | OAuth2 authorization-code flow | `rejected` | `gap` | none: Paired-deferred/rejected-not-in-contract. No OAuth PR without a new product/security decision and paired acceptance. |
| R1 | Route-aware rate-limit contract | `existing` | `candidate_unwired` | #86468: Transport candidate is not the production REST authority on main. |
| R2 | REST route + pagination conformance | `pair_gap` | `candidate_blocked` | #86437: Paired addendum not accepted. |
| R3 | Recovery + reconnect correctness | `pair_gap` | `candidate_blocked` | #86547: Paired addendum and G3 recovery seam not landed. |
| R4 | Local reliability telemetry | `conditional` | `candidate_blocked` | #86442: Operational consumer and retention contract missing. |

## Cross-cutting gaps

1. **Semantic authority:** row IDs were mutable prose labels rather than immutable contract identities. The W lane proves the failure.
2. **Publication authority:** duplicate and stale PRs remain open; supersession is described but not executable.
3. **Consumer authority:** most current candidates are request builders or isolated modules. Main has no `tools/discord_api/` package, so a candidate file is not a user-visible capability.
4. **Architecture:** `plugins/platforms/discord/adapter.py` remains a 475,891-byte monolith while the extraction train is open. New feature work cannot be considered release-ready when its stable consumer seam is still unsettled.
5. **Product gates:** I5/I7/I8/I9, V6, A1–A6, W1/W2/W5, and R2–R4 have explicit contract decisions or paired-addendum gates that packets cannot bypass.
6. **Release proof:** there is no single integration SHA, exact-head full CI, live sandbox matrix, two independent reviews, merge receipt, and current-main re-verification for any row.

## Immediate topology decisions encoded here

- M3 authority is #89405. #86419 was closed as superseded after current-head verification confirmed remove-all coverage, the real plugin/adapter consumer path, and explicit provenance.
- W1 remains rejected and `tools/discord_api/webhooks.py` remains forbidden absent a concrete consumer.
- W3 remains multiplex profile routing; W4 remains proactive/home/cron delivery; W5 remains paired-deferred/rejected OAuth.
- A1–A6 remain blocked until target guild/profile/requester authority is explicit.
- Packet files and green packet tests remain `artifact_evidence`; they do not advance `delivery_state`.

## Terminal condition

A row advances to `released` only with an exact merged commit, head-bound CI, live receipt when required, and two independent exact-head approvals. The campaign closes only after all 42 rows are released, intentionally rejected/deferred, or explicitly superseded with zero orphan publication and credit edges.
