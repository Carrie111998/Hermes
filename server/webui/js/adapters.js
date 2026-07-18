/* ============================================================
   interfaze-agent — real-backend adapters.

   Per-route shims between the UI's mock-era contract and the real
   interfaze-api backend (server/WEBUI_CONNECTION_PRD.md §3.3).
   Consulted only on the real path in api.js; mock handlers keep the
   original shapes. Keyed by logical route name.

   Cross-cutting rules handled here:
     - DataPatch envelope: company/onboarding/brain PATCH routes take
       { data: {...} } with extra="forbid" — flat bodies are a 422.
     - Section envelope: company GETs return
       { company_id, data, updated_at } — pages expect the flat object.
     - auth.login composite: backend returns
       { access_token, refresh_token, ... }; the UI session needs
       { token, user, company }.
   ============================================================ */

/* PATCH routes whose real body must be wrapped as { data: {...} }. */
const DATA_ENVELOPE_ROUTES = new Set([
  'company.updateProfile',
  'company.updatePositioning',
  'company.updateSalesPreferences',
  'onboarding.updateCompanyIdentity',
  'onboarding.updatePositioning',
  'onboarding.updateProducts',
  'onboarding.updateInternalSalesData',
  'onboarding.updateCurrentContacts',
  'onboarding.updateTargetMarkets',
  'onboarding.updateIntegrations',
  'onboarding.reviewBrain',
  'brain.update',
  'company.updateEmailTemplates',
]);

/* Routes whose real response is the company-section envelope. */
const SECTION_ROUTES = new Set([
  'company.getProfile',
  'company.updateProfile',
  'company.getPositioning',
  'company.updatePositioning',
  'company.getSalesPreferences',
  'company.updateSalesPreferences',
  'company.getEmailTemplates',
  'company.updateEmailTemplates',
]);

