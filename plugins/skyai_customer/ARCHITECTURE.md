# SkyAI v2 Hermes Architecture Contract

This document is the source of truth for SkyAI v2 customer-facing behavior.

## Fundamental Principle

Hermes reasons. The SkyAI backend and tools provide public facts, structured
evidence, transport adapters, cards, URLs, prices, slots, safety boundaries, and
observability. They must not make customer-visible semantic decisions instead of
Hermes.

Do not reintroduce keyword routers, template routers, hardcoded phrase guards, or
example-driven backend behavior. If a customer-visible answer is wrong, fix the
knowledge, evidence quality, prompt principles, or evaluation set. Do not patch
one phrase at a time with backend logic.

## What The Backend May Do

- Fetch public SkyVision catalog data, product detail, categories, campaign
  pages, public terms, delivery/payment/contact facts, and public slot data.
- Normalize public URLs, prices, images, product IDs, category labels, and card
  payloads.
- Enforce security boundaries: no admin access, no customer/order/payment
  mutation, no internal metrics disclosure, no Muncho brain access, no technical
  implementation disclosure to customers.
- Store append-only telemetry/events only through the dedicated `skyai_ci`
  boundary and consent rules.
- Provide Discord mirror/admin diagnostics as sanitized operational evidence.

## What The Backend Must Not Do

- Decide the customer's intent through keyword matching.
- Decide which emotional/sales path to take through hardcoded rules.
- Return ready-made customer-facing scripts for Hermes to copy.
- Add route/classifier/template gates before Hermes reasons.
- Patch every QA miss by adding a new keyword, phrase ban, or special-case branch.
- Leak tracking/customer intelligence, aggregate sales data, operational secrets,
  model/runtime details, or internal architecture to the customer.

## Current Audit

The current SkyAI v2 implementation is a Hermes/AIAgent runtime with a
`skyai_customer` plugin and a FAB-compatible gateway. That is the right
direction, but the plugin still contains legacy-like smart logic that must be
reduced.

### `plugins/skyai_customer/dev_gateway.py`

Current role: HTTP/FAB gateway, Discord mirror, card extraction, Hermes runner,
and system prompt.

Required direction:

- Keep the gateway, response shape, cards, mirror, and safe runtime wiring.
- Keep a short principle-based system prompt.
- Avoid long prompt sections that list exact phrases, examples, and scenario
  scripts. They make Hermes sound trained by snippets rather than reasoning.

### `plugins/skyai_customer/public_tools.py`

Current role: public tools for catalog search, product detail, slots, campaigns,
support facts, and event append.

Required direction:

- Keep public data retrieval and normalization.
- Keep deterministic safety checks and data sanitization.
- Move catalog search toward evidence-first candidate retrieval. Ranking may be
  used for recall ordering, but it must expose candidates/evidence to Hermes
  rather than deciding the answer.
- Remove or quarantine semantic/taste logic based on token sets such as calm,
  extreme, mature recipient, single recipient, narrow query, and similar
  customer-visible judgment.
- Tool outputs should be fact packs, not sales scripts.

### `plugins/skyai_customer/fixtures/compare_scenarios.json`

Current role: v2 vs v1 comparison prompts.

Required direction:

- Keep as evaluation data, not runtime policy.
- Add the real QA failures as scenarios and scoring expectations.
- Do not turn scenarios into if/then backend logic.

## Refactor Plan

### Gate 1: Contract + Prompt Cleanup

- Record this contract.
- Shorten the system prompt to Hermes-led reasoning principles.
- Convert campaign/support tool outputs from scripts to fact packs.
- Add tests that protect the contract.

### Gate 2: Catalog Tool Evidence-First Refactor

- Split catalog retrieval from customer-visible judgment:
  - `catalog_candidates`: public products matching text/filters/location.
  - `catalog_evidence`: normalized fields, price, category, location, distance,
    rating, offer flags, product detail availability.
  - `cards`: UI payload generated from the final products Hermes references.
- Remove token-based persona/taste penalties from the backend.
- Keep only mechanical filters that reflect explicit user constraints or public
  data structure: price range, exact product detail lookup, distance metadata,
  product/category labels, public URL validity.
- Let Hermes decide relevance, diversity, tone, and tradeoffs from evidence.

### Gate 3: QA Problems Become Evaluation, Not Rules

The 17 QA problems are training/evaluation material. They should become:

- scenario prompts with conversation history;
- expected behavioral principles;
- manual/automated scoring dimensions;
- v2 vs v1 comparison rows;
- no runtime keyword guards.

### Gate 4: Daily Upstream Hermes Update Flow

SkyAI v2 should stay close to upstream `NousResearch/hermes-agent`.

Daily workflow:

1. Fetch upstream:
   `git fetch upstream main`
2. Create/update a dedicated sync branch:
   `git switch -c codex/skyai-v2-upstream-YYYYMMDD` or reuse the active sync
   branch.
3. Rebase or merge upstream main into the SkyAI branch.
4. Run:
   `python3 scripts/skyai_v2_upstream_sync_check.py upstream/main`
5. The report must include:
   - upstream commit;
   - local SkyAI commit;
   - ahead/behind counts;
   - changed files;
   - whether all SkyAI changes remain inside allowed plugin/skill/docs/scripts
     boundaries;
   - tests run and result;
   - any conflict or manual resolution.
6. Run targeted SkyAI tests and v2 comparison matrix.
7. Do not deploy automatically. Deploy remains a separate explicit gate.

## Plugin vs Fork

Preferred model: SkyAI customizations live as a Hermes plugin/profile/gateway
layer, not as a fork of Hermes core.

Why:

- Upstream Hermes can be updated daily without replaying customer logic through
  core conflicts.
- Our business logic remains isolated in `plugins/skyai_customer/`, skills,
  fixtures, docs, and SkyAI deployment scripts.
- The upstream-sync guard can fail fast if we accidentally modify Hermes core.
- Security boundaries are clearer: customer-facing SkyAI does not inherit
  Muncho/internal capabilities.

Fork only if upstream Hermes lacks a necessary stable extension point. If that
happens, the preferred fix is to contribute a generic extension point upstream
or keep the smallest possible local patch with a written reason and test.

## How To Teach Hermes From QA

Use QA issues as high-level feedback:

- Explain the business principle and customer risk.
- Provide the public facts Hermes needs.
- Add the case to the evaluation suite.
- Score the answer on understanding, accuracy, tone, sales usefulness, safety,
  cards/links, and whether it follows the customer context.

Do not teach by adding phrase-level bans, keyword guards, or one-off branches.

## SkyAI Voice Contract v0.1

Voice integration is an edge adapter, not a Hermes core change. The source of
truth for the first PBX/voice design gate is
`docs/skyai-voice-contract-v0.1.md`.

The contract keeps SIP/RTP, STT, TTS, barge-in, silence timeouts, call state,
recording policy, and human transfer behavior inside a future SkyAI Voice
Gateway. The gateway may target the current SkyAI v1 chat backend or the SkyAI
v2 Hermes backend through the same adapter surface, so PBX work remains usable
after v2 promotion.

The DEV gateway may register `/voice/*` HTTP transcript/event routes as the
stable adapter surface. Those routes must remain transport-only: no SIP client,
raw audio processing, STT/TTS provider, deployment action, PBX configuration,
or customer-facing semantic routing belongs in this plugin layer.
