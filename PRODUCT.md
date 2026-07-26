# interfaze-agent Implementation Spec 

## API Routes and Frontend Structure

## 1. Core Implementation Decision

interfaze-agent will be built by editing and rebranding the existing Hermes source code.

Hermes should not stay as a separate external dependency. Instead:

```text
Hermes source code
        ↓
Fork / internal repo
        ↓
Rename product layer to interfaze-agent
        ↓
Keep agent runtime, tools, skills, gateway concepts
        ↓
Replace generic agent UI with B2B SaaS sales-agent interface
```

## 2. Product Behavior Changes

### MVP now supports

* Multi-company SaaS
* Admin-managed customers
* Company onboarding
* Internal company data ingestion
* Past sales and contact data ingestion
* Lead discovery
* Map-based country selection
* Lead research
* Contact discovery
* Custom lead creation
* Custom cold email generation
* Real cold email sending after approval
* Gmail / Google Workspace integration
* Microsoft 365 / Exchange integration
* WhatsApp Business integration
* LinkedIn profile finding and note generation
* CSV export
* Sales analytics
* Market intelligence analytics

### Important email behavior

The MVP should support both:

```text
1. Draft mode
2. Approved send mode
```

Draft mode is safer and should be the default. Approved send mode is required for custom-lead cold emails.

For Gmail, the API supports both creating drafts and sending messages; Gmail also supports sending from a draft.

For Microsoft 365 / Exchange Online, Microsoft Graph supports creating draft messages and sending mail.

---

# 3. Email Provider Support Versions

## Email Provider MVP — v1.0

Required:

```text
Google Workspace / Gmail
Microsoft 365 / Outlook / Exchange Online
```

Behavior:

* One company connects one main salesperson mailbox.
* Emails are sent from that connected salesperson mailbox.
* Other provided company emails can be added to CC.
* CC behavior can be configured by country, market, product, or campaign.

Example:

```text
Germany campaign:
From: sales@silverline.com
CC: export@silverline.com, germany@silverline.com

UAE campaign:
From: sales@silverline.com
CC: mena@silverline.com
```

## Email Provider v1.1

Add:

```text
Zoho Mail
Generic SMTP
```

Zoho has OAuth-based Mail APIs and draft/send workflows, so it is a reasonable next provider after Gmail and Microsoft.

Generic SMTP should be treated as a fallback, not the preferred option.

## Email Provider v1.2

Add managed sending providers:

```text
Resend
Mailgun
SendGrid
Brevo
Amazon SES
```

These are useful for platform-managed sending, but they are not ideal for “send from the actual salesperson mailbox” unless domain authentication and sender identity are configured correctly. Resend and Mailgun both expose email sending APIs.

## Email provider architecture

Use a provider adapter interface:

```text
email_providers/
  base.py
  gmail_provider.py
  microsoft_provider.py
  zoho_provider.py
  smtp_provider.py
  resend_provider.py
  mailgun_provider.py
```

Each provider should implement:

```text
connect_account()
refresh_token()
create_draft()
send_email()
send_draft()
get_message_status()
list_recent_replies()
disconnect_account()
```

---

# 4. WhatsApp Business Requirement

Customers must create or provide access to their own WhatsApp Business account.

The product should not use a personal WhatsApp Web automation flow. It should use WhatsApp Business Platform / Cloud API. Meta’s developer resources describe the WhatsApp Business Platform as the supported integration path for scalable business messaging.

## WhatsApp MVP behavior

```text
Customer creates WhatsApp Business account
        ↓
Customer connects phone number / business account
        ↓
interfaze-agent stores integration status
        ↓
System generates message
        ↓
User approves
        ↓
System sends through WhatsApp Business API
```

## WhatsApp onboarding fields

```text
business_name
whatsapp_business_account_id
phone_number_id
display_phone_number
business_country
default_language
template_status
webhook_verify_token
access_token_encrypted
```

---

# 5. LinkedIn Implementation Decision

For MVP, LinkedIn should support:

```text
Profile discovery
LinkedIn URL storage
Connection note generation
Manual open profile action
Manual status update
```

Do not ship automated LinkedIn connection requests through browser automation in the SaaS MVP unless you later confirm a compliant integration path. LinkedIn’s User Agreement prohibits scripts, robots, crawlers, browser plugins, or other technology used to scrape/copy LinkedIn services, and also prohibits unauthorized automated methods to add/download contacts or send messages.

