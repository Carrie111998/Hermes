# HSP/1 → M3: Collective Wisdom

**A design proposal for the Skill Sync / HSP team**

**Author:** (drafted by Hermes agent for Herve)
**Status:** Proposal for review — no code written yet.
**Scope:** How the "Collective Wisdom" V1 product spec maps onto the existing HSP sync plane, what it reuses, and the specific contract/schema/endpoint additions it needs. This is deliberately framed as **M3 layered on M2**, not a parallel system.
**Grounding:** All "today" claims cite files read at HEAD on biz-dev-box (`~/nous/{hermes-agent,gateway-gateway,nous-account-service}`), Aug 2026. See the Evidence Boundary (§10).

---

## 0.6. Where the contract actually lives (docs hunt result)

The code comments cite `hsp-1-contract.md` and a sync `design.md` as the arbiter. **Those prose docs are NOT in git** — `gh api "search/code?q=filename:hsp-1-contract.md+org:NousResearch"` returns **total_count 0** across NousResearch; they live in Notion/Linear/private design space. That's fine, because the **authoritative wire contract is encoded in-repo in two better-than-prose forms:**

1. **OpenAPI spec — `gateway-gateway/src/openapi/paths.ts` + `schemas.ts` (2,221 lines).** This is the contract arbiter for M3-B. It enumerates every `/v1/sync/**` endpoint with exact status codes and guards, e.g.:
   - `POST /v1/sync/objects` — "server recomputes + verifies each hash"; `?scope=org` uploads to org scope.
   - `POST /v1/sync/refs/{name}` — "Atomic compare-and-swap … **accept-always push**."
   - `POST /v1/sync/org/proposals` — **"there is no direct-merge path for any role, so both always create a proposal"**; `422 author_mismatch — commit author.owner ≠ authenticated user (attribution guard)`.
   - `POST /v1/sync/org/proposals/{n}/approve` — "Approve (ADMIN) — fast-forwards the org HEAD"; `409 HEAD moved since the proposal's base, or already decided`.
   - `403` on all org routes: "No org_id/org_role on the token (personal org), or dev gate."
2. **Migration SQL — `prisma/migrations/20260718130000_add_hsp_sync_plane/migration.sql`** — the concrete table encoding, with contract §-refs inline (§2.1 addressing, §4.4 CAS).
3. **Shipping org-skill portal UI (already built!)** — `nas/.../orgs/[orgSlug]/sync/_components/{SyncManagementHome,OrgSkillTable,ProposalInbox,ThreeWayMergeWidget}.tsx`. `OrgSkillTable` already renders shared skills with **Skill / Shared by / Version / Updated** columns; `ProposalInbox` is the admin approve/reject queue; attribution resolves token user-ids → display names at render (`user-display.ts`) so **names never enter the plane**. **This is the direct precedent for CW's discovery page (§8.9) and moderation queue (§8.6)** — extend it, don't invent it.

**Two authoritative-contract facts that sharpen §3 (consent):**
- The plane's invariant is **"no direct-merge path for any role"** — *every* org-HEAD advance goes through a proposal + explicit approve. My §3.3 "open publication" must therefore be expressed as a **policy-gated auto-approve of the owner's own proposal**, not a bypass of the proposal mechanism. This keeps the audit invariant intact and is a smaller change than I first framed it.
- There is already a **`422 author_mismatch` attribution guard** (commit `author.owner` must equal the authenticated user). CW owner-consent aligns perfectly: the owner *is* the commit author, so the guard already enforces "only the owner can propose their own skill."

---

## 1. TL;DR for the sync team

Collective Wisdom (CW) is the product spec's name for **turning an individual's refined local skills into installable, versioned, org-shared capabilities.** Read against the code, **CW ≈ M3 of HSP**: it sits directly on the M2 org-shared-skills spine (`refs/org/<orgId>/HEAD`, `SkillProposal`, propose→approve→distribute) and adds the two halves M2 doesn't have yet:

