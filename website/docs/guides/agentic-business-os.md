---
title: Governed Agentic Business OS
---

Charterforge can run a persistent business objective loop after `charterforge setup agentic`
establishes a standing operating charter. The human operator is an advisor by
default. Charterforge escalates only when authority, capital, or evidence is
insufficient.

Setup also requires an initial business mandate: company name, purpose, desired
outcome, measurable success criteria, explicit stop conditions, and a maximum
duration. Charterforge creates the solo-founder company, CEO mandate, capital
contribution, accepted objective, and first wake event as a resumable,
idempotent bootstrap. Incomplete mandates fail before creating business state.
Rerunning setup repairs missing later stages without duplicating capital or
starting a second initial objective.

With `objectives.create` in the standing charter, the CEO may decompose an
active objective into independently scheduled child objectives. A child
inherits the organization and authority scope, cannot outlive its parent,
cannot add systems or remove prohibitions, and receives an explicit allocation
from the parent's budget. Active children count against
`agentic.organization.max_active_objectives`. Parent-child lineage is immutable,
the accepted child receives its own durable wake event, and success criteria
must name currently registered independent verifiers. Creation is compensable
through `objectives.cancel_child`, bound in advance to the exact creation
idempotency key. The live planning context includes a bounded objective
portfolio and relationship graph so the CEO can coordinate workstreams across
cycles.

The initial organization is a solo-founder company with Charterforge as CEO. Hiring
requires backlog and objective evidence, a contractor-versus-FTE determination,
a reporting line, a mandate, a budget, and a least-privilege Charterforge profile.
Capability-gap hiring metrics are derived from organization-bound blocked
objectives and distinct plan attempts in the authority database; planner claims
cannot inflate them. Each evaluation is immutable and content-hashed. Only a
recorded `hire` verdict can create the corresponding employee, and that
engagement is idempotently linked to the sponsoring objective and decision.
Temporary workers receive expiring contractor mandates; durable roles become
FTE agent employees. Provisioning then creates a credential-empty,
least-privilege profile beneath the designated manager in the organization
chart.

When the standing charter includes `organization.hire.evaluate` and
`organization.hire`, the planner can use two exact workforce action contracts.
The first records and independently verifies the evidence-derived decision. A
later event-driven cycle may materialize the employee only when that record
says `hire`; profile metadata, mandate binding, reporting line, and active
employee state are then independently read back. Historical blocked
transitions are used as evidence, so beginning the staffing plan cannot erase
the capability-gap history that justified it.

Each planning cycle also receives a freshly read, credential-free workforce
context. It lists only active employees and includes their profile name,
reporting line, employment class, current mandate version, capabilities,
systems, toolsets, budget, expiry, and escalation contract. The planner may
delegate only to a listed self-or-descendant profile and only within that
mandate. Kanban independently rechecks the hierarchy before creating the task
and reads the board back afterward; hiring a worker therefore makes them
discoverable but does not bypass delegation authority.

Every business-created Kanban task now carries an immutable employee task
grant, not merely an assignee and prose description. The grant binds the
organization, objective, candidate action, manager, employee, exact mandate
version, title/body hashes, capabilities, systems, profile toolsets,
force-loaded skills, task budget, and expiry. Requested scope must be a subset
of the employee mandate; active grant allocations cannot exceed its budget,
and a task cannot outlive the mandate. Each grant binds one-to-one to a generic
Kanban execution-contract ID.

At process launch, the dispatcher supplies that contract ID and the
authoritative business-state path out of band. The employee worker refuses to
start if its profile identity, reporting organization, current mandate,
Kanban task hashes, assignee, tenant, expiry, or binding changed. Task comments
and external content cannot expand the grant. At result handoff, Charterforge checks
the employee and mandate again; expired, offboarded, superseded, ungranted, or
forged task results are quarantined in an advisor intervention and never enter
CEO replanning. Grants and bindings are included in planning posture, authority
integrity checks, recovery snapshots, and audit exports.

Rerunning reviewed agentic setup does not mutate the CEO's existing mandate.
If the standing charter changes, Charterforge appends a superseding mandate version,
updates the CEO profile's exact mandate snapshot, and preserves the prior
version as audit evidence. Both authority expansion and authority reduction use
this path. A new policy baseline revokes every unconsumed execution permit,
unused exact approval, and still-live employee task grant issued under the
previous charter. Revocations are append-only; affected work must be replanned
and reauthorized under the new mandate instead of inheriting stale authority.

## Financial controls

