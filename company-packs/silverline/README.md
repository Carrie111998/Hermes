# Silverline — demo Company Brain data pack

Demo/company-specific data for the interfaze-agent Sales Agent MVP, extracted
from the hand-built OpenClaw prototype ("silver-claw") and scrubbed of
personal data and credentials. The generic logic lives in `skills/sales/`;
this pack is one company's instantiation of it, and doubles as the demo
company from PRODUCT.md §6.6 / §11.

| File | Company Brain slot |
|---|---|
| `company.yaml` | Company identity, positioning, target buyers, sender identity, assets |
| `business-rules.md` | Contact policy, approval model, channel state machine, message standards |
| `market-preferences.yaml` | Client-selected markets: target / no-outreach / no-research (replaces the old territory matrix) |
| `cc-rules.yaml` | Market CC rules (PRODUCT.md §7.20) |
| `templates/email-templates.md` | Email templates, all language variants, fixed-subject translation table |
| `templates/whatsapp-templates.md` | WhatsApp templates + media bundle spec |
| `templates/linkedin-note-templates.md` | Connection note templates + country→language map |

Placeholders (`{{sender_name}}`, `{{sender_email}}`, `{{sender_phone}}`,
`{{internal_sales_email}}`, `{{product_photo_block}}`) are resolved from
onboarding/integration data at runtime — real values are never committed.

This pack is the demo instance of
[`../company-config-schema.yaml`](../company-config-schema.yaml): every client
configures the same fields through the frontend; only Silverline's demo
values ship in the repo.
