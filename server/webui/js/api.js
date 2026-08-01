/* ============================================================
   interfaze-agent — central API client.

   EVERY page talks to data exclusively through call(name, opts).
   The full /api/v1 route surface from the product spec is
   declared in `routes` below, so the UI is already wired for the
   real backend.

   Backend modes (config.mode):
     'mock'   — every route served by js/mocks/handlers.js.
     'hybrid' — real interfaze-api backend, except the logical names
                in MOCK_ROUTES (UI-extra routes the spec/backend does
                not implement, plus shape-deferred ones). Default.
     'real'   — everything hits the backend (no mock fallback).
   A real 404 is NEVER silently served from mocks — a legitimate
   "not found" must surface as an error, not as demo data.

   Real-backend contract notes (see server/WEBUI_CONNECTION_PRD.md):
     - Bearer auth via config.authHeader (set at boot from session.js).
     - Backend list endpoints return bare arrays; they are wrapped to
       { items, total } here. Mutations return the affected entity.
     - Per-route request/response shims live in js/adapters.js
       (DataPatch {data} envelopes, auth.login composite, ...).
     - Errors: non-2xx with { error | message | detail } -> ApiError.
   ============================================================ */

import { adaptRequest, adaptResponse, normalizeResponseTimestamps } from './adapters.js';
import { syncRealResponse } from './real-state.js';

export const config = {
  mode: 'real',              // production/test default; mock mode must be explicit
  baseUrl: '/api/v1',
  latencyMs: [120, 420],     // simulated mock latency range
  authHeader: null,          // () => string | null
  beforeRequest: null,       // (req) => req
  refreshAuth: null,         // async () => replacement Authorization value
  onAuthFailure: null,       // () => clear session and return to login
  chatEnabled: globalThis.window?.__HERMES_CONFIG__?.chatEnabled === true,
  // Future sales backend only. The browser must never call Hermes directly.
  // That backend owns the Hermes session, CSRF/cookie flow, and all provider secrets.
  agentAdapter: {
    mode: 'mock',
    enabled: false,
  },
};

