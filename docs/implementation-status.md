# Implementation Status and Known Limitations

## Implemented

- Persistent governed objective/event runtime and explicit lifecycle state.
- Founder/CEO organization bootstrap and versioned mandate.
- Advisor-by-default authority policy.
- Exact solo-founder and employee delegation grants.
- Evidence-based contractor/FTE decision and hierarchical profiles.
- Treasury, budgets, reservations, double-entry accounting, fiscal periods,
  tax records, payment intents, and verification.
- Compliance inventory, obligations, deadlines, evidence, and audit export.
- Recovery snapshots, leases, retries, circuit breakers, stop reasons, and
  interventions.
- Canonical Charterforge Python distribution/namespace/CLI plus migration
  aliases.

## Partial

- The authority store is SQLite, not Postgres.
- Event scheduling is implemented in-process; Kafka/RabbitMQ are not bundled.
- The inherited UI and internal code still contain legacy identifiers where
  changing them would break migration or require a separate subsystem rewrite.
- The optional `packages/charterforge-stripe-rail` package provides a concrete
  HTTP Stripe Checkout/read-back rail and a narrowly scoped Connected Account
  outbound path. It is not installed by the core runtime; live settlement
  still requires separate installation, credentials, provider assessment, and
  jurisdictional compliance evidence.
- AgentMail configuration exists; provider availability and plan terms are
  external facts.
- Compliance tracking exists; legal applicability is not autonomously proven.
- Local terminal execution is available but is not isolation.

## Not implemented or not proven

- Corporate formation, beneficial-owner filings, bank-account opening, or
  legal personhood for the agent.
- Autonomous signing of contracts where law requires a human/legal principal.
- Universal tax filing in every jurisdiction.
- PCI DSS, SOC 2, SOX, GDPR, EU AI Act, CASL, CAN-SPAM, or other certification.
- A published Charterforge installer, package release, container registry
  image, production deployment, or repository rename.
- Trademark clearance for Charterforge.
- High-availability database replication or disaster-recovery drill evidence.

## Validation evidence

The exact-grant increment passed 281 focused tests. The rebrand and migration
validation passed 361 focused Python tests, desktop/TUI/web TypeScript
typechecks, Python compilation, and the implemented `charterforge` help and
version commands. The independent documentation site prebuild completed, but
its Docusaurus build was unavailable because the local `docusaurus` executable
is not installed; no build pass is claimed.
