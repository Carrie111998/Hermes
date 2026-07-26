# Research Page UI Guidelines

**Applies to:** `server/webui`

**Primary route:** `/app/research`

**Related routes:** `/app/lead-map`, `/app/leads`, `/app/agent-runs/:runId`, `/admin/data-sources`

## 1. Purpose

The Research page is the control center for discovering and enriching buyer companies. A user must be able to configure, estimate, run, monitor, inspect, save, clone, export, and refresh research campaigns without editing files or model prompts.

The page must expose every behavioral setting defined by the lead-research architecture. Credentials remain in secure integration/admin flows and appear here only as masked availability states.

The implementation must follow the existing vanilla JavaScript module system in `server/webui`. Do not rebuild this page in the unrelated React dashboard under `web/src`.

## 2. Information architecture

Add **Research** to the primary Discover navigation group before Leads. Keep Lead Map as an optional geographic exploration view.

```text
Research
├── Campaigns
│   ├── Draft
│   ├── Running
│   └── Completed / partial / failed
├── New or edit campaign
│   ├── 1. Scope
│   ├── 2. Sources
│   ├── 3. Qualification
│   ├── 4. Enrichment
│   └── 5. Review and run
└── Campaign detail
    ├── Overview
    ├── Funnel
    ├── Leads
    ├── Sources
    ├── Evidence issues
    └── Configuration
```

Use page routes, not one oversized modal:

```text
/app/research
/app/research/new
/app/research/:campaignId
/app/research/:campaignId/edit
```

The existing Lead Map `Configure scan` action should navigate to `/app/research/new?countries=DE,AT` with selected markets prefilled. It must not maintain a second configuration schema.

## 3. Page-level principles

1. **Required first, advanced on demand.** Seller country, target countries, sector/products, buyer types, and sources are immediately visible. Timeouts, rate limits, and per-source overrides use an Advanced disclosure.
2. **Show effective values.** For inherited defaults, display whether the value comes from the tenant, template, or campaign override.
3. **Evidence language, not AI language.** Prefer “verified claims,” “source evidence,” and “estimated range.” Never imply that AI-generated text is authoritative by itself.
4. **Fit and confidence are distinct.** Never combine them into one unexplained badge.
5. **Unknown is a valid state.** Do not render missing private valuation, capacity, or export values as zero.
6. **No fake volume promises.** Pre-run lead volume is an estimate range with its basis. Post-run counts are actual funnel values.
7. **Source access is explicit.** Disabled, credential-required, licensed, degraded, and retired are separate states.
8. **Destructive source actions show impact.** Disable, uninstall, and purge have different copy and consequences.
9. **Partial success is normal.** A campaign with one failed provider may still complete as `partial` and display usable results.
10. **Configuration is reusable.** Users can save tenant defaults and clone successful campaigns.

## 4. Campaign list

The `/app/research` landing page contains:

- page title and concise purpose;
- primary **New research campaign** action;
- secondary **Open Lead Map** action;
- status filters: Draft, Running, Completed, Partial, Failed;
- search by campaign name, country, sector, or product;
- sortable campaign table;
- empty state that explains the minimum required inputs.

Table columns:

| Column | Content |
|---|---|
| Campaign | Name and seller country |
| Targets | Country chips with overflow count |
| Sectors | Up to two names and overflow count |
| Sources | Enabled/attempted source count |
| Funnel | Qualified / eligible / named candidates |
| Status | Draft, estimating, queued, running, partial, completed, failed, cancelled |
| Updated | Relative time with absolute timestamp available |
| Actions | Open; overflow menu for Edit, Clone, Export, Cancel, Archive |

Running rows show bounded progress from completed partitions, not a decorative spinner alone.

## 5. Campaign editor

Use a persistent five-step editor with a compact step indicator. Preserve values when moving between steps. Save creates or patches a server-side draft; browser state is not the source of truth.

### Step 1: Scope

Required fields:

| Field | Control | Rules |
|---|---|---|
| Campaign name | Text input | 3–120 characters |
| Seller countries | Searchable multi-select | Defaults to Türkiye; at least one |
| Target countries | Searchable multi-select plus map shortcut | At least one; show partition impact for large selections |
| Sectors | Hierarchical searchable multi-select | Uses canonical sector IDs |
| HS codes | Token input with validation | Optional narrowing; show HS revision |
| Products | Multi-select from Company Brain | Optional only when sector/HS is present |
| Buyer types | Checkbox group | Importer, distributor, retailer, brand, wholesaler, procurement organization, other configured types |
| Precision profile | Radio cards | High precision default; balanced; exploratory |
| Maximum qualified leads | Numeric per target country | Ceiling, never a promised result |

Supporting actions:

- **Select on map** opens the existing Lead Map and returns selected country codes.
- **Use tenant defaults** restores inherited scope settings after confirmation when overrides exist.
- Sector selection shows mapped HS/NACE/NAICS/CPV coverage in a read-only detail disclosure.

Validation:

- seller and target countries may overlap only with an explicit intra-market acknowledgement;
- unknown sector IDs or classification codes block save;
- selected products must belong to the active tenant;
- a campaign must have at least one of sector, HS code, or product;
- target-country limits are enforced by the server and mirrored in the UI.

### Step 2: Sources

Sources are grouped by capability:

- Government trade and market data;
- Company registries and filings;
- Procurement and buying opportunities;
- Exhibitions and official directories;
- Matchmaking and hosted-buyer data;
- Licensed company/shipment/financial databases;
- Customer uploads.

Each source row/card shows:

```text
source name
publisher and jurisdiction
capability badges
entity level: market / named company / opportunity / event
access: public / credential required / licensed / customer upload / retired
health: active / degraded / retired
last successful refresh
country and sector coverage
expected freshness
campaign selection toggle
```

Do not label an aggregate trade source as a “company database.”

Source interactions:

- Selecting a source adds it to the campaign only; it does not enable it tenant-wide.
- Unavailable sources have a disabled checkbox and one corrective action: **Configure access**, **Ask admin**, or **Upload data**.
- Degraded sources remain selectable with a visible warning and last-valid snapshot age.
- Retired sources are visible in historical configurations but cannot be selected for new runs.
- **Test source** belongs to admin/source management, not the campaign editor.

Per-source Advanced settings:

- date window;
- country/sector override when supported;
- maximum pages/records;
- freshness threshold;
- request timeout;
- concurrency and rate cap within provider-safe bounds;
- use last valid snapshot when live acquisition fails;
- include/exclude event types or opportunity statuses.

The UI must show whether an override changes the estimated volume or runtime.

### Step 3: Qualification

#### Eligibility gates

Expose switches or selectors for:

- require resolved legal identity;
- require official domain;
- require target-country presence;
- require plausible buyer role;
- exclude seller-only companies;
- exclude inactive/dissolved entities;
- sanctions/compliance gate;
- excluded domains, organizations, and registry identifiers;
- minimum evidence recency;
- minimum independent source count.

Unsafe relaxations require an inline warning but not a hidden server-only rule. Non-overridable security/compliance gates appear read-only with their reason.

#### Scoring weights

Display the seven dimensions with numeric inputs or sliders:

| Dimension | Default |
|---|---:|
| Product and sector fit | 25 |
| Buyer and channel fit | 20 |
| Buying intent | 15 |
| Market coverage | 15 |
| Commercial scale and capacity | 10 |
| Relevant trade activity | 10 |
| Contactability | 5 |

Requirements:

- show a live total;
- block continuation unless the total is exactly 100;
- offer **Restore defaults**;
- show the profile name and inheritance origin;
- do not make evidence confidence a score weight;
- provide one-sentence examples of evidence that affects each dimension.

Priority-band controls expose the fit and minimum confidence thresholds for A, B, and C. Validate monotonicity and prevent overlapping bands.

### Step 4: Enrichment

#### Feature selection

Allow feature-family selection:

- identity and company scale;
- market coverage;
- import/export activity;
- commercial/physical capacity;
- revenue and public financial value;
- documented private value range;
- stores, offices, warehouses, and factories;
- buying/procurement intent;
- product, brand, certification, OEM, and private-label fit.

Sector rules mark features as Required, Useful, or Not applicable. Users may add useful features but cannot force a nonsensical field to affect scoring.

#### Local-AI fallback

Configurable fields:

| Field | Behavior |
|---|---|
| Enable local-AI fallback | Off skips web enrichment and retains structured results |
| Model profile | Select from server-provided Hermes profiles; show local/remote label |
| Trigger | Missing required fields, completeness below threshold, or manual only |
| Completeness target | Percentage of applicable priority fields |
| Companies per campaign | Hard maximum |
| Pages per company | Hard maximum |
| Time per company | Hard maximum |
| Model-token budget | Hard maximum or profile default |
| Allowed web-source classes | Official only; official plus credible secondary; custom allowlist |
| Cache reuse | Prefer fresh cache; permit stale-with-warning; live only |
| Research languages | Auto plus explicit additions |

When local execution is unavailable, disable the model profile and explain how to configure it. Never silently substitute a paid remote model.

#### Refresh and storage

Expose:

- no schedule, weekly, monthly, quarterly, or custom supported interval;
- stable-identity and volatile-feature refresh windows;
- reuse public cache when permitted;
- raw snapshot retention;
- web snapshot retention;
- export retention;
- campaign archive behavior.

Retention controls show estimated local disk impact where the server can calculate it. Tenant users can inspect storage policy; only authorized roles can purge evidence.

### Step 5: Review and run

Show a compact, complete summary:

- seller and target countries;
- sectors, HS codes, and products;
- buyer types and exclusions;
- selected source count by capability and access tier;
- eligibility gates;
- scoring profile and modified weights;
- enrichment features and local-AI budget;
- refresh/retention policy;
- expected partitions;
- source coverage limitations;
- pre-run volume range and method when available;
- runtime/cost range when available;
- blocking validation issues and non-blocking warnings.

Primary action: **Start research**.

Secondary actions: **Save draft**, **Save as template**, and **Back**.

The confirmation copy says “up to N qualified leads” and never promises a specific result count.

## 6. Estimate state

When scope or sources change, request a debounced server estimate. Cancel or ignore superseded responses.

Display:

```text
Estimated named candidates: 800–1,400
Estimated eligible companies: 180–340
Estimated qualified leads: 45–90
Basis: current source counts + 6 comparable completed campaigns
Confidence: medium
Unavailable: 2 sources have no count endpoint
```

If evidence is insufficient, show “No defensible lead-volume estimate yet” and list the missing basis. Never replace it with a guessed midpoint.

## 7. Campaign detail and run monitoring

### Overview

Show status, configuration summary, started/finished timestamps, parent/child partitions, and actions appropriate to state:

- Draft: Edit, Start, Delete draft.
- Estimating: Cancel estimate.
- Queued/running: Cancel, Open run details.
- Partial/completed: Refresh, Clone, Export, View leads.
- Failed: Retry failed partitions, Clone, Inspect errors.

### Funnel

Use one ordered funnel with actual counts:

```text
raw records -> named candidates -> resolved organizations
-> eligible companies -> qualified leads -> contactable leads
```

Allow breakdown by country, sector, and source. Percentages must state their denominator.

### Source progress

Each selected provider shows:

- queued/running/succeeded/partial/failed/skipped/circuit-open;
- pages and records processed;
- checkpoint and last activity;
- normalized, duplicate, eligible, and qualified counts;
- retry count and safe error category;
- whether last-valid cached data was used.

A failed provider must not turn the entire page into an error state.

### Leads

Embed or link to the standard Leads table filtered by campaign. Add columns/filters for:

- fit score;
- evidence confidence;
- priority band;
- buyer type;
- market coverage summary;
- relevant trade activity;
- buying-intent status;
- applicable-feature completeness;
- unresolved conflict count;
- top evidence sources.

### Evidence issues

Show review queues for:

- ambiguous identity matches;
- conflicting claims;
- unsupported AI extractions rejected by validation;
- stale-only evidence;
- missing high-priority features;
- license/redistribution limitations.

Each issue links to a lead evidence drawer. The drawer shows the claim, status, value/range, unit/currency, reporting period, confidence, method, sources, retrieval date, and snapshot availability.

## 8. Lead detail changes

Update `/app/leads/:leadId` so the score panel shows:

```text
Fit score: 82 / 100
Evidence confidence: 0.74
Priority: B — promising, enrichment recommended
```

Replace the current single weighted bar list with:

- seven fit dimensions;
- evidence confidence factors: authority, corroboration, freshness, conflict penalty, estimate share;
- eligibility result and rejection reasons;
- completeness by applicable feature family.

The Research & Insights panel should render structured feature groups before narrative summary. Numeric values must show status and period, for example:

```text
Store count: 84 · observed · FY2025
Relevant imports: €12m–€18m · estimated range · 2024
Private company value: unknown
```

Every claim opens its evidence drawer. “Re-research” opens a bounded enrichment dialog with the current profile and budget rather than immediately launching an opaque run.

## 9. Data-source administration

Expand `/admin/data-sources` from a test-only table into a provider catalog.

### Catalog columns

- source and publisher;
- categories/capabilities;
- jurisdictions;
- access tier;
- installed/enabled state;
- health and last check;
- last successful snapshot;
- tenant usage count;
- actions.

### Source detail/edit

Expose:

- immutable source ID and adapter version;
- publisher/homepage and terms links;
- supported record/entity types;
- country, sector, and period coverage;
- authentication state with masked credential status;
- licensed retention and redistribution settings;
- concurrency, timeout, rate, and freshness defaults within safe bounds;
- health history and schema version;
- snapshot inventory and local storage usage;
- enable/disable/install/uninstall/purge controls.

### Destructive semantics

- **Disable:** “Stops future collection. Existing evidence remains active.”
- **Uninstall:** “Removes the adapter. Historical evidence and source metadata remain.”
- **Purge evidence:** “Deletes this tenant’s raw and normalized evidence, then recalculates affected leads and scores.”

Before purge, fetch and display an impact preview:

```text
12 campaigns
438 organizations
1,902 claims
37 leads may lose qualification
2.6 GB local storage
```

Require typing the source name. Do not use a generic browser confirm dialog.

## 10. API/UI state contract

The page should consume explicit resources rather than constructing provider or scoring rules client-side.

Minimum resources:

```text
GET    /api/v1/research-campaigns
POST   /api/v1/research-campaigns
GET    /api/v1/research-campaigns/{campaign_id}
PATCH  /api/v1/research-campaigns/{campaign_id}
POST   /api/v1/research-campaigns/{campaign_id}/estimate
POST   /api/v1/research-campaigns/{campaign_id}/start
POST   /api/v1/research-campaigns/{campaign_id}/cancel
POST   /api/v1/research-campaigns/{campaign_id}/retry
POST   /api/v1/research-campaigns/{campaign_id}/clone
GET    /api/v1/research-campaigns/{campaign_id}/metrics
GET    /api/v1/research-campaigns/{campaign_id}/source-runs
GET    /api/v1/research-campaigns/{campaign_id}/issues

GET    /api/v1/research/configuration
GET    /api/v1/research/sectors
GET    /api/v1/research/scoring-profiles
GET    /api/v1/research/enrichment-profiles
GET    /api/v1/research/model-profiles

GET    /api/v1/data-sources/catalog
GET    /api/v1/data-sources/{source_id}/impact
POST   /api/v1/data-sources/{source_id}/install
POST   /api/v1/data-sources/{source_id}/uninstall
POST   /api/v1/data-sources/{source_id}/purge
```

All responses are tenant-scoped. Server validation is authoritative. Field errors use stable paths such as `scoring.weights.buying_intent` so the UI can focus the relevant control.

Draft mutation uses optimistic concurrency through a version/ETag. On conflict, show the changed server version and offer Reload or Save a copy; never silently overwrite.

## 11. Loading, empty, error, and stale states

### Loading

- Use skeleton structure for the campaign list and detail summary.
- Use field-local progress for estimates and source tests.
- Disable only the action whose request is active.

### Empty

- No campaigns: explain seller, target, sector, and source requirements.
- No enabled sources: link to source administration or ask-admin action.
- No qualified leads: show funnel and rejection reasons, not “research failed.”
- No feature evidence: show unknown with the last research attempt and reason.

### Errors

- Field validation stays beside the field and appears in a summary at the top.
- Provider failures stay in Source progress.
- Authentication/licensing errors use corrective actions without leaking secrets.
- Network failure preserves the local form state and allows retry.

### Stale