Initial capital is at least $10.00, with no software-defined maximum. Money is
stored in integer minor units. Every paid candidate action must receive an exact
treasury reservation before its execution permit is issued. Provider read-back
evidence is required before incoming or outgoing payments are reconciled.

The CEO planner receives a fresh financial summary from authoritative state on
every cycle: balance, active reservations, actually available base-currency
capital, trailing 30-day ledger expenses, ledger-derived runway, P&L,
balance-sheet totals, tax liability, and bounded lists of open tax and
compliance obligations. It does not receive payment instruments, provider
references, registration numbers, counterparties, evidence blobs, or financial
credentials. Procurement context explicitly orders evaluation as existing
capability, suitable FOSS, internal build, paid purchase, then defer; its hard
budget source is available capital after reservations, not gross balance.

## Business commitments

Customer promises, service levels, vendor obligations, contractual dates,
renewals, and internal delivery commitments live in an authoritative
commitment register rather than conversation history. Each commitment binds
an active objective, source-system reference, counterparty reference, due
date, grace period, financial exposure, currency, and exact fulfillment
verifier into an immutable content hash. Counterparty details remain excluded
from the bounded CEO planning projection.

With `commitments.manage` on the `commitments` system, the CEO receives exact
create, cancel, and fulfill action contracts. A reversible creation carries a
stable cancellation contract keyed to the original idempotency key.
Supersession preserves both records and atomically retires the old promise.
Idempotency-key reuse with different terms fails closed.

A model cannot declare a commitment fulfilled. Fulfillment requires an
existing `pass` verification record belonging to the commitment's objective
and matching its contracted verifier exactly. Database triggers independently
enforce that evidence relationship and prevent terminal-state rollback or
resolution-evidence rewriting.

Every worker cycle scans commitments without model inference. Upcoming,
overdue, and breached phases receive distinct immutable event identities;
breaches receive critical queue priority. An obligation whose objective has
already terminated creates one advisor intervention instead of disappearing.
Open and breached commitments appear in the CEO planning context, Business
dashboard, integrity preflight, recovery snapshots, and audit export.

With `procurement.evaluate` in the charter, Charterforge records the comparison as an
immutable, content-hashed decision using current unreserved treasury and a
source-evidence reference. A paid software, SaaS, or cloud-service action must
then cite a `buy` decision for the same organization, objective, currency, and
exact amount. That decision can bind to only one payment action. A decision to
use existing capability, FOSS, build, or defer cannot authorize a purchase, and
an ungoverned software payment is rejected before any payment rail is called.

The same database maintains an immutable double-entry journal, standard chart of
accounts, receivables, payables, fiscal periods, financial statements, tax
registrations, effective tax rules, and filing obligations. Charterforge fails closed
when no verified jurisdictional tax rule covers a transaction. The software
does not determine the company's legal entity, nexus, or tax elections; those
facts must be supplied during setup or by a qualified advisor.

The CEO runtime can operate the accounting lifecycle without bypassing that
evidence boundary through three exact contracts:
`accounting.open_period` (`accounting.manage_periods`),
`accounting.assess_tax_obligation` (`accounting.assess_tax`), and
`accounting.close_period` (`accounting.close_period`). Filing and settlement
are separate actions:
`accounting.record_tax_filing` (`accounting.file_tax`) requires authority
receipt evidence, while `accounting.record_tax_payment`
(`accounting.record_tax_payment`) binds the obligation to an exact successful
outbound payment intent. A zero balance uses the explicit
`not_required:zero_balance` sentinel rather than inventing a payment. These
contracts are available only
when the `accounting` system and corresponding capabilities appear in the
initial charter. Periods cannot overlap; a tax assessment must reference an
active registration covering the entire period; and closing fails unless the
trial balance is balanced and every active registration has an assessment.
Retries return the same record only when the assessed facts match exactly.
Opened, assessed, filed, paid, and closed transitions create immutable evidence
events and a separate deterministic verifier reads those records back before progress.
Fiscal periods, tax rules, obligations, and their event lineage are included in
the tenant-scoped audit export.

Payment providers are standalone packages exposing the
`charterforge.inbound_payment_rails` and `charterforge.outbound_payment_rails` Python
entry-point contracts. A compatibility `charterforge.payment_rails` contract exists
for providers that implement both. This keeps vendor SDKs and credentials out
of the state machine while allowing real invoice creation, payment read-back,
and constrained payouts.

The reference implementation is maintained as the independent
`charterforge-stripe-payment-rail` package rather than in this repository. Install it
into the same Python environment as Charterforge and provide `STRIPE_SECRET_KEY`
through the deployment secret manager:

```bash
python -m pip install /path/to/charterforge-stripe-payment-rail
charterforge business payment-rails
```

This command is read-only and safe before credentials are configured. It
reports each discovered inbound/outbound rail as `available: true` or
`available: false` with a bounded reason. A listed rail is not evidence that
payments are authorized; provider verification, jurisdictional compliance
evidence, and exact payment controls remain required before execution.

The package uses hosted Stripe Checkout Sessions for inbound payments and
Stripe Connect Transfers for outbound payments. Checkout payment methods remain
Stripe-managed rather than being hardcoded. Both directions preserve Charterforge'
idempotency key and reconcile through an independent provider GET before the
ledger treats a payment as externally observed. The rail is installed but not
admissible until `charterforge business provider-verify` records current evidence for
the exact direction and jurisdiction. Outbound use additionally requires a
registered `stripe_platform_balance` or `ch_`/`py_` source instrument, exact
spend controls, a budget reservation, and a verified `acct_` connected-account
payee.

Outbound instruments are opaque provider identifiers, never PANs, bank account
numbers, private keys, seed phrases, or provider entity secrets. Every payout
must pass an exact objective reservation, per-transaction and rolling 24-hour
limits, merchant-category and payee allowlists, any configured human threshold,
and a current jurisdictional provider assessment. Inbound rails separately
support hosted checkout, MPP, and x402 adapters. Usage meters retain sub-cent
carry so micro-billing does not lose value through premature rounding.

## Regulatory compliance

The compliance engine stores regimes, applicability assessments, obligations,
control mappings, independent control evidence, source-review deadlines, and a
tamper-evident Know-Your-Agent event chain. The initial discovery catalog
includes CAN-SPAM, CASL, GDPR, the EU AI Act, SOX, PCI DSS, PIPEDA, Canada's
RPAA, and FINTRAC MSB obligations.

Catalog inclusion does not mean a regime applies. Applicability depends on
evidenced jurisdictions, activities, data classes, and entity attributes. For
example, SOX is not inferred merely because the company keeps books; issuer
status is a separate applicability fact. An implicated regime with unknown or
expired applicability blocks the action. An applicable regime without mapped,
currently passing control evidence also blocks the action.

The catalog is discovery scaffolding, not legal advice or a complete inventory.
Regime sources and applicability expire and must be periodically reaffirmed by
an authorized advisor or compliance worker.

Each worker cycle also performs a deterministic, model-free deadline scan.
Approaching or overdue tax filings, applicability expiries, control-evidence
expiries, and regime source reviews emit one deduplicated event to the
organization's active root objective. Event payloads contain bounded deadline
metadata, never tax registration numbers or evidence documents. If the
organization has no active objective capable of owning the issue, Charterforge opens
an organization-scoped advisor intervention instead of silently losing the
deadline. Configure the look-ahead window with
`agentic.compliance.deadline_horizon_days`.

## Bootstrap email

The default bootstrap decision is AgentMail's free plan via the existing
`official/email/agentmail` skill. Charterforge monitors plan usage. At 80% utilization
it opens a procurement decision; reaching a limit does not authorize a paid
upgrade. The CEO compares the paid plan, another provider, and a feasible FOSS
or built alternative against available capital.

Email and payment credentials remain secrets. Behavioral limits and provider
selection belong in `config.yaml`.

## Recovery and operator control

Every external action uses an exact permit, idempotency key, execution window,
fresh-state requirement, and resource lease. Unknown action types, stale
observations, active change freezes, and malformed boundary payloads fail
before execution. Per-objective ceilings bound cycles, actions, input/output
tokens, and compute cost. Before a CEO planning call crosses the model-provider
boundary, Charterforge conservatively reserves the full output allowance, an
upper-bound input estimate, one cycle, and
`planner_call_compute_reservation_minor`. Provider failures, invalid model
responses, and process crashes retain that reservation, so billable failures
cannot become free infinite retries. A projected call that would cross any
ceiling is rejected before inference. Reservations are serialized with an
immediate authority-store transaction across every active objective. The
organization-wide compute ceiling and operating treasury are checked in the
same transaction, and committed planner cost reduces spendable cash for
payments and procurement. Concurrent objectives therefore cannot each allocate
the same remaining capital. Resource commitments are restart-durable, visible
on Business status, and included in the audit package.