- **Pre-proposal (owner side):** automatic *candidate detection*, an **owner-consent gate that fires before any object leaves the device**, an LLM-generated plain-language explanation, a required **System Specification**, and a real **sensitive-content scan** (hard-blocks, not just advisory capability flags).
- **Post-publish (consumer side):** a **discovery/detail UI**, `hermes wisdom install`, **compatibility preflight**, **update modes** (manual / auto-with-notice / required), **per-user seen-state**, notifications, and archive-as-status.

**The one thing we must reconcile:** M2's consent model is *member proposes → **admin** approves*. CW's core principle is *the **owner** approves their own skill*, with admin moderation **optional** (open vs. moderated collectives). The proposal below keeps the existing admin approve/reject **as the moderation gate** and adds owner consent as a new, earlier gate — so M2 semantics survive as the "moderated" configuration.

**Net ask of the sync team:** a small, additive set of HSP contract extensions (a `wisdom` feature flag, a private owner-draft area that is *not* the shared object store, extra `SkillProposal` fields, and a couple of publish/version read endpoints). Nothing about the object model, hashing, CAS, or tenant scoping needs to change.

---

## 2. What M3 reuses unchanged (the good news)

| CW spec requirement | Already provided by HSP today | Source |
|---|---|---|
| Immutable sequential versions (§8.13) | Content-addressed `commit` ancestry on `refs/org/<orgId>/HEAD`; "Version 1/2/3" is display numbering over commit history | `gateway-gateway/prisma` `SyncObject`/`SyncRef`; `hsp-types.ts HspCommit{parents,tree,ts,message,artifact_type}` |
| Content hash + integrity (§8.2, §12) | `sha256:` addresses; composite `(owner,hash)` PK; integrity is intrinsic | `SyncObject`, `src/sync/hash.ts` |
| Tenant isolation (§12) | `orgScopeKey(orgId)="org:<orgId>"`; token-derived org, fail-closed, caller can never name another org | `orgSkillService.ts` |
| Org proposal record + approve/reject (§8.6 moderated path) | `SkillProposal{orgId,n,proposalCommit,baseCommit,proposerUserId,state,capabilities,decidedAt,deciderUserId}` + `POST /v1/sync/org/proposals/:n/{approve,reject}` (ADMIN, fail-closed) | `prisma SkillProposal`, `syncRouter.ts`, `orgSkillService.ts` |
| Server-side merge / conflict handling | approve fast-forwards HEAD when `base == currentHEAD`, else 409; personal path has 3-way merge | `orgSkillService.casOrgHead/approve` |
| Advisory capability signals (proto of §8.5) | `extractCapabilities()` scans blobs: `code_execution / network_egress / env_secret_read / subagent_spawn / tool_invoke` | `orgSkillService.ts:167` |
| Agent transport (build objects, push, CAS, 3-way) | `skills_sync_client.py` (blob/tree/commit, `SyncClient`, org refs) | `hermes-agent/tools/skills_sync_client.py` |
| BFF forwarding + typed mock + org reads | `sync-plane-client.ts` (`listOrgProposals`, `approveOrgProposal`, `getOrgSyncStatus`) + `sync-mock.ts` | `nas/src/server/sync/*` |
| Auth: org role in token | `org_role` claim (multi-member orgs only) + `tool_gateway_admin`; absent org_role ⇒ 403 `org_workflow_unavailable` | `nas/.../access-token-issuer.ts`, `skills_sync_client.py` |
| Local skill telemetry (basis for §8.2/§8.3) | `.usage.json`: `use_count`, `patch_count`, `patch_generation`, `created_by`, `state`, hooks in `skill_manage` | `hermes-agent/tools/skill_usage.py` |
| Recoverable archive; aux-LLM review pattern | curator archive (never hard-delete) + forked aux agent | `hermes-agent/agent/curator.py` |
| Scheduled feed check + one-shot stability timer | cron `tick()`, one-shot + recurring jobs, `deliver` targeting incl. Telegram | `hermes-agent/cron/*` |

**Implication:** M3 is mostly *new orchestration and UI around an existing spine*, plus a genuinely new **candidate engine** on the agent. The distributed-systems-hard parts (addressing, CAS, isolation, merge) are done.

---

## 3. The consent reconciliation (the core design decision)