Stale evidence remains visible with snapshot age and refresh action. It is not styled as current or silently removed.

## 12. Permissions

The current product roles are `customer` and `admin`; this feature must not invent a third role implicitly.

| Capability | Tenant customer | Platform admin |
|---|---:|---:|
| View campaigns and evidence | Yes | When acting in tenant scope |
| Create/edit/run campaigns | Yes | When acting in tenant scope |
| Save tenant defaults/templates | Yes | When acting in tenant scope |
| Select tenant-enabled sources | Yes | Yes |
| Configure credentials/licenses | No | Yes |
| Change safe provider limits | No | Yes |
| Disable/uninstall source | No | Yes |
| Purge tenant evidence | No | Yes with confirmation |

The API, not presentation, enforces permissions.

## 13. Accessibility and responsive behavior

- Every field has a visible label and programmatic description for hints/errors.
- The stepper uses an ordered list and marks the current step.
- Status is conveyed by text plus color/icon.
- Source toggles and scoring controls are keyboard-operable native controls.
- Focus moves to the first invalid field after validation.
- Modals/drawers trap focus only while open and restore it on close.
- Live run updates use a polite live region; errors do not repeatedly announce.
- Tables preserve headers and provide a stacked summary at narrow widths.
- At mobile widths, the five-step editor becomes a vertical step summary; the primary action remains in document flow rather than fixed over content.
- Do not put essential source or claim details only in tooltips.

## 14. Copy rules

Use:

- “qualified leads,” not “guaranteed leads”;
- “estimated range,” not “expected total,” before a run;
- “evidence confidence,” not “AI confidence”;
- “unknown,” not `0`, when evidence is absent;
- “company-specific trade evidence,” not “export volume” when the record is aggregate;
- “public market capitalization,” “reported valuation,” “estimated company value range,” and “addressable market value” as separate labels;
- “partial,” not “failed,” when usable results exist;
- “source unavailable,” not “no data exists,” when access failed.

## 15. Frontend file boundaries

Create focused modules rather than expanding `lead-map.js` or `leads.js` into larger controllers:

```text
server/webui/js/pages/research.js
server/webui/js/pages/research-editor.js
server/webui/js/pages/research-detail.js
server/webui/js/pages/research-source-picker.js
server/webui/js/pages/research-scoring.js
server/webui/js/pages/research-enrichment.js
server/webui/js/pages/research-evidence.js
server/webui/js/research-state.js
```

Shared rendering primitives belong in `server/webui/js/ui.js` only when at least two product surfaces use them. Research-specific UI stays colocated with research pages.

Extend:

```text
server/webui/js/main.js
server/webui/js/shell.js
server/webui/js/api.js
server/webui/js/adapters.js
server/webui/js/mocks/seed.js
server/webui/js/mocks/handlers.js
server/webui/css/app.css
```

Do not duplicate API payload normalization in page files; keep real/mock shape adaptation in `adapters.js` and API routing in `api.js`.

## 16. Web acceptance criteria

1. A tenant user can create a draft with seller country, targets, sectors, buyer types, sources, gates, weights, enrichment, and retention settings.
2. Refreshing or navigating away and back reloads the server draft without losing configuration.
3. Every setting in the effective campaign JSON has a visible UI control or a visible read-only policy explanation.
4. Source availability, health, access tier, entity level, freshness, and coverage are visible before selection.
5. Score weights cannot save unless they total 100; confidence is displayed separately.
6. Local-AI fallback cannot run without a selected available model profile and explicit budgets.
7. A pre-run estimate states range, basis, confidence, and unavailable sources—or explicitly states that no defensible estimate exists.
8. A partial campaign shows usable leads and provider failures together.
9. Lead details show structured claims with status, period, confidence, and evidence.
10. Dataset disable, uninstall, and purge have distinct behavior and copy; purge includes an impact preview.
11. Country, sector, source, and feature configuration can be changed without adding frontend conditionals for each provider.
12. CSV export includes campaign configuration, lead fit, evidence confidence, claims, and source attribution subject to licensing.
13. Keyboard navigation, focus handling, mobile layout, and status announcements satisfy the accessibility requirements above.
14. Real-backend and mock-mode contract tests cover the same successful, partial, invalid, stale, and destructive flows.