So MVP behavior should be:

```text
Find LinkedIn profile
        ↓
Store URL
        ↓
Generate note
        ↓
User opens LinkedIn manually
        ↓
User sends connection request manually
        ↓
User marks status in interfaze-agent
```

---

# 6. Customer Onboarding Requirements

The onboarding must define the company deeply, not just collect basic profile info.

## 6.1 Company identity

```text
company_name
legal_name
website
headquarters_country
city
founded_year
industry
sub_industry
employee_count
business_model
main_language
sales_regions_current
sales_regions_target
```

## 6.2 Company positioning

```text
what_company_sells
main_value_proposition
quality_position
price_position
premium_or_mass_market
main_differentiators
certifications
manufacturing_capacity
export_capacity
delivery_capabilities
after_sales_support
```

## 6.3 Product data

```text
product_name
product_category
description
technical_specs
materials
certifications
target_industries
target_customer_types
buyer_roles
price_range_optional
moq_optional
production_capacity_optional
available_markets
restricted_markets
```

## 6.4 Internal sales data

This is now required for a strong Company Brain.

Supported internal data:

```text
past_sales
past_customers
current_contacts
existing_distributors
existing_dealers
lost_deals
active_deals
previous_outreach
best_performing_products
country_revenue_breakdown
customer_segments
average_order_value
sales_cycle_length
repeat_customers
customer_objections
support_questions
proposal_examples
email_examples
price_lists
```

## 6.5 Current contact data

Customers should be able to upload:

```text
customer_contact_list
lead_contact_list
distributor_contact_list
dealer_contact_list
partner_contact_list
supplier_contact_list
```

Fields:

```text
company_name
contact_name
title
email
phone
country
city
language
relationship_type
status
notes
last_contacted_at
source
```

## 6.6 Silverline demo onboarding example

```text
Company: Silverline
Country: Türkiye
Industry: Kitchen appliances
Business model: Manufacturer / exporter / supplier
Target buyers:
- appliance distributors
- kitchen appliance importers
- hotel equipment suppliers
- construction project suppliers
- kitchen design companies
- retail chains
- white goods dealers
```

---

# 7. API Route Structure

Base API prefix:

```text
/api/v1
```

---

## 7.1 Auth routes

```text
POST   /api/v1/auth/login
POST   /api/v1/auth/logout
GET    /api/v1/auth/me
POST   /api/v1/auth/refresh
POST   /api/v1/auth/password-reset/request
POST   /api/v1/auth/password-reset/confirm
```

Customer users are created by admin, not through open signup.

---

## 7.2 Admin company management

```text
GET    /api/v1/admin/companies
POST   /api/v1/admin/companies
GET    /api/v1/admin/companies/:companyId
PATCH  /api/v1/admin/companies/:companyId
DELETE /api/v1/admin/companies/:companyId

POST   /api/v1/admin/companies/:companyId/activate
POST   /api/v1/admin/companies/:companyId/disable
POST   /api/v1/admin/companies/:companyId/suspend
```

---

## 7.3 Admin user management

```text
GET    /api/v1/admin/users
POST   /api/v1/admin/users
GET    /api/v1/admin/users/:userId
PATCH  /api/v1/admin/users/:userId
DELETE /api/v1/admin/users/:userId

POST   /api/v1/admin/users/:userId/assign-company
POST   /api/v1/admin/users/:userId/reset-password
POST   /api/v1/admin/users/:userId/disable

GET    /api/v1/admin/errors
GET    /api/v1/admin/logs
```

The error and log views are administrator-only, cross-workspace operational
summaries. Responses must omit credentials, message bodies, and other secrets.

---

## 7.4 Company profile routes

```text
GET    /api/v1/company/profile
PATCH  /api/v1/company/profile

GET    /api/v1/company/positioning
PATCH  /api/v1/company/positioning

GET    /api/v1/company/sales-preferences
PATCH  /api/v1/company/sales-preferences
```

---

## 7.5 Company onboarding routes