### 3.1 How M2 works today
Any member's CAS on `refs/org/<orgId>/HEAD` is **accept-always → 202 {proposal_id}** (never merged on the spot, never rejected for authority). An **ADMIN** then `approve`s, which fast-forwards HEAD. `POST /v1/sync/org/proposals` explicitly downgrades an admin caller to MEMBER so this route *always* creates a proposal (explicit-intent share). *(Proven: `orgSkillService.ts`, `syncRouter.ts:386`.)*

### 3.2 What CW requires
- **§5.1 Private until owner shares it.** A candidate must **not** be visible to the collective — *and, we argue, must not enter the shared org object store* — before the owner approves. Today, `sync propose` uploads the commit to `org:<orgId>` scope *first*, then awaits admin. CW inverts the first gate: **owner consents first**, publication second.
- **§5.2 Human (owner) approval is the gate.** Owner approves the *specific content hash* they saw (§8.4 hash-binding).
- **§8.6 Two publication policies.** *Open* = owner approval publishes immediately. *Moderated* = owner approval, then an admin/moderator approves before publish.

### 3.3 Proposed unified flow
```
candidate detected (agent)
      │
      ▼
private owner draft  ── stored owner-only, NOT in org scope ──►  [see §4.2]
      │  (LLM explanation + System Spec + sensitive scan)
      ▼
owner review  ──decline──► recorded, dedup rules apply
      │ approve (bound to contentHash)
      ▼
publication policy?
   ├─ OPEN ......... agent uploads commit to org scope + CAS org HEAD → published
   └─ MODERATED .... agent uploads commit to org scope → SkillProposal (state=submitted)
                     → existing ADMIN approve/reject → CAS org HEAD → published
```

