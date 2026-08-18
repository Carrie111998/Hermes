# Collective Wisdom — Source-Backed Implementation Plan

**Status:** Draft v2 — adds the decisive **Skill Sync / HSP** finding (see §0.5). Grounded in actual source on biz-dev-box.
**Repos read (shallow clones, ~/nous on biz-dev-box):**
- `hermes-agent` @ HEAD (v0.20.0) — Python agent + CLI + gateway + cron.
- `nous-account-service` @ HEAD — Next.js (App Router) + Prisma/Postgres, portal.nousresearch.com. Also the **BFF** for the sync plane (`src/server/sync/*`).
- **`gateway-gateway` @ HEAD** — the **sync plane** (Hono + Prisma/Postgres), home of `src/sync/*`. **This is where Collective Wisdom's registry belongs.**

Every "reuse" claim below cites a real file I read. Every "net-new" item is flagged as not present today.

---

## 0.5. DECISIVE FINDING — Collective Wisdom is effectively "M3" of Skill Sync (HSP)

The spec contrasts CW with "Skill Sync … transports files between environments." That Skill Sync is **real, shipping-staged, and a far closer precedent than the Skills Hub.** It already implements most of CW's hard parts:

**Architecture (proven from source):**
- **HSP/1 = Hermes Sync Protocol v1**, a **frozen wire contract**. A git-like **content-addressed object model**: `blob` / `tree` / `commit` objects + mutable `ref` pointers moved by atomic **compare-and-swap** (`nas/src/server/sync/hsp-types.ts`; `gateway-gateway/src/sync/*`).
- Three tiers by repo:
  - **Agent client** — `hermes-agent/tools/skills_sync_client.py` (builds blob/tree/commit, pushes objects, CAS a ref, 3-way merge on 409) + `tools/skills_sync.py` + `hermes_cli/subcommands/sync.py` (`hermes sync status|pull|push|now|enable|disable|device|propose`).
  - **BFF** — `nas/src/server/sync/{hsp-types,sync-plane-client,sync-mock}.ts`. NAS forwards to the plane in hosted mode, typed mock in dev.
  - **Sync plane** — `gateway-gateway/src/sync/{objectStorage,syncStore,syncService,syncRouter,manifest,orgSkillStore,orgSkillService,hash}.ts`; Prisma models `SyncObject`, `SyncRef`, `SkillProposal`; endpoints under `/v1/sync/**`.

**Milestone roadmap already in the code (proven):**
- **M1 — personal sync** (your skills across your devices). `refs/user/<owner>/HEAD`. Shipping-staged but gated.
- **M2 — org-shared skills**: `hermes sync propose <skill>` → member CAS on `refs/org/<orgId>/HEAD` is **accept-always → 202 { proposal_id }**, creating a `SkillProposal`; an **ADMIN** approves → server fast-forwards org HEAD (409 if base moved). Endpoints: `POST/GET /v1/sync/org/proposals`, `POST /v1/sync/org/proposals/:n/{approve,reject}`. `extractCapabilities()` already scans blobs for `code_execution / network_egress / env_secret_read / subagent_spawn / tool_invoke` (a proto sensitive-content scan, contract §11.7).
- **Collective Wisdom V1 ≈ M3** — it layers *candidate detection, owner-consent, LLM proposal explanation, discovery UI, System Spec, compatibility install, update modes, notifications* on this existing propose→approve→distribute spine.

**Auth model (proven):** the Nous bearer JWT carries claims minted by `nas/src/server/oauth/access-token-issuer.ts`: `tool_gateway_admin` (global portal admin — the current pre-launch Skill Sync gate) and **`org_role`** (stamped ONLY for multi-member orgs; absent ⇒ 403 `org_workflow_unavailable`, which also gates personal orgs out of the org surface). Sync plane resolves identity/org fail-closed from the token; `orgScopeKey(orgId) = "org:<orgId>"` scopes all org objects. Default plane URL: `https://gateway-gateway.nousresearch.com`.