```text
GET    /api/v1/onboarding/status
POST   /api/v1/onboarding/start
PATCH  /api/v1/onboarding/company-identity
PATCH  /api/v1/onboarding/positioning
PATCH  /api/v1/onboarding/products
PATCH  /api/v1/onboarding/internal-sales-data
PATCH  /api/v1/onboarding/current-contacts
PATCH  /api/v1/onboarding/target-markets
PATCH  /api/v1/onboarding/integrations
PATCH  /api/v1/onboarding/brain-review
POST   /api/v1/onboarding/complete
```

`current-contacts`, `integrations`, and `brain-review` persist WebUI progress.
The original five data-bearing steps remain the completion compatibility
boundary for existing API clients.

---

## 7.6 Document upload routes

```text
GET    /api/v1/documents
POST   /api/v1/documents/upload
GET    /api/v1/documents/:documentId
DELETE /api/v1/documents/:documentId

POST   /api/v1/documents/:documentId/process
GET    /api/v1/documents/:documentId/processing-status
```

Document types:

```text
product_catalog
technical_sheet
price_list
past_sales
past_customers
current_contacts
proposal_example
pitch_deck
certificate
case_study
dealer_list
distributor_list
lost_deals
other
```

---

## 7.7 Product routes

```text
GET    /api/v1/products
POST   /api/v1/products
GET    /api/v1/products/:productId
PATCH  /api/v1/products/:productId
DELETE /api/v1/products/:productId

POST   /api/v1/products/extract-from-documents
POST   /api/v1/products/:productId/generate-buyer-roles
POST   /api/v1/products/:productId/generate-market-fit
```

---

## 7.8 Company Brain routes

```text
GET    /api/v1/company-brain
POST   /api/v1/company-brain/build
POST   /api/v1/company-brain/rebuild
PATCH  /api/v1/company-brain
POST   /api/v1/company-brain/approve

GET    /api/v1/company-brain/snapshots
GET    /api/v1/company-brain/snapshots/:snapshotId
```

---

## 7.9 Lead map routes

```text
GET    /api/v1/lead-map/countries
GET    /api/v1/lead-map/countries/:countryCode
GET    /api/v1/lead-map/countries/:countryCode/summary
GET    /api/v1/lead-map/selected-countries
POST   /api/v1/lead-map/selected-countries
DELETE /api/v1/lead-map/selected-countries/:countryCode
```

Rules:

```text
Maximum selected countries per scan: 5
```

---

## 7.10 Lead scan routes

```text
GET    /api/v1/lead-scans
POST   /api/v1/lead-scans
GET    /api/v1/lead-scans/:scanId
POST   /api/v1/lead-scans/:scanId/start
POST   /api/v1/lead-scans/:scanId/cancel
POST   /api/v1/lead-scans/:scanId/retry
GET    /api/v1/lead-scans/:scanId/results
```

Create scan request body:

```json
{
  "countries": ["DE", "AE", "SA"],
  "product_ids": ["product_123"],
  "industries": ["kitchen appliances", "home appliances", "hospitality supply"],
  "target_company_types": ["distributor", "importer", "retailer"],
  "max_leads_per_country": 50,
  "scan_depth": "standard",
  "data_sources": ["web", "government_reports", "summit_lists", "company_directories"],
  "contact_discovery_enabled": true,
  "outreach_generation_enabled": true
}
```

---

## 7.11 Lead routes

```text
GET    /api/v1/leads
POST   /api/v1/leads
GET    /api/v1/leads/:leadId
PATCH  /api/v1/leads/:leadId
DELETE /api/v1/leads/:leadId

POST   /api/v1/leads/:leadId/research
POST   /api/v1/leads/:leadId/find-contacts
POST   /api/v1/leads/:leadId/generate-outreach
POST   /api/v1/leads/:leadId/mark-do-not-contact
POST   /api/v1/leads/:leadId/archive
```

The `POST /api/v1/leads` route is important for **custom-lead cold emails**.

Manual custom lead creation body:

```json
{
  "company_name": "Example Appliances GmbH",
  "website": "https://example.com",
  "country": "DE",
  "city": "Berlin",
  "industry": "Kitchen appliances distribution",
  "source": "manual",
  "notes": "Met at trade fair. Potential distributor."
}
```

---

## 7.12 Lead scoring routes

```text
GET    /api/v1/leads/:leadId/score
POST   /api/v1/leads/:leadId/score/recalculate
GET    /api/v1/leads/:leadId/score/explanation
```

Score explanation response should include:

```text
product_fit_score
market_fit_score
company_quality_score
intent_signal_score
contactability_score
insight_quality_score
source_confidence_score
final_score
explanation
```

---

## 7.13 Research routes

```text
GET    /api/v1/research
GET    /api/v1/research/:researchId
POST   /api/v1/research/company
POST   /api/v1/research/lead/:leadId
POST   /api/v1/research/bulk
GET    /api/v1/research/lead/:leadId/insights
POST   /api/v1/research/lead/:leadId/regenerate-insights
```

---

## 7.14 Contact routes

```text
GET    /api/v1/contacts
POST   /api/v1/contacts
GET    /api/v1/contacts/:contactId
PATCH  /api/v1/contacts/:contactId
DELETE /api/v1/contacts/:contactId

POST   /api/v1/contacts/discover
POST   /api/v1/contacts/:contactId/verify
POST   /api/v1/contacts/:contactId/mark-do-not-contact
```

Bulk discovery:

```text
POST /api/v1/contacts/discover
```

Body:

```json
{
  "lead_ids": ["lead_1", "lead_2"],
  "buyer_roles": ["import_manager", "purchasing_manager", "general_manager"],
  "channels": ["email", "phone", "linkedin", "whatsapp"],
  "max_contacts_per_company": 5
}
```

---

## 7.15 Outreach campaign routes

```text
GET    /api/v1/outreach/campaigns
POST   /api/v1/outreach/campaigns
GET    /api/v1/outreach/campaigns/:campaignId
PATCH  /api/v1/outreach/campaigns/:campaignId
DELETE /api/v1/outreach/campaigns/:campaignId

POST   /api/v1/outreach/campaigns/:campaignId/generate-messages
POST   /api/v1/outreach/campaigns/:campaignId/approve
POST   /api/v1/outreach/campaigns/:campaignId/send
POST   /api/v1/outreach/campaigns/:campaignId/pause
POST   /api/v1/outreach/campaigns/:campaignId/cancel
```

---

## 7.16 Custom-lead cold email routes

These are required for MVP.

```text
POST   /api/v1/custom-outreach/create-lead-and-message
POST   /api/v1/custom-outreach/generate-email
POST   /api/v1/custom-outreach/send-email
POST   /api/v1/custom-outreach/create-draft
```

Example flow:

```text
User manually adds custom lead
        ↓
System researches the company
        ↓
System finds contacts or user adds contact manually
        ↓
System generates custom cold email
        ↓
User approves
        ↓
System sends from connected salesperson mailbox
```

Request body:

```json
{
  "lead": {
    "company_name": "Example Appliances GmbH",
    "website": "https://example.com",
    "country": "DE",
    "industry": "Kitchen appliance distributor"
  },
  "contact": {
    "full_name": "Anna Müller",
    "title": "Purchasing Manager",
    "email": "anna@example.com"
  },
  "product_id": "product_123",
  "language": "de",
  "mode": "send",
  "cc_rule": "market_default"
}
```

---

## 7.17 Outreach message routes

```text
GET    /api/v1/outreach/messages
GET    /api/v1/outreach/messages/:messageId
PATCH  /api/v1/outreach/messages/:messageId

POST   /api/v1/outreach/messages/:messageId/regenerate
POST   /api/v1/outreach/messages/:messageId/approve
POST   /api/v1/outreach/messages/:messageId/create-draft
POST   /api/v1/outreach/messages/:messageId/send
POST   /api/v1/outreach/messages/:messageId/mark-sent-manually
POST   /api/v1/outreach/messages/:messageId/mark-replied
```

---

## 7.18 Email integration routes

```text
GET    /api/v1/integrations/email
POST   /api/v1/integrations/email/connect/google
POST   /api/v1/integrations/email/connect/microsoft
POST   /api/v1/integrations/email/connect/smtp
POST   /api/v1/integrations/email/connect/browser

GET    /api/v1/integrations/email/:integrationId
PATCH  /api/v1/integrations/email/:integrationId
DELETE /api/v1/integrations/email/:integrationId

POST   /api/v1/integrations/email/:integrationId/test
POST   /api/v1/integrations/email/:integrationId/refresh-token
```

---

## 7.19 Email sending routes