**Key point for the team:** the existing `SkillProposal` + admin `approve/reject` + `casOrgHead` machinery is **exactly** the *moderated* path — we keep it verbatim. *Open* publication is the new path: owner approval itself performs the member-CAS-then-immediate-admin-approve as a single server operation (a new `autoApprove` disposition, gated by the collective's policy, not by caller role). This means **the org HEAD still only advances through an auditable decision** — we're changing *who* is authorized to make it (owner, when policy=open) not *whether* one is required.

### 3.4 Why the private draft must live outside org scope
Uploading to `org:<orgId>` before owner approval would make pre-consent content reachable by any code path that reads org objects (capability scans, admin tooling, future features) — violating §5.1/§12. Options (team's call, §9 Q2):
- **(A) Owner-scope ref** `refs/user/<owner>/wisdom-draft/<id>` reusing the existing personal object store (no new storage, isolation via existing owner scoping). **Recommended.**
- **(B) A dedicated `wisdom_draft` table + object area in the plane** (cleaner separation, more code).
- **(C) Keep the draft entirely on the device + in the BFF** (portal-hosted review reads from the agent) — least plane change, but the spec wants a portal-hosted review page (§8.4) which implies server-side storage.

---

## 4. Proposed contract & schema changes (additive)

All additive; no breaking change to M1/M2 wire shapes.

### 4.1 `HspCapabilities.features` — advertise M3
Add `"wisdom"` (and/or `"wisdom.v1"`) to the `features` array so clients can detect plane support before attempting CW ops. *(`hsp-types.ts HspCapabilities.features: string[]` — already a forward-compat list.)*

### 4.2 Private owner draft (pick one from §3.4; shapes assume A)
- New ref namespace `refs/user/<owner>/wisdom/draft/<draftId>` (owner-scoped, never enumerated by org reads).
- New read endpoints (owner-only authz):
  - `GET /v1/sync/wisdom/drafts` → list caller's drafts
  - `GET /v1/sync/wisdom/drafts/:id` → draft manifest (title, summary, evidence, sysspec, scan results, contentHash)
- Draft carries the **generated explanation, System Spec, sensitive-scan disposition, candidate evidence, modelUsed** as commit message metadata or a sidecar `wisdom.json` blob in the tree.

### 4.3 `SkillProposal` — extend for CW (all nullable/defaulted → M2 rows unaffected)
```
+ publicationPolicy   String   // "open" | "moderated"  (resolved at submit)
+ ownerApprovedAt     DateTime? // owner-consent timestamp (§5.2)
+ ownerContentHash    String?   // hash the owner approved (§8.4 binding)
+ explanationModel    String?   // LLM that generated the summary (§8.4 record model)
+ systemSpec          Json?     // §8.8 required metadata
+ sensitiveScan       Json?     // findings + disposition (§8.5)
+ evidence            Json?     // exact usage/refinement counts + windows (§8.7/§12.analytics)
  state               String    // extend enum: submitted|approved|rejected  → add: owner_pending|published|archived (align to design.md §5.3 once obtained)
```
Note: `capabilities` (already present) becomes one input to `sensitiveScan`.

### 4.4 Publish / version read endpoints (consumer side, §8.9/8.10/8.11)
- `GET /v1/sync/wisdom/skills` → discovery list for caller's collective (title, owner, currentVersion, evidence, New/Updated hints)
- `GET /v1/sync/wisdom/skills/:skillId` → detail (contents, version history from commit ancestry, sysspec, scan disposition, compat summary)
- `GET /v1/sync/wisdom/skills/:skillId/versions/:v` → a specific immutable version (resolve to commit)
- `POST /v1/sync/wisdom/skills/:skillId/seen` → per-user seen-state (§8.16)
- `GET /v1/sync/wisdom/feed` → new/updated/archived since cursor (drives the scheduled checker + notifications, §8.15)
- `POST /v1/sync/wisdom/installations` → register a Hermes installation ID (§8.11 identity; random/opaque/revocable — mirrors existing device id but for install identity)

### 4.5 Seen-state + notification records
CW needs per-user `latestVersionSeen` (an int, **not** a boolean — spec §8.16) and a notification-event/dedup record. These are **user-scoped, not org-shared object state** → cleaner in the **BFF/NAS DB** than in the plane. *(Recommend NAS owns seen-state + notification events; plane owns objects/refs/proposals. §9 Q3.)*

### 4.6 New entitlement (replace the pre-launch gate)
`skills_sync_client.py` documents that the `tool_gateway_admin` gate is "pre-launch containment, not the shipping entitlement." M3 introduces a real **`wisdom` entitlement**: a `wisdom:*` scope or tier claim minted in `access-token-issuer.ts`, checked by the plane in place of the admin claim. This unblocks a beta cohort without handing out portal admin.

---

## 5. Agent-side work (hermes-agent) — the genuinely new part

M2 gives us `hermes sync propose`. CW's owner half is net-new and lives behind a new `hermes wisdom` command group (`hermes_cli/subcommands/wisdom.py`, `add_parser` convention):

1. **Windowed telemetry.** `.usage.json` tracks *lifetime* `use_count`/`patch_count` today. CW's rules need **windows**: invocations in last 30d, distinct days in rolling 7d, "used ≥1/day for 7 consecutive days" (§8.3). → add a bounded **invocation event log** + a **content hash** per skill.
2. **Meaningful-refinement classifier.** `patch_count` counts *all* edits; §8.2 needs *meaningful* refinements only (not formatting/metadata). → structural diff first, aux-LLM tiebreak (reuse `curator.py` aux client).
3. **Candidate engine** (`agent/wisdom/candidate_engine.py`): both qualification paths (refinement + high-usage), dedup/reproposal rules (§8.3), one-time 7-day stability check via a **one-shot cron job** (already supported).
4. **Event hooks:** on `skill_manage` patch/edit → evaluate + (re)schedule stability check; on usage threshold crossing → evaluate. Event-driven, **not** a recurring full scan (spec §8.3 is explicit).
5. **Proposal generation:** explanation via the **user's configured default LLM**, honoring `OrgModelPolicy` routing/retention (record `explanationModel`, no silent substitution — §8.4).
6. **System Spec extraction** (§8.8) + **sensitive-content scan** with hard-blocks (private key / live credential / org hard-block rule — §8.5), superset of `extractCapabilities`.
7. **In-agent owner review:** display **complete raw contents verbatim**, exact evidence, sysspec, scan, policy; **bind approval to content hash**; `hermes wisdom review|approve|decline`. Portal-hosted review is the equivalent surface (§8.4).
8. **Consumer side:** a `CollectiveWisdomSource` (extends the Skills-Hub `SkillSource` ABC for install/lockfile/quarantine reuse) + `hermes wisdom list|show|install|check|update|versions|uninstall`; compatibility preflight against System Spec; auto-install safe deps; managed-install record; update modes.

---

## 6. Milestone slicing (proposed)

- **M3-A (agent-only, no plane change):** windowed telemetry + candidate engine + in-agent owner review, writing a *local* draft. Fully testable offline. → spec acceptance criteria 1–15.
- **M3-B (plane + BFF):** private draft area (§4.2), `SkillProposal` extensions (§4.3), open-vs-moderated publish, `wisdom` entitlement. → criteria 16, 19, 22, 39. **This is the milestone that touches the contract — gate on obtaining `hsp-1-contract.md` / `design.md` §5.3 first.**
- **M3-C (consumer):** discovery/detail read endpoints + UI, install/compat, seen-state, feed/notifications, versions/updates, archive. → criteria 17–18, 20–21, 23–38.

---

## 7. Open questions for the sync team (decision-owners)

1. **Consent inversion (§3.3):** OK to add an owner-consent gate before object upload, and to model *open* publication as a policy-gated `autoApprove` disposition on `casOrgHead` (rather than caller-role-gated)? This is the crux.
2. **Private draft location (§3.4):** owner-scope ref (A, recommended), dedicated draft store (B), or device+BFF only (C)?
3. **Seen-state & notifications (§4.5):** NAS/BFF-owned (recommended) vs. plane-owned?
4. **Version display:** confirm "Version N" = ordinal over `refs/org/<orgId>/HEAD` commit ancestry filtered to a given skill's tree subpath. Does a single org HEAD carry all skills (one ancestry) or do we want per-skill refs `refs/org/<orgId>/skills/<name>/HEAD` for cleaner per-skill history? (Affects §8.13 version numbering + §8.10 history.)
5. **Entitlement shape (§4.6):** `wisdom:*` scope vs. subscription tier vs. per-cohort flag to replace `tool_gateway_admin`.
6. **System Spec schema (§8.8):** owned where? Proposed: a versioned JSON schema in the contract, validated in the plane at publish and in the agent at install.

---

## 8. Risks / watch-items
- **Contract drift:** the code repeatedly names `hsp-1-contract.md` / `design.md` as the arbiter, and those docs are **not** in the clones I read. Extending the wire contract without them risks divergence. **Blocker for M3-B.**
- **Pre-consent leakage:** any path that reads `org:<orgId>` objects must never see a pre-approval draft (§3.4) — the reason for the owner-scope/private-store decision.
- **"Meaningful refinement" false positives:** an over-eager classifier spams owners with proposals; §8.3 dedup/reproposal rules and the 7-day stability check are the dampeners — must be enforced agent-side.
- **Archive is not deletion (§8.14, §12):** existing installs keep working; the plane's archive is a *status + discovery removal*, matching curator's recoverable-archive philosophy — no recall.

---

## 9. Evidence boundary
- **Proven from source (read directly):** HSP object model, `SyncObject`/`SyncRef`/`SkillProposal` schema, `syncRouter.ts` org endpoints, `orgSkillService.ts` (accept-always/approve/reject/extractCapabilities/roleSatisfies/orgScopeKey), `hsp-types.ts` wire shapes, `sync-plane-client.ts` BFF surface, `access-token-issuer.ts` `org_role`/`tool_gateway_admin` claims, `skills_sync_client.py` gate + client, `skill_usage.py` telemetry, `curator.py` review/archive, cron scheduler. File+symbol cited inline.
- **Contract-level / inferred:** the exact §-numbers in `hsp-1-contract.md` and `design.md` (§5.3 state machine, §11 org skills) come from *references in the code comments*, not the documents themselves — I could not locate the docs in the clones. Treat all §-number citations to those two docs as pointers to verify, not verified text.
- **Not yet read in full:** internal bodies of `objectStorage.ts`/`syncStore.ts` (read the service layer above them), the NAS wisdom UI (does not exist yet), and the full `sync-plane-client.ts` conflict-resolution path.
- **Unknown / to confirm:** production rollout of the plane (staging-railway doc exists; not exercised), and whether a `design.md` proposal-state machine already anticipates owner-consent (it may — obtaining it could shrink §3–§4 considerably).