export class ApiError extends Error {
  constructor(message, status = 500, code = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

/* ---------------- Route surface (product spec §7) ---------------- */
export const routes = {
  // 7.1 Auth
  'auth.login':                 ['POST',   '/auth/login'],
  'auth.logout':                ['POST',   '/auth/logout'],
  'auth.me':                    ['GET',    '/auth/me'],
  'auth.refresh':               ['POST',   '/auth/refresh'],
  'auth.passwordResetRequest':  ['POST',   '/auth/password-reset/request'],
  'auth.passwordResetConfirm':  ['POST',   '/auth/password-reset/confirm'],

  // 7.2 Admin company management
  'admin.companies.list':       ['GET',    '/admin/companies'],
  'admin.companies.create':     ['POST',   '/admin/companies'],
  'admin.companies.get':        ['GET',    '/admin/companies/:companyId'],
  'admin.companies.update':     ['PATCH',  '/admin/companies/:companyId'],
  'admin.companies.delete':     ['DELETE', '/admin/companies/:companyId'],
  'admin.companies.activate':   ['POST',   '/admin/companies/:companyId/activate'],
  'admin.companies.disable':    ['POST',   '/admin/companies/:companyId/disable'],
  'admin.companies.suspend':    ['POST',   '/admin/companies/:companyId/suspend'],

  // 7.3 Admin user management
  'admin.users.list':           ['GET',    '/admin/users'],
  'admin.users.create':         ['POST',   '/admin/users'],
  'admin.users.get':            ['GET',    '/admin/users/:userId'],
  'admin.users.update':         ['PATCH',  '/admin/users/:userId'],
  'admin.users.delete':         ['DELETE', '/admin/users/:userId'],
  'admin.users.assignCompany':  ['POST',   '/admin/users/:userId/assign-company'],
  'admin.users.resetPassword':  ['POST',   '/admin/users/:userId/reset-password'],
  'admin.users.disable':        ['POST',   '/admin/users/:userId/disable'],
  'admin.errors':               ['GET',    '/admin/errors'],
  'admin.logs':                 ['GET',    '/admin/logs'],

  // 7.4 Company profile
  'company.getProfile':         ['GET',    '/company/profile'],
  'company.updateProfile':      ['PATCH',  '/company/profile'],
  'company.getPositioning':     ['GET',    '/company/positioning'],
  'company.updatePositioning':  ['PATCH',  '/company/positioning'],
  'company.getSalesPreferences':    ['GET',   '/company/sales-preferences'],
  'company.updateSalesPreferences': ['PATCH', '/company/sales-preferences'],
  'company.getEmailTemplates':      ['GET',   '/company/email-templates'],
  'company.updateEmailTemplates':   ['PATCH', '/company/email-templates'],

  // 7.5 Onboarding
  'onboarding.status':              ['GET',   '/onboarding/status'],
  'onboarding.start':               ['POST',  '/onboarding/start'],
  'onboarding.updateCompanyIdentity':  ['PATCH', '/onboarding/company-identity'],
  'onboarding.updatePositioning':      ['PATCH', '/onboarding/positioning'],
  'onboarding.updateProducts':         ['PATCH', '/onboarding/products'],
  'onboarding.updateInternalSalesData':['PATCH', '/onboarding/internal-sales-data'],
  'onboarding.updateCurrentContacts':  ['PATCH', '/onboarding/current-contacts'],
  'onboarding.updateTargetMarkets':    ['PATCH', '/onboarding/target-markets'],
  'onboarding.updateIntegrations':     ['PATCH', '/onboarding/integrations'],
  'onboarding.reviewBrain':            ['PATCH', '/onboarding/brain-review'],
  'onboarding.complete':            ['POST',  '/onboarding/complete'],

  // 7.6 Documents
  'documents.list':             ['GET',    '/documents'],
  'documents.upload':           ['POST',   '/documents/upload'],
  'documents.get':              ['GET',    '/documents/:documentId'],
  'documents.delete':           ['DELETE', '/documents/:documentId'],
  'documents.process':          ['POST',   '/documents/:documentId/process'],
  'documents.processingStatus': ['GET',    '/documents/:documentId/processing-status'],

  // 7.7 Products
  'products.list':                ['GET',    '/products'],
  'products.create':              ['POST',   '/products'],
  'products.get':                 ['GET',    '/products/:productId'],
  'products.update':              ['PATCH',  '/products/:productId'],
  'products.delete':              ['DELETE', '/products/:productId'],
  'products.extractFromDocuments':['POST',   '/products/extract-from-documents'],
  'products.generateBuyerRoles':  ['POST',   '/products/:productId/generate-buyer-roles'],
  'products.generateMarketFit':   ['POST',   '/products/:productId/generate-market-fit'],

  // 7.8 Company Brain
  'brain.get':                  ['GET',    '/company-brain'],
  'brain.build':                ['POST',   '/company-brain/build'],
  'brain.rebuild':              ['POST',   '/company-brain/rebuild'],
  'brain.update':               ['PATCH',  '/company-brain'],
  'brain.approve':              ['POST',   '/company-brain/approve'],
  'brain.snapshots':            ['GET',    '/company-brain/snapshots'],
  'brain.snapshot':             ['GET',    '/company-brain/snapshots/:snapshotId'],

  // 7.9 Lead map
  'leadMap.countries':          ['GET',    '/lead-map/countries'],
  'leadMap.country':            ['GET',    '/lead-map/countries/:countryCode'],
  'leadMap.countrySummary':     ['GET',    '/lead-map/countries/:countryCode/summary'],
  'leadMap.selectedCountries':  ['GET',    '/lead-map/selected-countries'],
  'leadMap.selectCountry':      ['POST',   '/lead-map/selected-countries'],
  'leadMap.deselectCountry':    ['DELETE', '/lead-map/selected-countries/:countryCode'],

  // 7.10 Lead scans
  'leadScans.list':             ['GET',    '/lead-scans'],
  'leadScans.create':           ['POST',   '/lead-scans'],
  'leadScans.get':              ['GET',    '/lead-scans/:scanId'],
  'leadScans.start':            ['POST',   '/lead-scans/:scanId/start'],
  'leadScans.cancel':           ['POST',   '/lead-scans/:scanId/cancel'],
  'leadScans.retry':            ['POST',   '/lead-scans/:scanId/retry'],
  'leadScans.results':          ['GET',    '/lead-scans/:scanId/results'],

  // Evidence-first research campaigns
  'researchCampaigns.list':     ['GET',    '/research-campaigns'],
  'researchCampaigns.create':   ['POST',   '/research-campaigns'],
  'researchCampaigns.get':      ['GET',    '/research-campaigns/:campaignId'],
  'researchCampaigns.patch':    ['PATCH',  '/research-campaigns/:campaignId'],
  'researchCampaigns.delete':   ['DELETE', '/research-campaigns/:campaignId'],
  'researchCampaigns.estimate': ['POST',   '/research-campaigns/:campaignId/estimate'],
  'researchCampaigns.start':    ['POST',   '/research-campaigns/:campaignId/start'],
  'researchCampaigns.cancel':   ['POST',   '/research-campaigns/:campaignId/cancel'],
  'researchCampaigns.retry':    ['POST',   '/research-campaigns/:campaignId/retry'],
  'researchCampaigns.clone':    ['POST',   '/research-campaigns/:campaignId/clone'],
  'researchCampaigns.metrics':  ['GET',    '/research-campaigns/:campaignId/metrics'],
  'researchCampaigns.sources':  ['GET',    '/research-campaigns/:campaignId/source-runs'],
  'researchCampaigns.issues':   ['GET',    '/research-campaigns/:campaignId/issues'],
  'researchCampaigns.leads':    ['GET',    '/research-campaigns/:campaignId/leads'],
  'researchCampaigns.export':   ['POST',   '/research-campaigns/:campaignId/export'],
  'research.configuration':     ['GET',    '/research/configuration'],
  'research.sectors':           ['GET',    '/research/sectors'],
  'research.scoringProfiles':   ['GET',    '/research/scoring-profiles'],
  'research.enrichmentProfiles':['GET',    '/research/enrichment-profiles'],
  'research.modelProfiles':     ['GET',    '/research/model-profiles'],
  'research.leadClaims':        ['GET',    '/research/leads/:leadId/claims'],

  // 7.11 Leads
  'leads.list':                 ['GET',    '/leads'],
  'leads.create':               ['POST',   '/leads'],
  'leads.get':                  ['GET',    '/leads/:leadId'],
  'leads.update':               ['PATCH',  '/leads/:leadId'],
  'leads.delete':               ['DELETE', '/leads/:leadId'],
  'leads.research':             ['POST',   '/leads/:leadId/research'],
  'leads.findContacts':         ['POST',   '/leads/:leadId/find-contacts'],
  'leads.generateOutreach':     ['POST',   '/leads/:leadId/generate-outreach'],
  'leads.markDoNotContact':     ['POST',   '/leads/:leadId/mark-do-not-contact'],
  'leads.archive':              ['POST',   '/leads/:leadId/archive'],

  // 7.12 Lead scoring
  'leads.score':                ['GET',    '/leads/:leadId/score'],
  'leads.scoreRecalculate':     ['POST',   '/leads/:leadId/score/recalculate'],
  'leads.scoreExplanation':     ['GET',    '/leads/:leadId/score/explanation'],

  // 7.13 Research
  'research.list':              ['GET',    '/research'],
  'research.get':               ['GET',    '/research/:researchId'],
  'research.company':           ['POST',   '/research/company'],
  'research.lead':              ['POST',   '/research/lead/:leadId'],
  'research.bulk':              ['POST',   '/research/bulk'],
  'research.leadInsights':      ['GET',    '/research/lead/:leadId/insights'],
  'research.regenerateInsights':['POST',   '/research/lead/:leadId/regenerate-insights'],

  // 7.14 Contacts
  'contacts.list':              ['GET',    '/contacts'],
  'contacts.create':            ['POST',   '/contacts'],
  'contacts.get':               ['GET',    '/contacts/:contactId'],
  'contacts.update':            ['PATCH',  '/contacts/:contactId'],
  'contacts.delete':            ['DELETE', '/contacts/:contactId'],
  'contacts.discover':          ['POST',   '/contacts/discover'],
  'contacts.verify':            ['POST',   '/contacts/:contactId/verify'],
  'contacts.markDoNotContact':  ['POST',   '/contacts/:contactId/mark-do-not-contact'],

  // 7.15 Outreach campaigns
  'campaigns.list':             ['GET',    '/outreach/campaigns'],
  'campaigns.create':           ['POST',   '/outreach/campaigns'],
  'campaigns.get':              ['GET',    '/outreach/campaigns/:campaignId'],
  'campaigns.update':           ['PATCH',  '/outreach/campaigns/:campaignId'],
  'campaigns.delete':           ['DELETE', '/outreach/campaigns/:campaignId'],
  'campaigns.generateMessages': ['POST',   '/outreach/campaigns/:campaignId/generate-messages'],
  'campaigns.approve':          ['POST',   '/outreach/campaigns/:campaignId/approve'],
  'campaigns.send':             ['POST',   '/outreach/campaigns/:campaignId/send'],
  'campaigns.pause':            ['POST',   '/outreach/campaigns/:campaignId/pause'],
  'campaigns.cancel':           ['POST',   '/outreach/campaigns/:campaignId/cancel'],

  // 7.17 Outreach messages
  'messages.list':              ['GET',    '/outreach/messages'],
  'messages.get':               ['GET',    '/outreach/messages/:messageId'],
  'messages.update':            ['PATCH',  '/outreach/messages/:messageId'],
  'messages.regenerate':        ['POST',   '/outreach/messages/:messageId/regenerate'],
  'messages.approve':           ['POST',   '/outreach/messages/:messageId/approve'],
  'messages.createDraft':       ['POST',   '/outreach/messages/:messageId/create-draft'],
  'messages.send':              ['POST',   '/outreach/messages/:messageId/send'],
  'messages.markSentManually':  ['POST',   '/outreach/messages/:messageId/mark-sent-manually'],
  'messages.markReplied':       ['POST',   '/outreach/messages/:messageId/mark-replied'],

  // 7.16 Custom-lead cold email (MVP-required)
  'customOutreach.createLeadAndMessage': ['POST', '/custom-outreach/create-lead-and-message'],
  'customOutreach.generateEmail':        ['POST', '/custom-outreach/generate-email'],
  'customOutreach.sendEmail':            ['POST', '/custom-outreach/send-email'],
  'customOutreach.createDraft':          ['POST', '/custom-outreach/create-draft'],

  // 7.18 Email integrations
  'emailIntegrations.list':          ['GET',    '/integrations/email'],
  'emailIntegrations.startOAuth':    ['POST',   '/integrations/email/oauth/:provider/start'],
  'emailIntegrations.connectGoogle': ['POST',   '/integrations/email/connect/google'],
  'emailIntegrations.connectMicrosoft': ['POST','/integrations/email/connect/microsoft'],
  'emailIntegrations.connectZoho':   ['POST',   '/integrations/email/connect/zoho'],
  'emailIntegrations.connectSmtp':   ['POST',   '/integrations/email/connect/smtp'],
  'emailIntegrations.connectBrowser':['POST',   '/integrations/email/connect/browser'],
  'emailIntegrations.get':           ['GET',    '/integrations/email/:integrationId'],
  'emailIntegrations.update':        ['PATCH',  '/integrations/email/:integrationId'],
  'emailIntegrations.delete':        ['DELETE', '/integrations/email/:integrationId'],
  'emailIntegrations.test':          ['POST',   '/integrations/email/:integrationId/test'],
  'emailIntegrations.refreshToken':  ['POST',   '/integrations/email/:integrationId/refresh-token'],

  // 7.19 Email sending
  'email.createDraft':          ['POST',   '/email/drafts'],
  'email.send':                 ['POST',   '/email/send'],
  'email.sendBulk':             ['POST',   '/email/send-bulk'],
  'email.sent':                 ['GET',    '/email/sent'],
  'email.replies':              ['GET',    '/email/replies'],
  'email.status':               ['GET',    '/email/status/:providerMessageId'],

  // 7.20 CC rules
  'ccRules.list':               ['GET',    '/cc-rules'],
  'ccRules.create':             ['POST',   '/cc-rules'],
  'ccRules.get':                ['GET',    '/cc-rules/:ruleId'],
  'ccRules.update':             ['PATCH',  '/cc-rules/:ruleId'],
  'ccRules.delete':             ['DELETE', '/cc-rules/:ruleId'],

  // 7.21 WhatsApp integration
  'whatsapp.integrations':      ['GET',    '/integrations/whatsapp'],
  'whatsapp.connect':           ['POST',   '/integrations/whatsapp/connect'],
  'whatsapp.getIntegration':    ['GET',    '/integrations/whatsapp/:integrationId'],
  'whatsapp.updateIntegration': ['PATCH',  '/integrations/whatsapp/:integrationId'],
  'whatsapp.deleteIntegration': ['DELETE', '/integrations/whatsapp/:integrationId'],
  'whatsapp.testIntegration':   ['POST',   '/integrations/whatsapp/:integrationId/test'],
  'whatsapp.profile':           ['GET',    '/integrations/whatsapp/profile'],
  'whatsapp.saveProfile':       ['PUT',    '/integrations/whatsapp/profile'],
  'whatsapp.verifyProfile':     ['POST',   '/integrations/whatsapp/profile/verify'],

  // 7.22 WhatsApp messages
  'whatsapp.messages':          ['GET',    '/whatsapp/messages'],
  'whatsapp.generate':          ['POST',   '/whatsapp/messages/generate'],
  'whatsapp.approve':           ['POST',   '/whatsapp/messages/:messageId/approve'],
  'whatsapp.send':              ['POST',   '/whatsapp/messages/:messageId/send'],
  'whatsapp.status':            ['GET',    '/whatsapp/messages/:messageId/status'],
  'whatsapp.markReplied':       ['POST',   '/whatsapp/messages/:messageId/mark-replied'],
  'whatsapp.markOptOut':        ['POST',   '/whatsapp/messages/:messageId/mark-opt-out'],

  // 7.23 LinkedIn
  'linkedin.actions':           ['GET',    '/linkedin/actions'],
  'linkedin.findProfile':       ['POST',   '/linkedin/find-profile'],
  'linkedin.generateNote':      ['POST',   '/linkedin/generate-note'],
  'linkedin.markOpened':        ['POST',   '/linkedin/actions/:actionId/mark-opened'],
  'linkedin.markConnectionSent':['POST',   '/linkedin/actions/:actionId/mark-connection-sent'],
  'linkedin.markConnected':     ['POST',   '/linkedin/actions/:actionId/mark-connected'],
  'linkedin.markReplied':       ['POST',   '/linkedin/actions/:actionId/mark-replied'],

  // 7.24 Agent runs
  'agentRuns.list':             ['GET',    '/agent-runs'],
  'agentRuns.create':           ['POST',   '/agent-runs'],
  'agentRuns.get':              ['GET',    '/agent-runs/:runId'],
  'agentRuns.start':            ['POST',   '/agent-runs/:runId/start'],
  'agentRuns.cancel':           ['POST',   '/agent-runs/:runId/cancel'],
  'agentRuns.retry':            ['POST',   '/agent-runs/:runId/retry'],
  'agentRuns.logs':             ['GET',    '/agent-runs/:runId/logs'],
  'agentRuns.events':           ['GET',    '/agent-runs/:runId/events'],
  'agent.capabilities':         ['GET',    '/agent/capabilities'],
  'agent.status':               ['GET',    '/agent/status'],

  // 7.26 Exports
  'exports.leads':              ['POST',   '/exports/leads'],
  'exports.contacts':           ['POST',   '/exports/contacts'],
  'exports.research':           ['POST',   '/exports/research'],
  'exports.outreach':           ['POST',   '/exports/outreach'],
  'exports.analytics':          ['POST',   '/exports/analytics'],
  'exports.get':                ['GET',    '/exports/:exportId'],
  'exports.download':           ['GET',    '/exports/:exportId/download'],

  // 7.27 Data sources
  'dataSources.list':           ['GET',    '/data-sources'],
  'dataSources.create':         ['POST',   '/data-sources'],
  'dataSources.get':            ['GET',    '/data-sources/:sourceId'],
  'dataSources.update':         ['PATCH',  '/data-sources/:sourceId'],
  'dataSources.delete':         ['DELETE', '/data-sources/:sourceId'],
  'dataSources.test':           ['POST',   '/data-sources/:sourceId/test'],
  'dataSources.enable':         ['POST',   '/data-sources/:sourceId/enable'],
  'dataSources.disable':        ['POST',   '/data-sources/:sourceId/disable'],
  'dataSources.catalog':        ['GET',    '/data-sources/catalog'],
  'dataSources.impact':         ['GET',    '/data-sources/:sourceId/impact'],
  'dataSources.install':        ['POST',   '/data-sources/:sourceId/install'],
  'dataSources.uninstall':      ['POST',   '/data-sources/:sourceId/uninstall'],
  'dataSources.purge':          ['POST',   '/data-sources/:sourceId/purge'],

  // 7.28 Activity
  'activity.list':              ['GET',    '/activity'],
  'activity.get':               ['GET',    '/activity/:activityId'],
  'activity.forLead':           ['GET',    '/leads/:leadId/activity'],
  'activity.forContact':        ['GET',    '/contacts/:contactId/activity'],
  'activity.forCampaign':       ['GET',    '/outreach/campaigns/:campaignId/activity'],

  // Dashboard + analytics aggregates (frontend contract; backend TBD)
  'dashboard.summary':          ['GET',    '/analytics/dashboard'],
  'analytics.pipeline':         ['GET',    '/analytics/sales-pipeline'],
  'analytics.market':           ['GET',    '/analytics/market-intelligence'],
};

/* ---------------- Hybrid mode: routes that stay on mocks ----------------
   Two admission reasons only (PRD §4):
   - UI-EXTRA: the route is a frontend addition beyond PRODUCT.md §7 and has
     no backend implementation.
   - DEFERRED: the backend route exists but the request/response shape (or
     transport) is not adapted yet; flipping it early would break the page.
   Shrink this set as phases land; never add try-real-fallback-mock logic. */
export const MOCK_ROUTES = new Set();

/* ---------------- Mock plumbing ---------------- */
let _mockHandlers = null;
async function loadMockHandlers() {
  if (!_mockHandlers) {
    const mod = await import('./mocks/handlers.js');
    _mockHandlers = mod.handlers;
  }
  return _mockHandlers;
}

function randomDelay() {
  const [lo, hi] = config.latencyMs;
  const ms = lo + Math.random() * (hi - lo);
  return new Promise(res => setTimeout(res, ms));
}

function interpolate(template, params = {}) {
  return template.replace(/:([A-Za-z]+)/g, (_, key) => {
    if (params[key] == null) throw new ApiError(`Missing path param :${key} for ${template}`, 400);
    return encodeURIComponent(params[key]);
  });
}

function errorMessage(payload) {
  if (!payload) return null;
  if (payload.error) return payload.error;
  if (payload.message) return payload.message;
  const d = payload.detail; // FastAPI: string, {..}, or a 422 validation array
  if (typeof d === 'string') return d;
  if (Array.isArray(d) && d.length) return d[0]?.msg || JSON.stringify(d[0]);
  if (d && typeof d === 'object') return d.message || JSON.stringify(d);
  return null;
}

function responseFilename(header, fallback) {
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(header || '')?.[1];
  if (encoded) {
    try { return decodeURIComponent(encoded); } catch { return encoded; }
  }
  return /filename="?([^";]+)"?/i.exec(header || '')?.[1] || fallback;
}