Each call also receives an immutable compute-reservation identity linked from
the planner inference request. An explicitly configured subscription-included
billing route can append an immutable zero-cost reconciliation and release the
reservation automatically. A paid reconciliation requires provider evidence
and a provider reference; it posts an idempotent `ai_compute` treasury and
double-entry accounting expense before releasing unused reservation capacity.
When auxiliary `auto` routing or fallback makes the billable provider
ambiguous, Charterforge retains the reservation as unreconciled rather than guessing
a price or inventing free usage. If a paid compute expense commits immediately
before its reconciliation lineage, deterministic housekeeping finds the exact
`compute-settlement:<reservation>` ledger idempotency key and appends the
missing reconciliation without charging twice. Transient failures use bounded
exponential backoff; retry exhaustion becomes an advisor intervention instead
of an infinite loop.

An unreconciled reservation older than
`compute_reconciliation_grace_seconds` raises one deduplicated advisor
intervention for that exact reservation. It remains open until authoritative
billing or subscription evidence is attached:

```bash
charterforge business compute-reconcile compute_... \
  --status provider_confirmed \
  --actual-minor 6 \
  --billing-provider openrouter \
  --provider-reference invoice-line-123 \
  --evidence '{"invoice":"INV-123","line":4}'
```

For an explicitly included subscription, use `--status included`,
`--actual-minor 0`, and evidence identifying the subscription entitlement.
Successful reconciliation resolves the intervention and emits a durable wake
event for the affected objective. Leaving the intervention open keeps the
capital reserved.

When setup explicitly requires approval for an action, escalation does not
become broad authority. The advisor may approve only the original candidate
action through `approve_exact_action`. Charterforge creates an immutable artifact
binding the organization, objective scope, candidate-action contract, payload
hash, capability, system, target resource, cost, currency, policy version,
state-evidence hash, advisor evidence, and a maximum one-hour expiry.
Out-of-charter capabilities, prohibited systems, missing compensation,
irreversible-action prohibitions, budget ceilings, and unresolved compliance
cannot be bypassed with this artifact; those require a charter change or
remediation.

The artifact progresses from issued to permit-materialized to consumed only
when the executor consumes that exact permit. It cannot be replayed for another
action or policy version. The runtime resumes the original candidate and plan
without asking the model to reconstruct it. State or objective-scope drift,
expiry, cross-tenant use, parameter changes, or evidence reuse fail closed.
Until execution begins, the advisor can revoke both artifact and linked permit:

```bash
charterforge business approval-list
charterforge business approval-revoke APPROVAL_ID \
  --reason "relevant external state changed"
```

Expired authority terminates the stale candidate action and emits a high
priority replanning event. Approval artifacts and their exact permit linkage
are checked by integrity preflight and included in the Business dashboard,
recovery snapshots, and audit export.

Each executable action is registered as one indivisible authority contract:
action type, required capability, target system, independent verifier, and
payload schema. The planner may select a contract but cannot relabel a payment
as low-risk delegation or substitute a weaker verifier. The runtime validates
the same tuple again before creating a candidate action, including proposals
inserted through non-model paths. If the charter admits no complete registered
contract, worker readiness fails closed without consuming an objective event.

Every production CEO-planner invocation has immutable inference lineage. Charterforge
records the exact system and user messages sent to the auxiliary planner, the
exact raw response, resolved model identifier, token usage, start/finish
timestamps, parse outcome, and SHA-256 hashes. Provider call failures and
malformed responses are recorded even though they produce no plan. A valid
plan version stores the originating inference ID; the state engine rejects an
inference from another objective or one whose response did not parse.
Deterministic test or policy planners may create plans without pretending that
an LLM call occurred.

`charterforge business audit-export` includes the tenant-scoped inference records and
their bound immutable plan versions alongside actions, permits, execution
results, independent verifications, payments, and ledger entries. The package
hash covers the exact prompt/response lineage, so an auditor can reconstruct
why an action was proposed and detect any exported-record modification.
Planning context is credential-free by construction; provider secrets and
payment instruments never enter these inference records.

Plans may decompose a long strategy into many tasks, but the default execution
contract permits only one external effect per cycle. Charterforge verifies that
effect, observes authoritative state again, and creates a new immutable plan
version before another effect. A planner that proposes multiple effects from
one observation is blocked as a contract violation; its task decomposition is
retained for audit, but no action or permit is created. State timestamps and
provider revisions must come from event or read-back evidence and may not be
invented by the planner.

## Closed-loop strategy measurement

Charterforge stores KPI definitions, observations, targets, strategy experiments,
and their evaluations as authoritative business state rather than planner
prose. Observations are append-only, bound to a contracted verifier, deduplicated
by a hashed provider source reference, and protected against evidence-changing
replay. Values use fixed-point integer scaling so comparisons are deterministic.