```text
POST   /api/v1/email/drafts
POST   /api/v1/email/send
POST   /api/v1/email/send-bulk
GET    /api/v1/email/sent
GET    /api/v1/email/replies
GET    /api/v1/email/status/:providerMessageId
```

Send email request:

```json
{
  "integration_account_id": "email_integration_123",
  "lead_id": "lead_123",
  "contact_id": "contact_123",
  "outreach_message_id": "message_123",
  "to": "buyer@example.com",
  "cc": ["export@silverline.com"],
  "subject": "Potential kitchen appliance distribution partnership",
  "body": "Email body here...",
  "language": "en",
  "send_mode": "approved_send"
}
```

---

## 7.20 CC rules routes

```text
GET    /api/v1/cc-rules
POST   /api/v1/cc-rules
GET    /api/v1/cc-rules/:ruleId
PATCH  /api/v1/cc-rules/:ruleId
DELETE /api/v1/cc-rules/:ruleId
```

CC rule fields:

```text
name
market_country
market_region
product_id
industry
cc_emails
is_default
```

Example:

```json
{
  "name": "Germany Export CC",
  "market_country": "DE",
  "cc_emails": ["export@silverline.com", "germany@silverline.com"],
  "is_default": false
}
```

---

## 7.21 WhatsApp integration routes

```text
GET    /api/v1/integrations/whatsapp
POST   /api/v1/integrations/whatsapp/connect
GET    /api/v1/integrations/whatsapp/profile
PUT    /api/v1/integrations/whatsapp/profile
POST   /api/v1/integrations/whatsapp/profile/verify
GET    /api/v1/integrations/whatsapp/:integrationId
PATCH  /api/v1/integrations/whatsapp/:integrationId
DELETE /api/v1/integrations/whatsapp/:integrationId

POST   /api/v1/integrations/whatsapp/:integrationId/test
POST   /api/v1/integrations/whatsapp/webhook
```

The profile routes persist non-secret WhatsApp Business identifiers separately
from server-managed credentials. Profile verification is a readiness check; it
does not prove live Meta API access.

---

## 7.22 WhatsApp message routes

```text
GET    /api/v1/whatsapp/messages
POST   /api/v1/whatsapp/messages/generate
POST   /api/v1/whatsapp/messages/:messageId/approve
POST   /api/v1/whatsapp/messages/:messageId/send
GET    /api/v1/whatsapp/messages/:messageId/status
POST   /api/v1/whatsapp/messages/:messageId/mark-replied
POST   /api/v1/whatsapp/messages/:messageId/mark-opt-out
```

---

## 7.23 LinkedIn routes

```text
GET    /api/v1/linkedin/actions
POST   /api/v1/linkedin/find-profile
POST   /api/v1/linkedin/generate-note
POST   /api/v1/linkedin/actions/:actionId/mark-opened
POST   /api/v1/linkedin/actions/:actionId/mark-connection-sent
POST   /api/v1/linkedin/actions/:actionId/mark-connected
POST   /api/v1/linkedin/actions/:actionId/mark-replied
```

---

## 7.24 Agent run routes

```text
GET    /api/v1/agent-runs
POST   /api/v1/agent-runs
GET    /api/v1/agent-runs/:runId
POST   /api/v1/agent-runs/:runId/start
POST   /api/v1/agent-runs/:runId/cancel
POST   /api/v1/agent-runs/:runId/retry
GET    /api/v1/agent-runs/:runId/logs
GET    /api/v1/agent-runs/:runId/events
```

Run types:

```text
company_brain_build
document_processing
product_extraction
lead_scan
lead_research
contact_discovery
outreach_generation
email_send
whatsapp_send
linkedin_note_generation
analytics_refresh
```

---

## 7.25 Analytics routes

Customer analytics:

```text
GET    /api/v1/analytics/overview
GET    /api/v1/analytics/dashboard
GET    /api/v1/analytics/sales-pipeline
GET    /api/v1/analytics/market-intelligence
GET    /api/v1/analytics/leads-by-country
GET    /api/v1/analytics/leads-by-industry
GET    /api/v1/analytics/contactability
GET    /api/v1/analytics/outreach
GET    /api/v1/analytics/source-performance
GET    /api/v1/analytics/product-market-fit
```

`GET /api/v1/analytics/dashboard` is the tenant-scoped composite used by the
customer dashboard. It combines sales totals, market signals, recent activity,
recommended actions, and selected countries.

