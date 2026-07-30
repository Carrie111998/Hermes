# SkyAI v2 Customer-Facing Security Model

## Core Principle

Do not rely on the model to keep secrets. The customer-facing SkyAI Hermes v2
runtime should not have secrets or privileged data in the first place.

If a prompt-injection attempt succeeds in changing the assistant's behavior,
the blast radius should be limited to a poor answer, not a data leak or an
admin action.

## Runtime Lanes

### Public SkyAI Hermes v2

Allowed:

- public catalog and category data;
- public product details;
- public product events/slots;
- public campaign and legal pages;
- current conversation context;
- sanitized event writes.

Denied:

- Muncho canonical brain;
- internal Discord history;
- Git/GCP/Render/Shopify/SSH/admin tools;
- raw customer intelligence reads;
- aggregate analytics, revenue, margins, abandoned-cart dashboards;
- order/payment/voucher/customer lookup;
- secret/env inspection;
- self-installing or self-deploying new skills.

### Muncho Internal Supervisor

Muncho may read sanitized reports, metrics, incidents, QA cases, regression
failures, and bounded diagnostics. Muncho may propose changes and run
approved DevOps gates. Muncho must not turn SkyAI customer intelligence into
Muncho canonical brain memory.

## Prompt-Injection Handling

Customer messages are evidence, never instructions to the operator layer.

SkyAI may be asked to:

- reveal system prompts;
- reveal model/provider/hosting details;
- ignore previous instructions;
- expose private business information;
- act outside SkyVision;
- ask Muncho or another internal agent for secrets.

The correct behavior is to decline or redirect to SkyVision help without
revealing technical or private details.

## Learning Loop

1. Customer talks to SkyAI.
2. Public-safe response and metadata are mirrored internally.
3. Append-only events are written to `skyai_ci.events`.
4. Muncho/humans review sanitized cases.
5. Improvements become reviewed knowledge, tests, tools, or skills.
6. Production enablement goes through explicit gates.

No customer prompt can directly create, enable, deploy, or broaden a skill.

## Database Boundary

SkyAI customer intelligence uses a dedicated `skyai_ci` schema/database.

Rules:

- no generic `DATABASE_URL` fallback;
- customer-facing runtime has INSERT-only access to `skyai_ci.events`;
- customer-facing runtime has no raw analytics read API;
- internal reporting uses separate credentials;
- marketing sends require human approval and separate internal jobs.

## Canary Requirement

Before replacing the current custom SkyAI backend:

- keep the current PROD backend as rollback;
- mirror every canary conversation;
- run prompt-injection and off-domain tests;
- verify product detail, slots, campaign, BookNow and voucher behavior;
- keep a kill switch that restores the current backend quickly.