The CEO can use governed `strategy.manage` actions on the `strategy` system to
register a metric, establish a recurring target, start a time-boxed experiment,
or decide to continue, revise, or stop an evaluated experiment. Experiments
require a falsifiable hypothesis, exact metric, threshold, end time, and spend
ceiling within the objective budget. The model cannot record its own KPI
observation; provider adapters and deterministic read-backs call the observation
contract with independent evidence.

Every worker cycle refreshes daily revenue, expense, available-cash, and tax
liability observations directly from the immutable ledger and treasury state.
Due target reviews compare only sufficiently fresh observations. Reviews after
downtime collapse missed intervals into one evaluation event. Ended experiments
become `supported`, `not_supported`, or `no_evidence` and wake the active root
objective with an explicit continue, revise, or stop decision. With no active
root, the same evidence becomes an organization-scoped advisor intervention.
The Business dashboard exposes bounded KPI and experiment read-backs without
raw provider evidence or credentials.

Charterforge also materializes immutable outcome attributions from these authoritative
records. This is an evidence-linking layer, not a license for the model to infer
causality. It accepts only three deterministic relationships: an executed
action with an independent passing verification, a provider-confirmed payment
already bound to its objective or action, or a controlled experiment evaluation
bound to its contracted observation. Temporal proximity and planner assertions
are not evidence.

Each provider payment intent can contribute at most one financial outcome, even
when the provider sends repeated successful read-backs. Incoming value excludes
recorded sales tax; outbound value is negative. A later reversal, dispute, or
other contradictory provider state appends immutable contradiction evidence and
removes the value from the CEO's effective planning projection without rewriting
the original record. Planning receives only bounded aggregates and evidence
strength labels; party data and raw provider evidence remain outside model
context. Attribution contracts and contradictions are checked by the authority
integrity preflight and included in the audit export.

Analytics, CRM, billing, and product systems can submit measurements through a
governed webhook route. The route binds one organization, metric ID, verifier,
and maximum event age in `config.yaml`; the request cannot select a different
tenant or metric. Use generic HMAC V2 so the signature covers
`X-Webhook-Timestamp` and the exact request body:

```yaml
platforms:
  webhook:
    extra:
      routes:
        activation-metric:
          objective_organization_id: "org_..."
          objective_source_type: metric_observation
          metric_id: "metric_..."
          metric_verifier: "analytics:hmac-route"
          metric_max_event_age_seconds: 3600
          objective_only: true
```

Inject `WEBHOOK_SECRET` through the configured secret environment; do not place
the HMAC secret in `config.yaml`.

The body supplies integer `value_scaled`, `observed_at`, and a stable provider
`source_reference`. Governed metric routes never run scripts or skills and
never create an agent chat turn. Transport authentication is stored in a
separate immutable ingestion receipt, while provider retries with a new
delivery ID reuse the same immutable observation. If a target is already due,
the request immediately performs the deterministic evaluation and wakes the
CEO; it does not wait for another conversational turn.

Governed company email uses the AgentMail HTTP API as a service-gated execution
edge. Setup records only the company inbox ID; `AGENTMAIL_API_KEY` must be
injected by the external secret manager and is never placed in the authority
database. Each send uses the action's stable provider idempotency key. Success
requires a separate AgentMail message GET whose message ID, recipients, and
subject hash match the permitted action. The audit store retains identifiers
and content hashes rather than message bodies.

Commercial email requires a consent basis, identified sender, physical mailing
address, and unsubscribe URL. Organization-scoped suppression records block
future sends deterministically. Transactional and relationship messages are
classified separately, while malformed or unclassified communication fails
before provider access. Free-plan daily and monthly capacity is checked before
each send; reaching the limit blocks execution instead of silently purchasing
an upgrade.

Inbound AgentMail events use the existing webhook platform's Svix signature
validation and replay window. A governed route binds one organization and one
company inbox:

```yaml
platforms:
  webhook:
    extra:
      routes:
        agentmail-business:
          secret: "whsec_..."
          events: ["message.received"]
          objective_source_type: agentmail
          objective_organization_id: "org_..."
          objective_inbox_id: "ceo@agentmail.to"
          objective_only: true
```

The signing secret is credential material and should be injected through the
secret-management layer in production. Governed routes reject scripts, skills,
and ordinary agent dispatch, so the signed email payload cannot become a raw
prompt. Provider event and delivery IDs deduplicate retries. Inbox binding
prevents cross-tenant routing, while normalized metadata and attachment
descriptors enter the event queue as authenticated data.