Admin analytics:

```text
GET    /api/v1/admin/analytics/overview
GET    /api/v1/admin/analytics/companies
GET    /api/v1/admin/analytics/usage
GET    /api/v1/admin/analytics/agent-runs
GET    /api/v1/admin/analytics/errors
GET    /api/v1/admin/analytics/integrations
GET    /api/v1/admin/analytics/costs
```

---

## 7.26 Export routes

```text
POST   /api/v1/exports/leads
POST   /api/v1/exports/contacts
POST   /api/v1/exports/research
POST   /api/v1/exports/outreach
POST   /api/v1/exports/analytics

GET    /api/v1/exports/:exportId
GET    /api/v1/exports/:exportId/download
```

Export formats:

```text
csv
xlsx later
json later
```

---

## 7.27 Data source routes

```text
GET    /api/v1/data-sources
POST   /api/v1/data-sources
GET    /api/v1/data-sources/:sourceId
PATCH  /api/v1/data-sources/:sourceId
DELETE /api/v1/data-sources/:sourceId

POST   /api/v1/data-sources/:sourceId/test
POST   /api/v1/data-sources/:sourceId/enable
POST   /api/v1/data-sources/:sourceId/disable
```

Source types:

```text
web_search
government_reports
company_registries
summit_lists
exhibitor_lists
procurement_data
trade_data
linkedin_reference
uploaded_internal_data
manual_upload
```

---

## 7.28 Activity log routes

```text
GET    /api/v1/activity
GET    /api/v1/activity/:activityId
GET    /api/v1/leads/:leadId/activity
GET    /api/v1/contacts/:contactId/activity
GET    /api/v1/outreach/campaigns/:campaignId/activity
```

---

# 8. Frontend Structure

## 8.1 Recommended repo structure

```text
interfaze-agent/
  apps/
    web/
      src/
        app/
        assets/
        components/
        config/
        features/
        hooks/
        layouts/
        lib/
        pages/
        routes/
        services/
        stores/
        styles/
        types/

  agent/
    company_brain/
    lead_discovery/
    research/
    contact_discovery/
    outreach/
    integrations/

  gateway/
  tools/
  skills/
  providers/
  server/
  docs/
```

Since this is built from Hermes, preserve the useful Hermes folders, but create a clean product boundary around the SaaS web app.

---

## 8.2 Frontend route structure

```text
/
  /login
  /access-pending

/app
  /dashboard
  /onboarding
  /company-brain
  /lead-map
  /leads
  /leads/:leadId
  /contacts
  /contacts/:contactId
  /outreach
  /outreach/campaigns/:campaignId
  /custom-outreach
  /analytics
  /agent-runs
  /agent-runs/:runId
  /integrations
  /settings

/admin
  /dashboard
  /companies
  /companies/:companyId
  /users
  /agent-runs
  /analytics
  /integrations
  /errors
  /logs
  /data-sources
```

---

# 9. Frontend Feature Modules

## 9.1 Auth feature

```text
features/auth/
  components/
    LoginForm.tsx
    AccessPending.tsx
    ProtectedRoute.tsx
    RoleGate.tsx
  hooks/
    useAuth.ts
  services/
    authApi.ts
  types.ts
```

---

## 9.2 Dashboard feature

```text
features/dashboard/
  components/
    DashboardHeader.tsx
    CompanyBrainSummaryCard.tsx
    SalesPipelineCards.tsx
    MarketIntelligenceCards.tsx
    RecentAgentActivity.tsx
    RecommendedActions.tsx
    CountryOpportunityMapPreview.tsx
  hooks/
    useDashboard.ts
  services/
    dashboardApi.ts
```

Dashboard should show both sales and market intelligence:

```text
Sales:
- leads found
- contacts found
- emails sent
- replies
- interested leads
- WhatsApp messages
- campaign status

Market intelligence:
- best countries
- product-market fit
- top industries
- source performance
- opportunity score by country
```

---

## 9.3 Onboarding feature

```text
features/onboarding/
  components/
    OnboardingStepper.tsx
    CompanyIdentityStep.tsx
    PositioningStep.tsx
    ProductCatalogStep.tsx
    InternalSalesDataStep.tsx
    CurrentContactsStep.tsx
    TargetMarketsStep.tsx
    IntegrationSetupStep.tsx
    CompanyBrainReviewStep.tsx
  hooks/
    useOnboarding.ts
  services/
    onboardingApi.ts
  types.ts
```

