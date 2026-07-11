# Company data packs

One directory per company = one Company Brain instance. Clients configure all
of this through the frontend (onboarding + settings); these files are the
on-disk shape of that configuration, defined by
[`company-config-schema.yaml`](company-config-schema.yaml).

- **`silverline/`** — the demo company (real spelling: Silverline). The only
  pack committed to the repo; it is scrubbed and placeholder-based.
- Real client packs are generated per tenant at runtime and are **gitignored**
  — never commit client data.

Division of labor:

| Who | Configures | Where |
|---|---|---|
| Client | company identity, positioning, products, contacts, market preferences (target / no-outreach / no-research), outreach rules (daily limits, send windows, re-reach days, exclusions), CC rules, templates | Frontend onboarding + settings (PRODUCT.md §6, §7.5, §7.20) |
| Admin | model providers + API keys (incl. local models), email/WhatsApp OAuth apps, platform limits, data sources | Admin panel |
| Repo | company-agnostic skill logic | `skills/sales/` |