### What this REPLACES in the earlier plan
- **Registry does NOT go in the portal (nas).** It goes in **`gateway-gateway`**, extending `src/sync/orgSkill*` + the HSP object model. nas remains the **BFF + UI + token issuer** (add CW views + any new claims). This is a big correction to Draft v1's §3.1.
- **Immutable versioning is free.** Content-addressed commits + `refs/org/<orgId>/HEAD` history = the spec's "immutable sequential versions" (§8.13) with content hashes (§8.2) already built. Sequential "Version 1/2/3" is a display numbering over commit ancestry.
- **Content hashing, integrity, tenant isolation, CAS, 3-way merge** — all already in HSP (`hash.ts`, composite `(owner,hash)` PK, `orgScopeKey`).
- **Org proposal storage + approve/reject + capability extraction** — already in `SkillProposal` + `orgSkillService.ts`.

### The genuine reconciliation CW forces on the existing model
1. **Consent model differs.** M2 today = *member proposes, **admin** approves* (four-eyes-ish, ADMIN-gated merge to org HEAD). CW §5.2 = ***owner** approves their own skill for sharing*, with admin **moderation** as an *optional* second gate (§8.6 open vs moderated). CW must add an **owner-consent step BEFORE** the object ever leaves the device (spec §5.1 private-until-shared), then map "open publication" to auto-merge and "moderated" to the existing admin approve. **The existing approve/reject stays as the moderation gate.**
2. **CW adds the pre-proposal half M2 lacks:** candidate detection, the private owner-only draft (spec §5.1 — must NOT live in the shared object store until approval), LLM-generated explanation, System Spec metadata, richer sensitive-content scan (M2's `extractCapabilities` is advisory-only and code-side; CW needs private-key/credential hard-blocks §8.5).
3. **CW adds the consumer half M2 lacks:** discovery page, skill detail, `hermes wisdom install`, compatibility preflight, update modes, per-user seen-state, notifications, archive-as-status.
4. **Gate replacement:** `skills_sync_client.py` itself flags the `tool_gateway_admin` gate as "pre-launch containment, not the shipping entitlement — replace with a real `sync:*` scope / tier / cohort flag before shipping." CW is the moment to introduce a real `wisdom:*` entitlement.

### Revised repo ownership
| Layer | Repo | CW action |
|---|---|---|
| Candidate engine, owner review, proposal draft, sys-spec, sensitive scan | hermes-agent | **NET-NEW** on top of `skill_usage`, `curator` aux pattern, `skills_sync_client` |
| Object model, org refs, proposal store, approve/reject, capability scan | gateway-gateway | **EXTEND** `src/sync/orgSkill*` + HSP; add CW fields/endpoints |
| BFF + discovery/detail/review UI + token claims | nous-account-service | **EXTEND** `src/server/sync/*` + new `wisdom` UI + `access-token-issuer.ts` |

**Docs status (RESOLVED — see the proposal doc §0.6):** the prose `hsp-1-contract.md` / `design.md` are **not committed to git anywhere in the org** (`gh api search/code filename:hsp-1-contract.md org:NousResearch` → total 0; they live in Notion/Linear). **However, the authoritative wire contract exists in-repo as an OpenAPI spec** — `gateway-gateway/src/openapi/{paths.ts,schemas.ts}` (2,221 lines) — and there is already a **shipping org-skill portal UI** to model CW's discovery page on (`nas/.../orgs/[orgSlug]/sync/_components/{SyncManagementHome,OrgSkillTable,ProposalInbox,ThreeWayMergeWidget}.tsx`). Use the OpenAPI spec as the contract arbiter for M3-B; still worth obtaining the prose design.md for the proposal state machine rationale, but it is no longer a hard blocker.

---

## 0. Executive summary

The spec describes a feature that is **~60% already built as primitives** in `hermes-agent` and **~0% built in the portal**. The work is less "build from scratch" and more:

1. **Agent side:** add a *candidate-detection engine* and a *wisdom CLI/state layer* on top of the existing skill-telemetry, curator, skills-hub, cron, and portal-auth machinery.
2. **Portal side:** add an entirely new *skill registry* domain (Prisma models + authenticated API routes + discovery/review UI) alongside the existing org/membership/role/entitlement/audit foundation.
3. **Wire the two** over the existing OAuth-PKCE transport.

The single biggest new invention is the **candidate engine** (Section 8.3 of the spec). Almost everything else has a load-bearing precedent to extend rather than invent.

---

## 1. What already exists (reuse, don't rebuild)

### 1.1 Skill telemetry — `tools/skill_usage.py`
Sidecar JSON at `~/.hermes/skills/.usage.json`, keyed by skill name, atomic writes + cross-process file lock. Per-skill record already tracks:
- `use_count` (lifetime), `last_used_at`
- `patch_count`, `patch_generation`, `last_patched_at`, `last_reused_patch_generation`
- `created_by` ∈ {agent, installed, user}, `state` ∈ {active, stale, archived}, `pinned`, `sync`, `archived_at`

Bumped from the real tool paths: `bump_use()` on skill load/reference, `bump_patch()` from `skill_manage` patch/edit. Emits lifecycle facts into the learning graph.

**Maps to spec §8.2 (local skill observation) and the Local candidate record (§11).** This is the telemetry backbone the candidate engine reads.

**Gaps vs spec (must add):**
- `use_count` is **lifetime**, not **windowed**. Spec needs invocations "within the last 30 days" and "distinct days used in a rolling 7-day window" and "once/day for 7 consecutive days" (§8.3 high-usage path). → Need a **rolling event log** (timestamps of invocations), not just a counter.
- `patch_count` counts **all** patches; spec needs **"meaningful refinements"** only — formatting/metadata-only edits must not count (§8.2). → Need a *meaningfulness classifier* on refinement.
- No content-hash tracking today. Spec needs current content hash + decision-bound hashes (§8.2, §8.4). → Add hashing.
- No `pending stability-check timestamp`, no proposal state, no lineage-to-published. → New fields.

### 1.2 Curator — `agent/curator.py` (2019 lines)
Background auxiliary-model reviewer, **inactivity-triggered** (`maybe_run_curator()`, `should_run_now()`), forks an `AIAgent` on the auxiliary client (never touches main prompt cache). Already does: automatic lifecycle transitions, **archive (recoverable, never hard-delete)**, consolidation, pin-bypass, run reports (`_write_run_report`, `_render_report_markdown`), structured-summary parsing.

**Maps to:** the "one-time stability check" concept, archive semantics (§8.14), and the aux-model review pattern. The curator's "only touches agent-created skills" invariant (via `skill_provenance.py`) is directly relevant to *which* skills are candidates.

**Reuse pattern, not the engine itself:** Collective Wisdom candidate detection is **event-driven** (spec §8.3 explicitly says *not* a recurring full scan), so it should hook the same telemetry the curator reads but trigger from skill-update/usage events, not the curator's idle timer.

### 1.3 Skills Hub — `tools/skills_hub.py` (4607 lines) + `hermes_cli/skills_hub.py`
Full registry-client abstraction already shipping: `SkillSource` ABC (`search/fetch/inspect/source_id/trust_level_for`), `GitHubSource`, `WellKnownSkillSource`, `UrlSource`, `HubLockFile` (installed-skill provenance), quarantine dir, audit log, taps, index cache, SSRF-safe HTTP (`_ssrf_safe_http_get`, `_guarded_http_get`), bundle model (`SkillBundle`, `SkillMeta`), path-safety validators.

**Maps to spec §8.11 (authenticated installation), §8.12 (managed installation record), §8.10 (copy/install controls), Managed installation record (§11).** A **new `CollectiveWisdomSource(SkillSource)`** that talks to the portal registry API drops straight into this framework and inherits install/lock/provenance/quarantine for free.

### 1.4 Cron scheduler — `cron/scheduler.py` + `cron/jobs.py`
File-based, `tick()` every 60s from the gateway thread, cross-process lock. Job records carry `schedule`, `prompt`, `skills`, `no_agent`, `deliver`, one-shot + recurring. `deliver` already supports origin/local/telegram targeting.

**Maps to spec §8.15 (scheduled collective checks & notifications) and §8.1 step 9 (create scheduled collective feed + update checker).** `hermes wisdom setup` creates one recurring cron job (`no_agent` script or agent job) that calls the registry feed endpoint. **No new scheduler needed.**

Also relevant: the **one-time stability check** (§8.3 trigger 1, step 4) is a **one-shot cron job** scheduled 7 days out — exactly what `cron/jobs.py` already supports.

### 1.5 Portal auth & entitlement transport — `hermes_cli/auth.py`, `nous_account.py`, `portal_cli.py`
`DEFAULT_NOUS_PORTAL_URL = "https://portal.nousresearch.com"`. OAuth device-code + PKCE + scoped JWT already implemented. `get_nous_portal_account_info()` returns subscription/entitlement/**org** info from JWT claims + account payload. `hermes portal status/login` exist.

**Maps to spec §8.11 auth steps 2–6 (authenticated user, org membership, collective membership, entitlement, registered installation) and §12 (authorization).** The agent already has an authenticated portal channel; Collective Wisdom adds **new authenticated endpoints** under it and **new scopes** (e.g. `wisdom:read`, `wisdom:publish`, `wisdom:install`).

### 1.6 Telegram identity linking
Relay/platform identity linking already built (spec §8.1 step 11, §8.15, decision register #13 explicitly say "reuse existing Telegram identity linking"). Notifications reuse the existing linked identity + the cron `deliver` path.

### 1.7 Portal foundation — `nous-account-service/prisma/schema.prisma`
Already present and directly reusable:
- **`Organisation`**, `OrgConfig` (`privacyMode`), **`OrgModelPolicy`** (`ALLOWLIST`/`denyModels`/`defaultForNew` — this *is* the model-routing policy the spec's proposal-generation must respect, §5.1/§8.4/§12), **`OrgMembership`**, **`OrgRole`** {OWNER, ADMIN, FINANCE_ADMIN, **SECURITY_ADMIN**, MEMBER}, `OrgInvitation`.
- **`OrgAuditEvent`** — generic (actor, eventType, resourceType, resourceId, before/after/metadata). **Reuse verbatim for the Collective Wisdom audit trail** (spec §8 audit, §12 auditability, Audit event §11).
- **`OAuthClient`** (DCR + PKCE, public clients), `SelfHostedAgentClient`, `/api/oauth/{token,device,register,agent-key}` — the agent↔portal auth surface.
- **`Product` / `ProductModelAccess` / `Subscriptions`** — entitlement checks (spec §8.11 step 5).
- API route convention: `src/app/api/**/route.ts` (App Router). Cron endpoints under `src/app/api/cron/*`.

### 1.8 What does NOT exist in the portal (the net-new backend)
`grep -rniE "wisdom|skill"` over `prisma/schema.prisma` and `src` returns **nothing** relevant (only an unrelated name-generator). **The entire skill-registry domain is greenfield in nas:** models, API routes, discovery/detail/review UI, moderation queue, per-user seen-state, notification-event generation.

---

## 2. Component ownership map

| Spec area | Repo | Reuse / Net-new | Anchor file(s) |
|---|---|---|---|
| Local skill observation §8.2 | hermes-agent | **Extend** | `tools/skill_usage.py` |
| Candidate detection §8.3 | hermes-agent | **NET-NEW engine** | new `agent/wisdom/candidate_engine.py` |
| Meaningful-refinement classifier §8.2 | hermes-agent | **NET-NEW** | new, reuse aux client from `curator.py` |
| Stability check §8.3 | hermes-agent | **Reuse** (one-shot cron) | `cron/jobs.py` |
| Proposal generation §8.4 | hermes-agent | **NET-NEW**, reuse LLM/model-policy | `agent/curator.py` aux pattern, `nous_account.py` |
| In-agent review §8.4 | hermes-agent | **NET-NEW** CLI surface | new `hermes_cli/subcommands/wisdom.py` |
| Sensitive-content scan §8.5 | hermes-agent + portal | **NET-NEW** (agent scans, portal stores/enforces) | new |
| Registry API §8.7, §15 | portal | **NET-NEW** | new `src/app/api/wisdom/*` |
| Data model §11 | portal | **NET-NEW** Prisma models | `prisma/schema.prisma` |
| Publication policy §8.6 | portal | **NET-NEW**, reuse OrgRole | new |
| Discovery/detail UI §8.9/8.10 | portal | **NET-NEW** | new `src/app/(app)/wisdom/*` |
| Installation §8.11/8.12 | hermes-agent | **Extend** SkillSource | `tools/skills_hub.py` |
| Versioning/updates §8.13 | both | **NET-NEW** logic on existing install records | `skills_hub.py` lockfile + new registry |
| Notifications §8.15 | hermes-agent | **Reuse** | `cron/*`, telegram relay |
| Seen-state §8.16 | portal | **NET-NEW** | new model + API |
| Archive §8.14 | both | **Reuse semantics** | curator archive + new registry status |
| Auth/authz §12 | both | **Reuse + new scopes** | `auth.py`, `OAuthClient`, `OrgMembership` |
| Audit §12 | portal | **Reuse** | `OrgAuditEvent` |

---

## 3. Data model (net-new)

### 3.1 Portal (Prisma) — new models
Add to `prisma/schema.prisma`, all scoped by `organisationId` for tenant isolation (§12):

- `Collective` — id, organisationId, name, membershipScope, publicationPolicy (enum OPEN|MODERATED), allowedUpdateModes[], defaultUpdateMode, notificationDefaults (Json), sensitiveHardBlockRules (Json), timestamps. *V1 default: one company-wide collective per org (§7).*
- `CollectiveMembership` — userId, collectiveId, role, viewPermission, installPermission, publishPermission, moderationPermission, status. *(May derive from existing `OrgMembership`/`OrgRole` for V1; keep the table for future team-scoped collectives.)*
- `HermesInstallation` — installationId (random opaque, revocable), userId, organisationId, deviceLabel, optional managedDeviceBinding, timestamps, revocationStatus.
- `WisdomProposalDraft` — **owner-only, private, pre-publication** (§5.1). proposalId, ownerId, organisationId, localSkillId, contentHash, generatedTitle, generatedSummary, modelUsed, candidateEvidence (Json), fullContents, systemSpec (Json), sensitiveScanResults (Json), decision, decisionAt, targetCollectiveId, retentionMeta. **Never joined into discovery queries.**
- `SharedSkill` — skillId, collectiveId, organisationId, ownerId, title, description, currentVersion, status (enum PENDING_REVIEW|PUBLISHED|ARCHIVED), createdAt, updatedAt, archivedAt, archiveReason, accessScope.
- `SkillVersion` — **immutable**; skillVersionId, skillId, sequentialVersion (1,2,3…), content, contentHash, changeSummary, systemSpec (Json), usageEvidence (Json) + timeWindow, refinementCount, sensitiveScanResult+disposition, publisherId, publishedAt.
- `ManagedInstallation` — userId, hermesInstallationId, skillId, installedVersion, installedContentHash, localPath, installAt, lastUpdateCheck, effectiveUpdateMode, compatibilityState, installedDependencyState (Json), locallyModified, archivedUpstream.
- `UserSeenState` — userId, skillId, latestVersionSeen (int, **not** a boolean — §8.16), firstSeenAt, lastSeenAt.
- `NotificationEvent` — userId, eventType, skillId, version, channel, createdAt, deliveredAt, openedAt, dedupKey.
- **Audit:** reuse `OrgAuditEvent` (no new model) — write rows for proposal/approval/publication/version/install/dependency/update-check/update/archive.

### 3.2 Agent (local, sidecar) — extend `~/.hermes/skills/.usage.json` + new store
- Extend the usage record with: `content_hash`, rolling `invocation_log` (capped timestamp ring for windowed counts), `meaningful_refinement_count`, `pending_stability_check_at`, `proposal_state` (the §10.1 enum), `last_decision_hash`, `last_decision_at`, `published_lineage_skill_id`, `installed_source` (registry skillId/version/org/collective).
- New `~/.hermes/wisdom/` dir: `config.json` (thresholds — configurable per §7/§8.3), `installation.json` (installation ID), `proposals/` (local proposal cache), reuse the hub lockfile pattern for managed installs.

---

## 4. Staged build plan (validate at each stage)

Ordering favors the highest-novelty, self-contained agent core first (it's testable locally without the portal), then the portal registry, then wiring. Each stage lands with tests run on the dev box.

### Stage A — Local observation + candidate engine *(agent, no portal dependency)*
1. Extend `skill_usage.py`: content hashing, rolling invocation log with windowed queries (`invocations_last_30d`, `distinct_days_last_7d`, `used_every_day_for_7d`), meaningful-refinement counter.
2. Meaningful-refinement classifier: cheap structural diff first (ignore formatting/metadata-only), aux-LLM tiebreak using the curator's aux-client pattern.
3. `agent/wisdom/candidate_engine.py`: implement both qualification paths (refinement + high-usage §8.3), dedup/reproposal rules (§8.3), stability-check scheduling via one-shot cron.
4. Event hooks: on `skill_manage` patch/edit → evaluate + (re)schedule stability check; on invocation threshold crossing → evaluate.
5. CLI: `hermes wisdom setup|status|scan|candidates` (new `hermes_cli/subcommands/wisdom.py`, following the `add_parser(subparsers)` convention).
- **Validation:** unit tests for every threshold boundary + dedup rule (mirrors nas/agent test style, e.g. `tests/agent/test_curator*.py`); acceptance criteria **1–8**.

### Stage B — Proposal + owner review *(agent)*
1. Proposal draft generation with the **user's configured default LLM**, honoring `OrgModelPolicy` routing/retention/residency; record model used; **no silent substitution** (§8.4).
2. Sensitive-content scan: advisory findings + narrow high-confidence hard-blocks (private key / live credential / org hard-block rule) (§8.5).
3. System Specification metadata extraction/validation (§8.8 schema).
4. In-agent review flow: display **complete raw contents verbatim**, exact evidence, sys-spec, scan results, policy; bind approval to content hash; `hermes wisdom review|approve|decline`.
- **Validation:** acceptance criteria **9–15**; hash-binding invalidation test; hard-block-prevents-approval test.

### Stage C — Portal registry backend *(nas)*
1. Prisma models (§3.1) + migration (`pnpm prisma migrate`), tenant-scoped.
2. Authenticated registry API under `src/app/api/wisdom/*`: proposal upload (owner-only), publish (applies OPEN/MODERATED policy), skill/version fetch (authz on every request — §5.6/§12), discovery/list, seen-state, feed, installation register. New OAuth scopes.
3. Moderation queue (§8.6) reusing `OrgRole` (SECURITY_ADMIN/ADMIN).
4. Audit via `OrgAuditEvent`.
- **Validation:** acceptance criteria **16, 19, 22, 39**; tenant-isolation test (cannot fetch another org's IDs — §12); "URL is not a token" authz test (§5.6).

### Stage D — Discovery + installation *(both)*
1. Portal discovery page + skill detail page (§8.9/8.10) with search/filter/sort, New/Updated badges, copy-prompt/copy-CLI/copy-contents controls.
2. `CollectiveWisdomSource(SkillSource)` in the hub framework; `hermes wisdom list|show|install`; preflight compatibility against System Spec; auto-install safe deps; managed install record.
3. Natural-language install prompt handling (paste-to-install, §9).
- **Validation:** acceptance criteria **17, 18, 20, 21, 23–27**; the four compatibility outcomes (§8.11); partial-compat install test.

### Stage E — Versioning, updates, notifications, archive, audit *(both)*
1. Immutable sequential versions on re-approval (§8.13); `hermes wisdom check|update|versions|uninstall`.
2. Three update modes (manual / auto-with-notice / required) incl. local-modification fork preservation (§8.12/8.13).
3. Scheduled collective check (reuse cron) + CLI/agent/Telegram notifications with dedup + deep links (§8.15).
4. Per-user seen-state (§8.16); archive behavior (§8.14).
- **Validation:** acceptance criteria **28–38**; required-update fork-preservation test; notification dedup test.

### Stage F (optional) — polish
Admin experience (§13), rate limits/abuse protection (§15), docs.

---

## 5. Key design decisions to confirm with stakeholders
1. **CollectiveMembership**: derive from `OrgMembership`/`OrgRole` for V1 (one company-wide collective) vs. a standalone table now. *Recommendation: standalone table, populated from org membership, so §19 multi-collective is a data change not a schema rewrite.*
2. **Where the proposal-generation LLM call runs**: agent-side (default LLM already local) vs. portal orchestration. Spec §5.1/§8.4 lean agent-side with portal storing the draft. *Recommendation: agent generates, uploads draft; portal never calls the model.*
3. **Registry storage of skill contents**: Postgres vs. object store (R2 — nas already has R2 snapshots infra). *Recommendation: contents in Postgres for V1 immutability simplicity; R2 later if size demands.*
4. **New OAuth scopes** naming + whether to mint a dedicated wisdom audience.
5. **Meaningful-refinement classifier** threshold/cost — structural-only vs. always aux-LLM.

---

## 6. Evidence boundary
- **Proven from source (read directly):** all `hermes-agent` modules cited in §1.1–1.6 and the `nous-account-service` schema/route structure in §1.7–1.8. File names, function signatures, model fields, and the *absence* of any skill/wisdom model in the portal were verified by reading the files / grepping the tree on biz-dev-box.
- **Not yet read in full (inferred at contract level):** the internal bodies of `skills_hub.py` install path (I read its API surface, not all 4607 lines), the exact JWT claim shape from `nous_account.py` (read signatures + base URLs, not the full payload parser), and the Next.js route handlers' internal authz middleware. These should be read in full before implementing Stages C–D.
- **Unknown / to confirm:** production migration/rollout process for nas (Vercel + Prisma migrate flow exists in README but not exercised), and any org-policy config UI beyond `OrgModelPolicy`.