Internal data upload should be a major step, not hidden under documents.

---

## 9.4 Company Brain feature

```text
features/company-brain/
  components/
    CompanyBrainOverview.tsx
    ProductUnderstandingPanel.tsx
    IdealCustomerProfilePanel.tsx
    BuyerRolesPanel.tsx
    MarketAssumptionsPanel.tsx
    SalesArgumentsPanel.tsx
    MissingDataPanel.tsx
    BrainSnapshotHistory.tsx
  hooks/
    useCompanyBrain.ts
  services/
    companyBrainApi.ts
```

---

## 9.5 Lead Map feature

```text
features/lead-map/
  components/
    WorldCountrySelector.tsx
    CountrySidePanel.tsx
    SelectedCountriesBar.tsx
    LeadScanConfigModal.tsx
    ScanDepthSelector.tsx
    DataSourceSelector.tsx
    ProductSelector.tsx
    IndustrySelector.tsx
  hooks/
    useLeadMap.ts
    useSelectedCountries.ts
  services/
    leadMapApi.ts
```

MVP behavior:

```text
Map only selects countries.
Maximum 5 countries.
No complex heatmap required in MVP.
```

---

## 9.6 Leads feature

```text
features/leads/
  components/
    LeadsTable.tsx
    LeadFilters.tsx
    LeadScoreBadge.tsx
    LeadStatusBadge.tsx
    LeadDetailHeader.tsx
    LeadResearchPanel.tsx
    LeadContactsPanel.tsx
    LeadOutreachPanel.tsx
    LeadActivityTimeline.tsx
    ManualLeadCreateModal.tsx
  hooks/
    useLeads.ts
    useLeadDetail.ts
  services/
    leadsApi.ts
```

Manual lead creation is required for custom-lead cold emails.

---

## 9.7 Contacts feature

```text
features/contacts/
  components/
    ContactsTable.tsx
    ContactFilters.tsx
    ContactScoreBadge.tsx
    BuyerRoleBadge.tsx
    ContactDetailPanel.tsx
    ContactVerificationStatus.tsx
    ManualContactCreateModal.tsx
  hooks/
    useContacts.ts
  services/
    contactsApi.ts
```

---

## 9.8 Outreach feature

```text
features/outreach/
  components/
    OutreachCampaignsTable.tsx
    OutreachMessageQueue.tsx
    EmailEditor.tsx
    EmailPreview.tsx
    ApprovalControls.tsx
    SendModeSelector.tsx
    CCRuleSelector.tsx
    LanguageSelector.tsx
    RegenerateMessageButton.tsx
    CustomLeadColdEmailFlow.tsx
  hooks/
    useOutreach.ts
    useEmailDrafts.ts
    useEmailSending.ts
  services/
    outreachApi.ts
    emailApi.ts
```

Send modes:

```text
create_draft
approved_send
```

---

## 9.9 Custom Outreach feature

```text
features/custom-outreach/
  components/
    CustomLeadForm.tsx
    CustomContactForm.tsx
    CustomLeadResearchStep.tsx
    CustomEmailGenerator.tsx
    CustomEmailApproval.tsx
    CustomEmailSendResult.tsx
  hooks/
    useCustomOutreach.ts
  services/
    customOutreachApi.ts
```

Flow:

```text
Create lead
Add/find contact
Research company
Generate email
Approve email
Create draft or send
```

---

## 9.10 Analytics feature

```text
features/analytics/
  components/
    AnalyticsOverview.tsx
    SalesPipelineAnalytics.tsx
    MarketIntelligenceAnalytics.tsx
    LeadsByCountryChart.tsx
    LeadsByIndustryChart.tsx
    ProductMarketFitChart.tsx
    SourcePerformanceChart.tsx
    OutreachPerformanceChart.tsx
    ContactabilityChart.tsx
    ExportAnalyticsButton.tsx
  hooks/
    useAnalytics.ts
  services/
    analyticsApi.ts
```

Both analytics types are MVP:

```text
Sales pipeline analytics
Market intelligence analytics
```

---

## 9.11 Integrations feature