/* ---------------- The single entry point ---------------- */
export async function call(name, { params, query, body } = {}) {
  const decl = routes[name];
  if (!decl) throw new ApiError(`Unknown API route '${name}'`, 400);
  const [method, template] = decl;

  const useMock = config.mode === 'mock' || (config.mode === 'hybrid' && MOCK_ROUTES.has(name));
  if (useMock) {
    const handlers = await loadMockHandlers();
    const handler = handlers[name];
    if (!handler) throw new ApiError(`Mock handler missing for '${name}' (${method} ${template})`, 501);
    await randomDelay();
    return handler({ params: params || {}, query: query || {}, body: body || null });
  }

  return realCall(name, { params, query, body });
}

/* Real-backend request. `authOverride` lets response adapters chain follow-up
   requests before the session token is stored (e.g. the auth.login composite). */
async function realCall(name, { params, query, body, authOverride, authRetried = false } = {}) {
  const [method, template] = routes[name];
  let url = config.baseUrl + interpolate(template, params);
  if (query && Object.keys(query).length) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) if (v != null && v !== '') qs.set(k, v);
    const s = qs.toString();
    if (s) url += '?' + s;
  }

  const requestBody = adaptRequest(name, body);
  let req = {
    method,
    headers: { 'Accept': 'application/json' },
    credentials: 'include',
  };
  if (requestBody != null) {
    const multipart = typeof FormData !== 'undefined' && requestBody instanceof FormData;
    if (multipart) {
      // The browser supplies the multipart boundary. Setting Content-Type
      // manually would make FastAPI reject an otherwise valid upload.
      req.body = requestBody;
    } else {
      req.headers['Content-Type'] = 'application/json';
      req.body = JSON.stringify(requestBody);
    }
  }
  const auth = authOverride ?? (typeof config.authHeader === 'function' ? config.authHeader() : null);
  if (auth) req.headers['Authorization'] = auth;
  if (typeof config.beforeRequest === 'function') req = config.beforeRequest({ url, ...req }) || req;

  const res = await fetch(url, req);
  if (res.status === 401 && !authRetried && authOverride == null
      && !['auth.login', 'auth.refresh'].includes(name)
      && typeof config.refreshAuth === 'function') {
    const replacement = await config.refreshAuth();
    if (replacement) {
      return realCall(name, { params, query, body, authOverride: replacement, authRetried: true });
    }
    config.onAuthFailure?.();
  }
  if (res.status === 204) return null;
  if (name === 'exports.download') {
    if (!res.ok) {
      let errorPayload = null;
      try { errorPayload = await res.json(); } catch { /* non-JSON error */ }
      throw new ApiError(
        errorMessage(errorPayload) || `${method} ${url} failed (${res.status})`,
        res.status,
        errorPayload?.code,
      );
    }
    const blob = await res.blob();
    return {
      blob,
      filename: responseFilename(res.headers.get('Content-Disposition'), `${params?.exportId || 'export'}.csv`),
    };
  }
  let payload = null;
  try { payload = await res.json(); } catch { /* non-JSON body */ }
  if (!res.ok) {
    throw new ApiError(errorMessage(payload) || `${method} ${url} failed (${res.status})`, res.status, payload?.code);
  }
  if (Array.isArray(payload)) payload = { items: payload, total: payload.length };
  const adapted = normalizeResponseTimestamps(
    await adaptResponse(name, payload, { realCall, params, query, body, requestBody }),
  );
  syncRealResponse(name, adapted, { params, query, body });
  return adapted;
}
