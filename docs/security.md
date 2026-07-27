# Security Model and Threat Model

## Security model

Charterforge separates proposal, policy, execution, verification, and state
commit. It uses least-privilege permits, payload hashes, idempotency keys,
resource budgets, immutable evidence, exact employee grants, intervention
queues, and circuit breakers.

### Concrete resource authorization

Authorization is bound to the exact action, not merely to an agent name or a
general tool label. A permit stores the action payload hash and target resource;
consumption rejects a changed path, resource, executor, organization, or
capability. Therefore a hypothetical `filesystem.read` permit for
`/home/mike/ceofile.txt` cannot be retargeted to
`/home/mike/notceofile.txt`, and it cannot be used for a write action. A write
requires a separately proposed action, a distinct write capability, a new
permit, and a policy decision within the current objective and charter.

Employee grants apply the same non-amplification rule across the subordinate
mandate and the delegator mandate, including capabilities, systems, toolsets,
skills, budget, and expiry. Filesystem access is only available through a
registered typed executor; raw shell access is not implied by a read grant.

External vendor plugins and payment rails should remain separate packages.
Credentials must not enter prompts, task grants, audit exports, or authority
records. Payment integration is non-custodial: providers hold instruments and
funds; Charterforge stores opaque references and verified state.
Durable business-audit payloads and planner lineage redact credential-like
fields before hashing and persistence; ordinary non-sensitive planner response
text remains byte-for-byte evidence.
Authenticated external-event receipts apply the same redaction to adapter
payloads and authentication evidence before durable storage or CEO-planner
context; provider adapters remain responsible for cryptographic verification.
When `agentic.security.require_runtime_baseline` is enabled, each unattended
cycle also compares the non-secret charter, authority-store schema, Python
runtime, and dependency-lock/package identity with a human-accepted baseline.
Drift pauses autonomy and opens an intervention; the planner cannot silently
repair or rebaseline the host.

The optional Stripe package follows this boundary: it uses an in-memory HTTP
client, sends the secret only to Stripe, and records no raw card or bank data.
Its outbound implementation is deliberately limited to explicitly identified
Connected Accounts and fails closed for arbitrary payees.
Stripe webhook ingress verifies the provider timestamp/HMAC signature before
writing a typed, idempotent external-event receipt; raw webhook bodies are not
persisted.

Terminal execution can use inherited local, Docker, SSH, Singularity, Modal,
or Daytona backends. A local terminal is not a sandbox. Production operators
must select an isolation backend and appropriate egress/secrets controls for
untrusted code.

## Threats and controls

| Threat | Implemented control | Residual risk |
|---|---|---|
| Prompt injection from email/web | External text cannot alter deterministic grants or permits | Model may still propose a harmful in-scope action |
| Authority expansion | Charter policy, exact payload permit, exact worker surfaces | Misconfigured initial charter remains powerful |
| Duplicate external action | Idempotency contract and immutable result lineage | Provider must honor or expose idempotency |
| Runaway loop/cost | Objective/action/token/compute ceilings and circuit breakers | Incorrect ceilings can still waste resources |
| Concurrent modification | Claims and resource leases | External systems may have weaker concurrency guarantees |
| False completion | Independent verifier and read-back records | Weak verifier design can accept incomplete reality |
| Credential disclosure | Secret stores and credential-free planning context | Tool/provider bugs remain possible |
| Financial loss | Reservations, limits, allowlists, provider verification | Authorized transactions may still be commercially poor |
| State/database loss | Backup, integrity, recovery snapshots | Single-host SQLite is not high availability |
| Audit repudiation | Append-only records, hashes, KYA event chain | Host administrator can still replace the whole database |

## Kill switch and revocation

Stopping the runtime must prevent new claims and permits. Charter changes revoke
stale authority. Offboarding and mandate supersession invalidate task handoff.
Operators should additionally revoke provider tokens and stop external
schedulers during a full autonomy shutdown.

## Compliance boundary

The compliance catalog helps track CAN-SPAM, CASL, GDPR, the EU AI Act, SOX,
PCI DSS, PIPEDA, RPAA, FINTRAC, and other regimes. Catalog presence is not an
applicability conclusion. Qualified legal, tax, privacy, security, or
accounting review is still required where the facts demand it.
# Event ingress authentication boundary

Objective wake-ups from external systems are accepted only when the adapter
supplies an explicit `signature_validated: true` or `verified: true` marker.
The routing layer still treats payload and text as untrusted, stores a
credential-redacted immutable receipt, and applies idempotent source identity.
Adapters remain responsible for performing the provider-specific cryptographic
or platform authentication before calling the router; an evidence dictionary
without a validated marker is rejected. When an adapter supplies
`signed_timestamp`, `authenticated_at`, or `timestamp`, the router rejects
malformed, future-dated, or stale evidence (older than five minutes, with a
thirty-second future-skew allowance) before waking objectives. This is a
freshness boundary, not a substitute for provider-specific signature
verification.