```text
features/integrations/
  components/
    IntegrationsOverview.tsx
    EmailIntegrationCard.tsx
    GoogleConnectButton.tsx
    MicrosoftConnectButton.tsx
    ZohoConnectButton.tsx
    SMTPConnectForm.tsx
    WhatsAppBusinessConnectForm.tsx
    IntegrationStatusBadge.tsx
    TestIntegrationButton.tsx
  hooks/
    useIntegrations.ts
  services/
    integrationsApi.ts
```

---

## 9.12 Agent Runs feature

```text
features/agent-runs/
  components/
    AgentRunsTable.tsx
    AgentRunDetail.tsx
    AgentRunStatusBadge.tsx
    AgentRunLogs.tsx
    AgentRunTimeline.tsx
    AgentRunRetryButton.tsx
    AgentRunCancelButton.tsx
  hooks/
    useAgentRuns.ts
  services/
    agentRunsApi.ts
```

---

## 9.13 Admin feature

```text
features/admin/
  components/
    AdminDashboard.tsx
    CompaniesTable.tsx
    CompanyDetailAdmin.tsx
    UsersTable.tsx
    UserCreateModal.tsx
    AdminAgentRunsTable.tsx
    AdminAnalytics.tsx
    IntegrationHealthTable.tsx
    ErrorLogsTable.tsx
    DataSourcesTable.tsx
  hooks/
    useAdminCompanies.ts
    useAdminUsers.ts
    useAdminAnalytics.ts
  services/
    adminApi.ts
```

---

# 10. MVP Page Priority

Build in this order:

## Sprint 1: Foundation

```text
Auth
Admin company creation
Admin user creation
Customer shell layout
Dashboard placeholder
Settings placeholder
```

## Sprint 2: Onboarding and Company Brain

```text
Company onboarding
Document upload
Internal sales data upload
Product management
Company Brain build route
Company Brain review UI
```

## Sprint 3: Lead Discovery

```text
Lead Map
Country selector
Scan configuration
Agent run creation
Lead table
Lead detail page
```

## Sprint 4: Research and Contacts

```text
Research insights UI
Contact discovery
Contact table
Contact scoring
Manual contact creation
```

## Sprint 5: Outreach

```text
Email integration
CC rules
Custom lead outreach
Email generation
Draft creation
Approved sending
Outreach campaign table
```

## Sprint 6: WhatsApp, LinkedIn, Analytics

```text
WhatsApp Business integration
WhatsApp message approval/send
LinkedIn profile storage
LinkedIn note generation
Sales analytics
Market intelligence analytics
CSV export
```

---

# 11. MVP Demo Flow for Silverline

## Demo scenario

```text
Silverline wants to sell kitchen appliances globally.
```

## Demo countries

Suggested first scan:

```text
Germany
United Arab Emirates
Saudi Arabia
Netherlands
United Kingdom
```

## Demo flow

```text
1. Admin creates Silverline company.
2. Admin creates Silverline customer user.
3. Silverline logs in.
4. Silverline completes onboarding.
5. Silverline uploads product catalog, past sales, current contacts, and distributor list.
6. Agent builds Company Brain.
7. User reviews and approves Company Brain.
8. User opens Lead Map.
9. User selects 5 countries.
10. User starts standard lead scan.
11. System discovers appliance distributors/importers.
12. System researches selected leads.
13. System finds buyer contacts.
14. User creates custom lead manually.
15. Agent researches custom lead.
16. Agent generates personalized cold email.
17. User selects connected salesperson mailbox.
18. System adds market-specific CC.
19. User approves.
20. System sends email or creates draft.
21. Analytics updates.
```

---

# 12. Final MVP Rules

```text
Hermes and interfaze-agent are merged into one editable product codebase.

FastAPI remains the recommended backend because the agent layer is Python-heavy.

Supabase is used for auth, database, and storage.

Admin can do everything.

Customers are managed by admin.

Customers cannot invite users in MVP.

Map UI is country selection only.

Maximum 5 countries per scan.

Email supports drafts and approved sending.

Default email identity is one connected salesperson mailbox.

Additional company emails can be added as CC based on market rules.

WhatsApp sending is enabled through customer-provided WhatsApp Business setup.

LinkedIn automation is not shipped as browser-controlled connection sending in MVP.

CSV export is included.

Analytics includes both sales pipeline and market intelligence.

Custom-lead cold email sending is included in MVP.
```