Governed routes do not rely on the gateway's process-local duplicate cache.
The authority database first binds each provider event identity to immutable
payload and content hashes, then admits objective events idempotently. A crash
before commit therefore remains retryable, a gateway restart does not change
replay behavior, and reusing one provider event ID with altered content is
rejected as contradictory evidence rather than creating a second business
event. Logical receipts and their authentication evidence are included in the
audit package.

Message bodies remain untrusted. Deterministic injection findings withhold the
body and create an advisor intervention; the objective still wakes with
quarantine metadata rather than silently dropping a customer event. An advisor
may ignore it or release it as explicitly reviewed, untrusted data. Release
creates a new deduplicated objective event with provenance and a boundary that
continues to prohibit treating email text as policy or authority.

Verification records cannot contain arbitrary self-attestation JSON. The
authority database requires a canonical evidence envelope with a distinct
observer identity, observation ID and timestamp, source kind and reference,
structured facts, and a validated facts hash. Stale, future-dated, malformed,
or hash-mismatched evidence is rejected or recorded as inconclusive.

Initial setup requires success criteria as verifier contracts rather than
aspirational sentences. For example:

```json
[
  {
    "verifier": "accounting.revenue_at_least",
    "params": {"amount_minor": 1000, "currency": "USD"}
  },
  {"verifier": "accounting.books_balanced", "params": {}}
]
```

Built-in production criteria include verified revenue, balanced books, and
completion of all delegated Kanban work. Before consuming an event, the worker
audits every active objective against its registered objective verifiers.
Missing, malformed, or unavailable criteria make readiness
`configuration_blocked`; Charterforge does not run indefinitely against a goal it
cannot prove. Accounting criteria read the immutable journal and trial balance,
not planner claims or action output.

Immutability is enforced by the database, not by application convention.
Plans, execution results, verification records, objective events, provider
read-backs, and hash-chained business audit events reject updates and deletes.
Candidate-action and permit contract fields are likewise frozen after
creation; only their explicit lifecycle fields may advance. Compensation
contracts remain fixed while resolution status and its linked evidence may
advance.

