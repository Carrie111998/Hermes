# Tool and Integration Development Guide

Follow the footprint ladder in [AGENTS.md](../AGENTS.md):

1. extend existing code;
2. CLI command plus skill;
3. service-gated tool;
4. standalone plugin;
5. MCP server;
6. new core model tool only as a last resort.

Vendor products, payment providers, accounting platforms, and other external
services should be standalone plugins/packages, not coupled into the core
tree. A governed integration needs:

- typed request and response contracts;
- exact required capability and system;
- idempotency key and target resource;
- spend, rate, reversibility, and expiry metadata;
- secret handling outside prompts and durable public records;
- deterministic input validation;
- external read-back or deterministic verification;
- failure classification and bounded backoff;
- audit-safe evidence with sensitive fields removed.

Tools must treat email, web pages, documents, customer text, and agent messages
as untrusted data. They cannot grant authority, change success criteria, or
declare completion.

Payment tools must remain non-custodial and use opaque provider identifiers.
They must not accept raw PANs, bank credentials, private keys, or seed phrases.

Canonical new names use `charterforge`, `CHARTERFORGE_`, and the
`charterforge` namespace. A compatibility use of a Hermes identifier must be
documented and covered by a migration test.