function displayName(email) {
  const local = String(email || '').split('@')[0];
  if (!local) return 'User';
  return local.replace(/[._-]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

function timestampValue(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return value;
  return new Date(value < 100000000000 ? value * 1000 : value).toISOString();
}

export function normalizeResponseTimestamps(value, key = '') {
  if (Array.isArray(value)) return value.map(item => normalizeResponseTimestamps(item));
  if (!value || typeof value !== 'object') {
    return /(?:_at|^at|_time)$/.test(key) ? timestampValue(value) : value;
  }
  return Object.fromEntries(Object.entries(value).map(([childKey, child]) => [
    childKey,
    normalizeResponseTimestamps(child, childKey),
  ]));
}

/* Backend /auth/me returns the flat principal {id,email,role,company_id,status};
   the UI renders user.name, which the backend does not store. */
function userFromPrincipal(me) {
  return { ...me, name: me?.name || displayName(me?.email) };
}

function mapItems(payload, mapper) {
  if (!payload || !Array.isArray(payload.items)) return payload;
  const mapped = payload.items.map(mapper);
  return { ...payload, items: mapped, total: mapped.length };
}

function score(value) {
  if (value && typeof value === 'object' && value.value != null) {
    return { factors: [], ...value };
  }
  const numeric = Number(value?.final_score ?? value ?? 0) || 0;
  return {
    value: numeric,
    band: numeric >= 75 ? 'high' : numeric >= 50 ? 'mid' : 'low',
    factors: Array.isArray(value?.factors) ? value.factors : [],
    explanation: value?.explanation || '',
  };
}

function lead(value = {}) {
  return {
    ...value,
    city: value.city || '',
    industry: value.industry || '',
    source: value.source || 'manual',
    score: score(value.score),
  };
}

function contact(value = {}) {
  const data = value.data || {};
  const status = value.status || 'unverified';
  return {
    ...value,
    ...data,
    data,
    name: data.full_name || data.name || value.full_name || value.email || 'Unnamed contact',
    full_name: data.full_name || data.name || value.full_name || '',
    title: data.title || value.title || '',
    email_status: status === 'active' ? 'unverified' : status,
  };
}

function product(value = {}) {
  const data = value.data || {};
  return { ...value, ...data, name: value.name || value.product_name || data.product_name || 'Product' };
}

function campaign(value = {}) {
  const data = value.data || {};
  return {
    ...value,
    ...data,
    data,
    country: data.country || value.country || '',
    language: data.language || value.language || 'en',
    send_mode: data.send_mode || value.send_mode || 'create_draft',
  };
}

function message(value = {}) {
  const content = value.content || {};
  const status = value.status === 'pending_approval' ? 'draft_generated'
    : value.status === 'draft' ? 'draft_created' : value.status;
  return {
    ...value,
    ...(value.data || {}),
    ...content,
    content,
    status,
    cc: Array.isArray(content.cc) ? content.cc : [],
    language: content.language || value.language || 'en',
  };
}

function research(value) {
  if (!value) return { status: 'pending', summary: '', insights: [] };
  const source = value.insights && !Array.isArray(value.insights) ? value.insights : value;
  const cards = [];
  const pushObject = (label, object) => {
    if (!object || typeof object !== 'object' || Array.isArray(object)) return;
    const text = Object.entries(object).map(([key, item]) => `${key.replace(/_/g, ' ')}: ${String(item)}`).join(' · ');
    if (text) cards.push({ title: label, body: text });
  };
  pushObject('Company profile', source.profile);
  pushObject('Commercial fit', source.fit);
  for (const signal of Array.isArray(source.signals) ? source.signals : []) {
    cards.push(typeof signal === 'object'
      ? { title: signal.title || 'Signal', body: signal.body || signal.note || JSON.stringify(signal) }
      : { title: 'Signal', body: String(signal) });
  }
  return {
    ...value,
    status: ['succeeded', 'completed'].includes(value.status) || !value.status ? 'completed' : value.status,
    summary: source.approach_angle || source.summary || 'Research completed. Review the structured signals below.',
    insights: cards,
    raw_insights: source,
  };
}

const RUN_LABELS = {
  company_brain_build: 'Company Brain build', document_processing: 'Document processing',
  product_extraction: 'Product extraction', lead_scan: 'Lead scan', lead_research: 'Lead research',
  contact_discovery: 'Contact discovery', outreach_generation: 'Outreach generation',
  email_send: 'Email delivery', whatsapp_send: 'WhatsApp delivery',
  linkedin_note_generation: 'LinkedIn note generation', analytics_refresh: 'Analytics refresh',
};

function run(value = {}) {
  const type = value.type || value.run_type;
  const status = value.status === 'succeeded' ? 'completed' : value.status;
  const terminal = ['completed', 'failed', 'cancelled'].includes(status);
  return {
    ...value,
    type,
    run_type: type,
    run_id: value.run_id || value.id,
    label: value.label || RUN_LABELS[type] || String(type || 'Agent run').replace(/_/g, ' '),
    status,
    progress: value.progress ?? (terminal ? 100 : status === 'running' ? 50 : 0),
    related: value.related || value.payload || {},
    finished_at: value.finished_at || value.completed_at || null,
    logs: value.logs || [],
  };
}

function activity(value = {}) {
  return {
    ...value,
    kind: value.kind || value.data?.kind || (String(value.action || '').includes('repl') ? 'reply' : 'agent'),
    label: value.label || String(value.action || 'activity').replace(/_/g, ' '),
    at: value.at || value.created_at,
    ref: value.ref || { [value.entity_type ? `${value.entity_type}_id` : 'entity_id']: value.entity_id },
  };
}

function brain(value) {
  const defaults = {
    product_understanding: [], ideal_customer_profile: [], buyer_roles: [],
    market_assumptions: [], sales_arguments: [], business_rules_digest: [], missing_data: [],
  };
  if (!value) return { id: null, status: 'not_built', version: 0, approved_at: null, sections: defaults, snapshots: [] };
  const sections = { ...defaults, ...(value.sections || value.content || {}) };
  return { ...value, ...sections, sections, approved_at: value.approved_at || null };
}

function document(value = {}) {
  return {
    ...(value.data || {}),
    ...value,
    type: value.type || value.document_type || 'other',
    size_kb: value.size_kb ?? Math.ceil((value.size_bytes || 0) / 1024),
    uploaded_at: value.uploaded_at || value.created_at,
  };
}

function integration(value = {}) {
  return { ...(value.data || {}), ...value, data: value.data || {} };
}

function adminCompany(value = {}) {
  return { ...(value.data || {}), ...value, data: value.data || {} };
}

function adminUser(value = {}) {
  return {
    ...(value.data || {}), ...value, data: value.data || {},
    name: value.name || value.data?.name || displayName(value.email),
  };
}

function weeklySeries(value) {
  return value && Array.isArray(value.labels) && Array.isArray(value.values)
    ? value
    : { labels: ['W-7', 'W-6', 'W-5', 'W-4', 'W-3', 'W-2', 'W-1', 'Now'], values: Array(8).fill(0) };
}

export function adaptRequest(name, body) {
  if (DATA_ENVELOPE_ROUTES.has(name)) return { data: body || {} };
  if (name === 'leadMap.selectCountry') {
    // UI sends { country_code }; backend CountrySelection wants { countries: [...] }
    const countries = body?.countries || (body?.country_code ? [body.country_code] : []);
    return { countries };
  }
  if (name === 'leadScans.create') {
    return {
      countries: body?.countries || [],
      product_ids: body?.product_ids || body?.products || [],
      industries: body?.industries || [],
      target_company_types: body?.target_company_types || [],
      max_leads_per_country: body?.max_leads_per_country || body?.leads_per_country || 50,
      scan_depth: body?.scan_depth || body?.depth || 'standard',
      data_sources: body?.data_sources || body?.sources || ['web'],
      contact_discovery_enabled: body?.contact_discovery_enabled ?? true,
      outreach_generation_enabled: body?.outreach_generation_enabled ?? false,
    };
  }
  if (name === 'contacts.create' || name === 'contacts.update') {
    const { full_name, name: contactName, title, ...rest } = body || {};
    return { ...rest, data: { ...(rest.data || {}), full_name: full_name || contactName || '', title: title || '' } };
  }
  if (name === 'contacts.discover') {
    return { ...body, lead_ids: body?.lead_ids || (body?.lead_id ? [body.lead_id] : []) };
  }
  if (name === 'leads.update') {
    const { company_name, website, country, status, data, ...extra } = body || {};
    return { company_name, website, country, status, data: { ...(data || {}), ...extra } };
  }
  if (name === 'products.create' || name === 'products.update') {
    const { product_name, name: productName, data, ...extra } = body || {};
    return { product_name: product_name || productName, data: { ...(data || {}), ...extra } };
  }
  if (name === 'campaigns.create' || name === 'campaigns.update') {
    const { name: campaignName, channel, lead_ids, data, ...extra } = body || {};
    const request = { name: campaignName, channel: channel || 'email', data: { ...(data || {}), ...extra } };
    if (name === 'campaigns.create' || lead_ids !== undefined) request.lead_ids = lead_ids || [];
    return request;
  }
  if (name === 'messages.update') return { content: body || {} };
  if (name === 'campaigns.generateMessages' || name === 'campaigns.send' || name === 'brain.build' || name === 'brain.rebuild') {
    return body || {};
  }
  return body;
}

export async function adaptResponse(name, payload, { realCall, body }) {
  if (SECTION_ROUTES.has(name)) {
    return { id: payload?.company_id, ...(payload?.data || {}), updated_at: payload?.updated_at };
  }

  if (name === 'auth.me') {
    return {
      user: userFromPrincipal(payload),
      company: { id: payload?.company_id || null, name: '' },
    };
  }

  if (name === 'auth.login') {
    // Composite: token -> who am I -> which company. Session shape:
    // { token, user, company } (login.js stores exactly these).
    const auth = `Bearer ${payload.access_token}`;
    const me = await realCall('auth.me', { authOverride: auth });
    let company = me.company;
    if (company.id) {
      try {
        const profile = await realCall('company.getProfile', { authOverride: auth });
        company = { ...company, name: profile?.name || profile?.legal_name || '' };
      } catch { /* no profile yet — name stays empty */ }
    } else if (me.user?.role === 'admin') {
      try {
        const companies = await realCall('admin.companies.list', { authOverride: auth });
        const active = companies?.items?.filter(item => item.status === 'active') || [];
        if (active.length === 1) company = { id: active[0].id, name: active[0].name };
      } catch { /* admin can still use global pages without a selected workspace */ }
    }
    return {
      token: payload.access_token,
      refresh_token: payload.refresh_token,
      expires_in: payload.expires_in,
      expires_at: Date.now() + Number(payload.expires_in || 3600) * 1000,
      user: me.user,
      company,
    };
  }

  if (name === 'leadMap.selectedCountries' || name === 'leadMap.selectCountry') {
    return { ...(payload || {}), items: payload?.items || [], total: payload?.items?.length || 0, max: 5 };
  }
  if (name === 'leadMap.countrySummary' || name === 'leadMap.country') {
    return {
      ...payload,
      opportunity_score: payload?.opportunity_score ?? 0,
      recommended: payload?.recommended ?? false,
      market_size: payload?.market_size || 'Market sizing is not available yet.',
      trade_note: payload?.trade_note || `${payload?.lead_count || 0} lead(s) currently tracked in this market.`,
      top_industries: payload?.top_industries || [],
      recommended_products: payload?.recommended_products || [],
      existing_leads: payload?.existing_leads ?? payload?.lead_count ?? 0,
    };
  }
  if (name === 'admin.companies.list') return mapItems(payload, adminCompany);
  if (['admin.companies.get', 'admin.companies.create', 'admin.companies.update'].includes(name)) return adminCompany(payload);
  if (name === 'admin.users.list') return mapItems(payload, adminUser);
  if (['admin.users.get', 'admin.users.create', 'admin.users.update'].includes(name)) return adminUser(payload);
  if (name === 'leadScans.list') return mapItems(payload, value => ({ ...value, ...value.config, name: value.name || `Scan — ${(value.config?.countries || []).join(', ')}` }));
  if (name === 'leadScans.create' || name === 'leadScans.get') {
    return { ...payload, ...payload?.config, name: payload?.name || body?.name || `Scan — ${(payload?.config?.countries || []).join(', ')}` };
  }
  if (name === 'leads.list' || name === 'leadScans.results') return mapItems(payload, lead);
  if (['leads.create', 'leads.get', 'leads.update'].includes(name)) return lead(payload);
  if (['leads.score', 'leads.scoreRecalculate', 'leads.scoreExplanation'].includes(name)) return score(payload);
  if (name === 'contacts.list') return mapItems(payload, contact);
  if (['contacts.create', 'contacts.get', 'contacts.update'].includes(name)) return contact(payload);
  if (name === 'products.list') return mapItems(payload, product);
  if (['products.create', 'products.get', 'products.update'].includes(name)) return product(payload);
  if (name === 'documents.list') return mapItems(payload, document);
  if (['documents.upload', 'documents.get', 'documents.processingStatus'].includes(name)) return document(payload);
  if (name === 'research.list') return mapItems(payload, research);
  if (name === 'research.get' || name === 'research.leadInsights') return research(payload);
  if (name === 'campaigns.list') return mapItems(payload, campaign);
  if (['campaigns.create', 'campaigns.get', 'campaigns.update'].includes(name)) return campaign(payload);
  if (name === 'messages.list') return mapItems(payload, message);
  if (['messages.get', 'messages.update', 'messages.approve'].includes(name)) return message(payload);
  if (name === 'campaigns.generateMessages' || name === 'research.bulk') return mapItems(payload, run);
  if (name === 'agentRuns.list') return mapItems(payload, run);
  if (['agentRuns.create', 'agentRuns.get', 'agentRuns.start', 'agentRuns.cancel', 'agentRuns.retry',
       'leadScans.start', 'leadScans.cancel', 'leadScans.retry', 'leads.research', 'leads.findContacts',
       'leads.generateOutreach', 'research.company', 'research.lead', 'research.regenerateInsights',
       'contacts.discover', 'messages.regenerate', 'documents.process', 'brain.build', 'brain.rebuild',
       'linkedin.findProfile', 'linkedin.generateNote'].includes(name)) return run(payload);
  if (name === 'agentRuns.logs' || name === 'agentRuns.events') {
    return mapItems(payload, value => ({ ...value, t: value.t || value.ts, line: value.line || value.message || value.kind, cls: value.cls || (value.kind === 'failed' ? 'error' : value.kind === 'succeeded' ? 'ok' : '') }));
  }
  if (name === 'activity.list' || name === 'activity.forLead' || name === 'activity.forContact' || name === 'activity.forCampaign') {
    return mapItems(payload, activity);
  }
  if (name === 'emailIntegrations.list' || name === 'whatsapp.integrations') {
    return mapItems(payload, integration);
  }
  if (['emailIntegrations.get', 'emailIntegrations.update', 'whatsapp.profile',
       'whatsapp.saveProfile', 'whatsapp.getIntegration', 'whatsapp.updateIntegration'].includes(name)) {
    return integration(payload);
  }
  if (name === 'analytics.pipeline') {
    const stages = payload?.leads_by_status || payload?.stages || [];
    return {
      ...payload,
      leads_by_status: stages,
      emails_sent_weekly: weeklySeries(payload?.emails_sent_weekly),
      replies_weekly: weeklySeries(payload?.replies_weekly),
      funnel: payload?.funnel || [
        { stage: 'Leads discovered', value: payload?.total || 0 },
        { stage: 'Researched', value: 0 },
        { stage: 'Contacts found', value: 0 },
        { stage: 'Emails sent', value: 0 },
        { stage: 'Replies', value: 0 },
        { stage: 'Interested', value: 0 },
      ],
    };
  }
  if (name === 'analytics.market') {
    const markets = payload?.markets || [];
    return {
      ...payload,
      country_scores: payload?.country_scores || markets.map(item => ({
        country: item.country,
        score: item.opportunity_score || 0,
      })),
      top_industries: payload?.top_industries || [],
      source_performance: payload?.source_performance || [],
      product_market_fit: payload?.product_market_fit || [],
    };
  }
  if (name === 'brain.get' || name === 'brain.update' || name === 'brain.approve') return brain(payload);
  if (name === 'brain.snapshots') {
    return mapItems(payload, value => ({
      ...value,
      note: value.note || value.sources?.[0] || '',
      approved: value.status === 'approved',
    }));
  }

  return payload;
}