The bundled authority store is explicitly a single-host SQLite topology. It
supports multiple supervised worker processes through atomic inbox claims and
fenced resource leases. Lease ownership binds the resource, worker, action,
expiry, and monotonically increasing fencing token; a stale worker cannot
release or validate a successor's lease. Configuring `deployment_scope:
multi_host` fails closed rather than silently operating independent databases.
A shared transactional backend must declare multi-host capability before that
topology is admitted.

Payment verification performs a second provider API read-back rather than
re-reading the row written by the executor. Every provider observation is
append-only and records status, amount, currency, provider reference, provider
evidence, and observation time. Kanban verification independently reads the
board database. Missing records are negative evidence; unavailable read-back
is inconclusive and blocks progress.

`reversible` is not accepted as a bare claim in the production charter. Every
reversible effect must carry an exact compensation action, stable payload
scope, capability, and verifier. If execution succeeds but verification fails,
Charterforge creates a durable compensation obligation and wake event. Until that
obligation is independently verified, policy denies unrelated actions for the
objective. Compensation receives its own candidate action, permit, resource
lease, execution result, and evidence record; it is never an unlogged rollback.

On startup Charterforge finds permits consumed without an execution result. If the
exact stored action carries a stable provider idempotency key, recovery invokes
that same adapter and payload under a fresh resource fence; it never asks the
planner to invent a replacement. Providers must collapse the replay into the
original operation. Without that guarantee Charterforge does not replay the effect:
it opens one reconciliation handoff with choices to read provider state,
execute an authorized compensation, or abandon the objective.

Use `charterforge business autonomy paused --reason "..."` as the master kill switch.
This revokes unused permits and active resource leases. `manual` keeps the
dashboard and evidence available without autonomous execution. Resuming with
`charterforge business autonomy autonomous --reason "..."` creates a new authority
generation; it does not restore old permits.

The dashboard and `charterforge business interventions` show exact exception context
and recorded resolution choices. Resolutions require evidence. Use
`charterforge business audit-export --output audit.json` to produce a tenant-scoped,
hash-verifiable package connecting objectives, plans, actions, permits,
executions, independent verification, payments, and immutable ledger entries.

Every runtime branch that stops for missing authority, no admissible action,
failed execution, or insufficient verification evidence creates one
deduplicated advisor handoff. Resolving a non-terminal choice emits a durable
`intervention.resolved` event containing the selected option and advisor
evidence, so the planner can reassess reality without relying on chat history.
Resolution never widens the charter or manufactures a permit: authority
changes must already exist in the governed configuration or exact permit
record. Selecting `abandon` deterministically terminates a blocked objective
without scheduling more work.

For unattended operation without a messaging gateway, run
`charterforge objectives worker` under systemd, s6, Docker, launchd, or another real
process supervisor. `charterforge objectives worker --once` performs one supervised
cycle for health checks and job runners. `charterforge objectives worker-status`
reports persisted PID, process nonce, heartbeat age, last cycle, errors, and
graceful stop state. The gateway-embedded runtime registers through the same
health contract. Multiple workers are safe because durable inbox claims, not a
process-local mutex, arbitrate objective execution.

Initial agentic setup binds the charter to `agentic.runtime_host`: `gateway`,
`standalone`, or the migration-only `either` mode. A cycle must prove that the
current process has a live, organization-scoped worker registration for the
selected host before it can inspect or mutate business state. Starting
`charterforge objectives worker` under a gateway-only charter, calling the tick
function ad hoc, or relying on a stale worker record fails readiness. The
Business dashboard reports the selected host, expected worker roles, matching
worker history, and whether a healthy supervisor is currently observed. Worker
history is included in the tenant-scoped audit package.

The outer worker supervisor is bounded independently from per-objective retry
policy. Consecutive systemic tick failures use exponential backoff and reset
only after a successful cycle. At
`agentic.security.circuit_breaker_failure_threshold`, Charterforge marks the worker
`circuit_open`, creates one `objective_runtime_unhealthy` intervention with the
last error and failure count, and stops the loop. The standalone worker exits
non-zero so systemd, Docker, or another process supervisor can apply its own
restart policy; the gateway worker remains stopped until the gateway or
operator restarts it after remediation.

Standalone shutdown is supervisor-safe even during exponential backoff:
`SIGINT` and `SIGTERM` wake the wait immediately instead of waiting through the
remaining delay. The worker closes its registration with `stopped_at` and an
exact `stop_reason` such as `signal:SIGTERM`, so audit and status surfaces can
distinguish an orderly supervisor stop from a stale crash record.

Worker liveness and event ownership continue to renew while a planner,
provider, or verifier call blocks. The worker heartbeat does not alter cycle
success or failure accounting. A claimed event uses a separate file-backed
lease keeper governed by `agentic.event_claim_ttl_seconds`; only its exact
runtime owner may renew it. Losing that ownership fails closed before another
external effect or verification can begin. This prevents a long-running call
from appearing dead or allowing another worker to reclaim the same event and
produce a competing plan. Expired claims remain recoverable after an actual
process crash. The exact resource fence is renewed independently across the
provider call as well; master pause, lease takeover, or fence-token mismatch
stops renewal and the stale executor cannot revive its authority.

Permit consumption is also the durable crash boundary. If a process stops
after consuming a permit but before recording the provider result, the next
worker does not invoke the planner. It reconstructs the exact immutable action,
permit, payload, and idempotency key, reacquires the resource fence, and asks
the same adapter to reconcile that operation. Missing or short idempotency keys
block automatically and raise an `execution_in_doubt` advisor handoff instead
of risking a duplicate effect. If the result was already committed, recovery
resumes at budget settlement and independent verification without calling the
provider again. If verification was already committed, recovery advances the
objective without rerunning either executor or verifier. Actual cost and
reservation-preservation semantics are stored with the immutable result, and
reconciled executions receive KYA and audit-chain evidence.

Objectives can wake from durable schedules and typed adapter events without a
chat turn. `charterforge objectives subscribe` binds an organization-scoped source
and event type to an objective. Adapters call `charterforge objectives ingest-event`
or the equivalent Python contract with a provider reference, structured
payload, and adapter authentication evidence; deduplication is per objective
and immutable provider event identity. A retry with identical hashes is safe;
the same identity with different payload or content hashes fails closed.
External message text crosses the quarantine boundary before an inbox record
is created.

Inbox arbitration is deterministic rather than FIFO or model-selected. The
control plane derives an immutable priority class, numeric priority, and
optional deadline from the typed event and bounded payload facts. Overdue tax
or compliance work and compensation failures preempt routine CEO reviews;
off-track metrics, failed experiments, intervention resolutions, and worker
failures receive elevated classes. A producer-supplied `priority` field has no
effect, and reuse of a deduplication key with different event semantics fails.

Within a tenant, claims order overdue deadlines first and then use priority
with bounded hourly aging. Aging eventually raises old routine work to the
front, preventing a continuous stream of newer events from starving it.
Per-objective processing remains serialized, stale claims remain recoverable,
future events remain unavailable, and the organization predicate is part of
the atomic claim query. The Business dashboard reports high-priority, overdue,
and oldest pending event posture.

The runtime includes the active CEO organization in the same atomic SQL
operation that claims an inbox event. Events belonging to another organization
remain pending and unattempted; tenant mismatch is not handled by consuming and
discarding the event after the fact.

`charterforge objectives schedule` creates an interval trigger. After downtime,
Charterforge emits one catch-up event containing the number of missed intervals
instead of creating a planner storm. Terminal objectives are never woken.
`charterforge objectives trigger-status <id> disabled` suspends a schedule or
subscription without deleting its audit history.

Initial agentic setup creates an idempotent CEO operating-review schedule, so a
solo founder continues to inspect portfolio state, runway, results, market
signals, compliance risk, and workforce capacity without the operator manually
creating a cron job. The default interval is 24 hours and can be changed or
disabled through `agentic.operating_cadence`. Restart catch-up remains bounded
to one event regardless of the number of missed intervals.

When the final active root objective satisfies its success criteria, Charterforge
does not interpret that milestone as the end of the company. Closure pauses
until the CEO creates a governed peer successor with
`objectives.create_successor`. The successor receives immutable `succeeds`
lineage, structured completion verifiers, inherited system scope and
prohibitions, a non-expanding budget, and the transferred CEO review cadence.
The predecessor may then become verified. If the standing charter does not
include `objectives.create` on the `objectives` system, Charterforge blocks the final
root and opens an advisor intervention rather than inventing authority or
silently stopping operations. Disabling `agentic.operating_cadence` is the
explicit opt-out from this continuity rule.

Every worker tick begins with model-free lifecycle maintenance. Dormant
objectives expire at their deadline even when no event arrives. Their unused
permits are revoked, candidate actions become expired, capital reservations are
released, and schedules/subscriptions are disabled. Employees whose latest
mandate expires are suspended. The dashboard shows the last maintenance check;
append-only maintenance records are created only when state changes, avoiding
an unbounded idle-heartbeat ledger.

Before that maintenance can mutate state, Charterforge runs a fail-closed authority
preflight. It verifies SQLite integrity, foreign keys, the append-only business
audit hash chain, the active CEO and contiguous immutable employee-mandate
chains, and
objective/plan/action/result/verification lineage. It also compares the live
`agentic` configuration with the latest immutable policy baseline accepted
during setup.

An integrity failure or an unreviewed direct charter edit pauses autonomy,
revokes live permits and leases, records immutable evidence, and creates one
deduplicated advisor intervention. Charterforge never repairs or re-baselines the
authority store on its own. Rerunning reviewed agentic setup may append a new
policy baseline; after a clean preflight, the operator can explicitly resume
bounded autonomy. Failed integrity posture is visible on the Business
dashboard and included in the accountant/regulator audit package.

After a successful preflight, Charterforge maintains an offline, known-good authority
snapshot using SQLite's consistent backup API. Each snapshot is bound to the
organization, integrity-run evidence, database hash, and audit-chain head by a
hashed manifest. Snapshot creation fails closed when integrity is not ready;
the default cadence is daily with seven retained versions. A storage or
verification failure pauses autonomous operation instead of pretending that
recovery remains available.

Recovery is an operator-only edge command, not a model tool:

```bash
charterforge business recovery-list
charterforge business recovery-verify --snapshot /exact/snapshot.json
charterforge business autonomy paused --reason "prepare offline recovery"
# stop the supervised objective worker, then:
charterforge business recovery-restore \
  --snapshot /exact/snapshot.json \
  --reason "restore reviewed known-good authority" \
  --evidence '{"ticket":"REC-123","reviewed":true}'
```

Restore refuses an autonomous database or a recently active objective worker.
It verifies the snapshot before replacement, quarantines the prior database
and all SQLite sidecars, atomically installs the snapshot, revokes restored
permits and leases, and returns in paused mode. It then raises a mandatory
post-restore reconciliation intervention and separately identifies any permits
whose external effects were uncertain at snapshot time. Autonomy cannot resume
until integrity passes and recovery interventions are resolved with evidence;
external actions are never replayed merely because database time moved
backward. Recovery events are immutable and included in the audit export.
If the current database is too corrupt to prove its own paused state, restore
additionally requires advisor evidence containing
`"source_integrity_failed": true` and `"workers_stopped": true`. A readable
store cannot use this exception and must prove its paused mode and matching
organization directly.
