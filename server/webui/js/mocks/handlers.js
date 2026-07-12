/* Mock handlers — one per declared route in api.js.
   Handlers mutate the in-memory db so demo flows work end-to-end.
   When the real backend lands these are simply bypassed
   (config.useMocks = false). */

import { ApiError } from '../api.js';
import {
  db, id, log, emit, startRun, cancelRun, startLeadScanRun, generateLeadsForCountry,
} from './db.js';
import { COUNTRY_NAMES } from './seed.js';

const ok = (extra = {}) => ({ ok: true, ...extra });
const listOf = (items) => ({ items, total: items.length });
const notFound = (what) => { throw new ApiError(`${what} not found`, 404); };

function findOr404(coll, key, what) {
  const item = coll.find(x => x.id === key);
  if (!item) notFound(what);
  return item;
}

function filterLeads(query) {
  let items = db.leads.filter(l => l.status !== 'archived' || query.status === 'archived');
  if (query.country) items = items.filter(l => l.country === query.country);
  if (query.status) items = items.filter(l => l.status === query.status);
  if (query.industry) items = items.filter(l => l.industry === query.industry);
  if (query.scan) items = items.filter(l => l.scan_id === query.scan);
  if (query.band) items = items.filter(l => l.score.band === query.band);
  if (query.q) {
    const q = query.q.toLowerCase();
    items = items.filter(l => l.company_name.toLowerCase().includes(q) || l.city.toLowerCase().includes(q));
  }
  return items;
}

/* --- email generation template (mock "LLM") --- */
function generateEmail({ lead, contact, product, language = 'en' }) {
  const firstName = contact?.name ? contact.name.split(' ')[0] : null;
  const prodName = product ? product.name : 'our built-in appliance range';
  if (language === 'de') {
    return {
      subject: `CE-zertifizierte Einbaugeräte — Partnerschaft mit ${lead.company_name}`,
      body: `Guten Tag${contact ? ` ${contact.name}` : ''},\n\nbei der Recherche im deutschen Fachhandel ist mir ${lead.company_name} in ${lead.city} aufgefallen — Ihr Profil als ${lead.industry} passt sehr gut zu unserem Exportprogramm.\n\nSilverine fertigt seit 2004 Küchengeräte in Istanbul: CE- und TSE-zertifiziert, A+ Energieklassen, mit 3 Wochen Lieferzeit in die EU. Unsere Partner erzielen 25–35% niedrigere Einkaufspreise gegenüber westeuropäischen Marken bei vergleichbarer Spezifikation.\n\nBesonders relevant für Sie: ${prodName} — auch als White-Label ab 50 Stück MOQ.\n\nHätten Sie kommende Woche 20 Minuten für ein kurzes Gespräch?\n\nMit freundlichen Grüßen\nMeltem Aydın\nExport Sales — Silverine`,
    };
  }
  return {
    subject: `CE-certified kitchen appliances for ${lead.company_name}`,
    body: `Dear ${firstName || 'Sir or Madam'},\n\nI came across ${lead.company_name} while researching ${lead.industry.toLowerCase()}s in ${COUNTRY_NAMES[lead.country] || lead.country} — your position in ${lead.city} stands out.\n\nSilverine has manufactured kitchen appliances in Istanbul since 2004: CE and ISO 9001 certified, with a 25–35% landed-cost advantage against Western European brands at comparable spec, and 3-week delivery to EU main ports (2 weeks to Jebel Ali).\n\nMost relevant for your assortment: ${prodName}, available as OEM/white-label from 50 units MOQ, backed by a 24-month warranty with regional service partners.\n\nWould you be open to a brief 20-minute call this week? I can also share references and our FOB price list.\n\nBest regards,\nMeltem Aydın\nExport Sales — Silverine`,
  };
}

function ccEmailsForRule(ruleId) {
  const rule = db.ccRules.find(r => r.id === ruleId) || db.ccRules.find(r => r.is_default);
  return rule ? rule.cc_emails.slice() : [];
}

/* ============================================================ */
export const handlers = {

  /* ---------- auth ---------- */
  'auth.login': ({ body }) => {
    // Mock mode accepts any credentials.
    const token = id('tok');
    return { token, user: db.user, company: { id: db.company.id, name: db.company.name } };
  },
  'auth.logout': () => ok(),
  'auth.me': () => ({ user: db.user, company: { id: db.company.id, name: db.company.name } }),
  'auth.refresh': () => ({ token: id('tok') }),
  'auth.passwordResetRequest': () => ok({ message: 'Reset email sent (mock)' }),
  'auth.passwordResetConfirm': () => ok(),

  /* ---------- admin ---------- */
  'admin.companies.list': () => listOf(db.admin.companies),
  'admin.companies.create': ({ body }) => {
    const company = {
      id: id('company'),
      name: body?.name || 'New customer',
      legal_name: body?.legal_name || body?.name || 'New customer',
      website: body?.website || '',
      status: 'access_pending',
      plan: body?.plan || 'trial',
      users: 0,
      created_at: new Date().toISOString(),
      last_seen_at: null,
    };
    db.admin.companies.unshift(company);
    emit('admin', company);
    return company;
  },
  'admin.companies.get': ({ params }) => findOr404(db.admin.companies, params.companyId, 'Company'),
  'admin.companies.update': ({ params, body }) => {
    const company = findOr404(db.admin.companies, params.companyId, 'Company');
    Object.assign(company, body || {});
    if (company.id === db.company.id) Object.assign(db.company, body || {});
    emit('admin', company);
    return company;
  },
  'admin.companies.delete': ({ params }) => {
    const i = db.admin.companies.findIndex(c => c.id === params.companyId);
    if (i < 0) notFound('Company');
    db.admin.companies.splice(i, 1);
    emit('admin', null);
    return ok();
  },
  'admin.companies.activate': ({ params }) => adminCompanyStatus(params.companyId, 'active'),
  'admin.companies.disable': ({ params }) => adminCompanyStatus(params.companyId, 'disabled'),
  'admin.companies.suspend': ({ params }) => adminCompanyStatus(params.companyId, 'suspended'),

  'admin.users.list': () => listOf(db.admin.users),
  'admin.users.create': ({ body }) => {
    const user = {
      id: id('user'),
      name: body?.name || 'New user',
      email: body?.email || 'user@example.com',
      role: body?.role || 'owner',
      company_id: body?.company_id || null,
      status: 'active',
      created_at: new Date().toISOString(),
      last_login_at: null,
    };
    db.admin.users.unshift(user);
    emit('admin', user);
    return user;
  },
  'admin.users.get': ({ params }) => findOr404(db.admin.users, params.userId, 'User'),
  'admin.users.update': ({ params, body }) => {
    const user = findOr404(db.admin.users, params.userId, 'User');
    Object.assign(user, body || {});
    if (user.id === db.user.id) Object.assign(db.user, body || {});
    emit('admin', user);
    return user;
  },
  'admin.users.delete': ({ params }) => {
    const i = db.admin.users.findIndex(u => u.id === params.userId);
    if (i < 0) notFound('User');
    db.admin.users.splice(i, 1);
    emit('admin', null);
    return ok();
  },
  'admin.users.assignCompany': ({ params, body }) => {
    const user = findOr404(db.admin.users, params.userId, 'User');
    user.company_id = body?.company_id || null;
    emit('admin', user);
    return user;
  },
  'admin.users.resetPassword': ({ params }) => {
    const user = findOr404(db.admin.users, params.userId, 'User');
    log('user', `Admin reset password for ${user.email}`, { user_id: user.id });
    return ok({ message: 'Password reset email queued (mock).' });
  },
  'admin.users.disable': ({ params }) => {
    const user = findOr404(db.admin.users, params.userId, 'User');
    user.status = 'disabled';
    emit('admin', user);
    return user;
  },
  'admin.errors': () => listOf(db.admin.errors),
  'admin.logs': ({ query }) => listOf(db.admin.logs.slice(0, query.limit ? Number(query.limit) : 100)),

  /* ---------- company ---------- */
  'company.getProfile': () => db.company,
  'company.updateProfile': ({ body }) => { Object.assign(db.company, body || {}); return db.company; },
  'company.getPositioning': () => db.company.positioning,
  'company.updatePositioning': ({ body }) => { Object.assign(db.company.positioning, body || {}); return db.company.positioning; },
  'company.getSalesPreferences': () => db.company.sales_preferences,
  'company.updateSalesPreferences': ({ body }) => { Object.assign(db.company.sales_preferences, body || {}); return db.company.sales_preferences; },

  /* ---------- onboarding ---------- */
  'onboarding.status': () => db.onboarding,
  'onboarding.start': () => { db.onboarding.status = 'in_progress'; return db.onboarding; },
  'onboarding.updateCompanyIdentity': ({ body }) => markStep('company-identity', body),
  'onboarding.updatePositioning': ({ body }) => markStep('positioning', body),
  'onboarding.updateProducts': ({ body }) => markStep('products', body),
  'onboarding.updateInternalSalesData': ({ body }) => markStep('internal-sales-data', body),
  'onboarding.updateCurrentContacts': ({ body }) => markStep('current-contacts', body),
  'onboarding.updateTargetMarkets': ({ body }) => markStep('target-markets', body),
  'onboarding.updateIntegrations': ({ body }) => markStep('integrations', body),
  'onboarding.reviewBrain': ({ body }) => markStep('brain-review', body),
  'onboarding.complete': () => {
    db.onboarding.status = 'complete';
    db.onboarding.steps.forEach(s => { s.status = 'done'; });
    db.onboarding.current_step = db.onboarding.steps.length;
    log('user', 'Onboarding completed');
    emit('onboarding', db.onboarding);
    return db.onboarding;
  },

  /* ---------- documents ---------- */
  'documents.list': () => listOf(db.documents),
  'documents.upload': ({ body }) => {
    const multipart = typeof FormData !== 'undefined' && body instanceof FormData;
    const file = multipart ? body.get('file') : null;
    const doc = {
      id: id('doc'),
      name: file?.name || body?.name || 'upload.pdf',
      type: (multipart ? body.get('document_type') : body?.type) || 'other',
      size_kb: file?.size ? Math.ceil(file.size / 1024) : body?.size_kb || Math.floor(200 + Math.random() * 4000),
      status: 'uploaded',
      uploaded_at: new Date().toISOString(),
    };
    db.documents.unshift(doc);
    log('document', `Uploaded ${doc.name}`, { document_id: doc.id });
    return doc;
  },
  'documents.get': ({ params }) => findOr404(db.documents, params.documentId, 'Document'),
  'documents.delete': ({ params }) => {
    const i = db.documents.findIndex(d => d.id === params.documentId);
    if (i < 0) notFound('Document');
    db.documents.splice(i, 1);
    return ok();
  },
  'documents.process': ({ params }) => {
    const doc = findOr404(db.documents, params.documentId, 'Document');
    doc.status = 'processing';
    const run = startRun({
      type: 'document_processing',
      label: `Process ${doc.name}`,
      related: { document_id: doc.id },
      script: [
        [800, (r) => { r.progress = 20; r.log(`Parsing ${doc.name}…`); }],
        [1600, (r) => { r.progress = 55; r.log('Extracting structured data (products, prices, contacts)…'); }],
        [1600, (r) => { r.progress = 85; r.log('Merging into Company Brain knowledge base…'); }],
        [900, (r) => {
          doc.status = 'processed';
          r.log(`${doc.name} processed ✓`, 'ok');
          log('agent', `Document processed: ${doc.name}`, { document_id: doc.id });
        }],
      ],
    });
    return { document: doc, run_id: run.id };
  },
  'documents.processingStatus': ({ params }) => {
    const doc = findOr404(db.documents, params.documentId, 'Document');
    return { status: doc.status };
  },

  /* ---------- products ---------- */
  'products.list': () => listOf(db.products),
  'products.create': ({ body }) => {
    const prod = { id: id('prod'), certifications: [], buyer_roles: [], market_fit: [], ...body };
    db.products.push(prod);
    return prod;
  },
  'products.get': ({ params }) => findOr404(db.products, params.productId, 'Product'),
  'products.update': ({ params, body }) => Object.assign(findOr404(db.products, params.productId, 'Product'), body || {}),
  'products.delete': ({ params }) => {
    const i = db.products.findIndex(p => p.id === params.productId);
    if (i < 0) notFound('Product');
    db.products.splice(i, 1);
    return ok();
  },
  'products.extractFromDocuments': () => ok({ message: 'Product extraction queued (mock)', extracted: 0 }),
  'products.generateBuyerRoles': ({ params }) => {
    const p = findOr404(db.products, params.productId, 'Product');
    p.buyer_roles = ['Purchasing Manager', 'Category Manager', 'Head of Imports'];
    return p;
  },
  'products.generateMarketFit': ({ params }) => findOr404(db.products, params.productId, 'Product'),

  /* ---------- company brain ---------- */
  'brain.get': () => db.brain,
  'brain.build': () => brainRebuild('Company Brain — build'),
  'brain.rebuild': () => brainRebuild('Company Brain — rebuild'),
  'brain.update': ({ body }) => { Object.assign(db.brain.sections, body || {}); return db.brain; },
  'brain.approve': () => {
    db.brain.status = 'approved';
    db.brain.approved_at = new Date().toISOString();
    const snap = db.brain.snapshots[0];
    if (snap) snap.approved = true;
    log('user', 'Company Brain approved');
    emit('brain', db.brain);
    return db.brain;
  },
  'brain.snapshots': () => listOf(db.brain.snapshots),
  'brain.snapshot': ({ params }) => findOr404(db.brain.snapshots, params.snapshotId, 'Snapshot'),

  /* ---------- lead map ---------- */
  'leadMap.countries': () => listOf(db.leadMap.countries),
  'leadMap.country': ({ params }) => {
    const c = db.leadMap.countries.find(c => c.code === params.countryCode.toUpperCase());
    if (!c) notFound('Country');
    return c;
  },
  'leadMap.countrySummary': ({ params }) => {
    const code = params.countryCode.toUpperCase();
    const country = db.leadMap.countries.find(c => c.code === code);
    if (!country) notFound('Country');
    const summary = db.leadMap.summaries[code] || db.leadMap.summaries._default;
    const products = db.products
      .map(p => ({ id: p.id, name: p.name, fit: (p.market_fit.find(f => f.country === code) || {}).score }))
      .filter(p => p.fit)
      .sort((a, b) => b.fit - a.fit);
    return {
      ...country,
      ...summary,
      recommended_products: products,
      existing_leads: db.leads.filter(l => l.country === code).length,
      selected: db.leadMap.selected.includes(code),
    };
  },
  'leadMap.selectedCountries': () => ({ items: db.leadMap.selected, max: db.leadMap.max_selected }),
  'leadMap.selectCountry': ({ body }) => {
    const code = (body?.country_code || '').toUpperCase();
    if (!code) throw new ApiError('country_code required', 400);
    if (db.leadMap.selected.includes(code)) return { items: db.leadMap.selected, max: db.leadMap.max_selected };
    if (db.leadMap.selected.length >= db.leadMap.max_selected) {
      throw new ApiError(`Maximum ${db.leadMap.max_selected} target markets per scan. Remove one first.`, 422, 'max_countries');
    }
    db.leadMap.selected.push(code);
    emit('leadMap', db.leadMap.selected);
    return { items: db.leadMap.selected, max: db.leadMap.max_selected };
  },
  'leadMap.deselectCountry': ({ params }) => {
    const code = params.countryCode.toUpperCase();
    db.leadMap.selected = db.leadMap.selected.filter(c => c !== code);
    emit('leadMap', db.leadMap.selected);
    return { items: db.leadMap.selected, max: db.leadMap.max_selected };
  },

  /* ---------- lead scans ---------- */
  'leadScans.list': () => listOf(db.leadScans),
  'leadScans.create': ({ body }) => {
    const countries = (body?.countries || []).map(c => c.toUpperCase());
    if (!countries.length) throw new ApiError('At least one country required', 400);
    if (countries.length > db.leadMap.max_selected) throw new ApiError(`Maximum ${db.leadMap.max_selected} countries per scan`, 422);
    const scan = {
      id: id('scan'),
      name: body?.name || `Scan — ${countries.map(c => COUNTRY_NAMES[c] || c).join(', ')}`,
      countries,
      depth: body?.depth || 'standard',
      sources: body?.sources || ['web_search'],
      products: body?.products || [],
      industries: body?.industries || [],
      leads_per_country: Math.max(3, Math.min(20, Number(body?.leads_per_country) || 8)),
      status: 'created',
      leads_found: 0,
      run_id: null,
      created_at: new Date().toISOString(),
      completed_at: null,
    };
    db.leadScans.unshift(scan);
    return scan;
  },
  'leadScans.get': ({ params }) => findOr404(db.leadScans, params.scanId, 'Scan'),
  'leadScans.start': ({ params }) => {
    const scan = findOr404(db.leadScans, params.scanId, 'Scan');
    if (scan.status === 'running') return scan;
    const run = startLeadScanRun(scan);
    log('agent', `Lead scan started — ${scan.countries.map(c => COUNTRY_NAMES[c] || c).join(', ')} (${scan.depth} depth)`, { scan_id: scan.id, run_id: run.id });
    return { scan, run_id: run.id };
  },
  'leadScans.cancel': ({ params }) => {
    const scan = findOr404(db.leadScans, params.scanId, 'Scan');
    scan.status = 'cancelled';
    if (scan.run_id) cancelRun(scan.run_id);
    emit('scans', scan);
    return scan;
  },
  'leadScans.retry': ({ params }) => {
    const scan = findOr404(db.leadScans, params.scanId, 'Scan');
    const run = startLeadScanRun(scan);
    return { scan, run_id: run.id };
  },
  'leadScans.results': ({ params }) => listOf(db.leads.filter(l => l.scan_id === params.scanId)),

  /* ---------- leads ---------- */
  'leads.list': ({ query }) => listOf(filterLeads(query)),
  'leads.create': ({ body }) => {
    const score = Math.round(45 + Math.random() * 40);
    const lead = {
      id: id('lead'),
      company_name: body?.company_name || 'Unnamed company',
      country: (body?.country || 'DE').toUpperCase(),
      city: body?.city || '',
      industry: body?.industry || 'Appliance distributor',
      website: body?.website || '',
      size_hint: body?.size_hint || null,
      source: 'manual',
      status: 'new',
      scan_id: null,
      created_at: new Date().toISOString(),
      score: {
        value: score,
        band: score >= 75 ? 'high' : score >= 50 ? 'mid' : 'low',
        factors: [
          { label: 'Industry fit', weight: 30, note: 'Manually created — assumed fit' },
          { label: 'Market opportunity', weight: 25, note: `${COUNTRY_NAMES[body?.country?.toUpperCase()] || body?.country} market score` },
          { label: 'Company size', weight: 20, note: 'Unverified' },
          { label: 'Web signals', weight: 25, note: 'Pending research' },
        ],
      },
    };
    db.leads.unshift(lead);
    log('user', `Custom lead created: ${lead.company_name}`, { lead_id: lead.id });
    emit('leads', [lead]);
    return lead;
  },
  'leads.get': ({ params }) => findOr404(db.leads, params.leadId, 'Lead'),
  'leads.update': ({ params, body }) => {
    const lead = findOr404(db.leads, params.leadId, 'Lead');
    Object.assign(lead, body || {});
    emit('leads', [lead]);
    return lead;
  },
  'leads.delete': ({ params }) => {
    const i = db.leads.findIndex(l => l.id === params.leadId);
    if (i < 0) notFound('Lead');
    db.leads.splice(i, 1);
    emit('leads', []);
    return ok();
  },
  'leads.research': ({ params }) => {
    const lead = findOr404(db.leads, params.leadId, 'Lead');
    const run = startRun({
      type: 'lead_research',
      label: `Research ${lead.company_name}`,
      related: { lead_id: lead.id },
      script: [
        [900, (r) => { r.progress = 18; r.log(`Crawling ${lead.website || 'company web presence'}…`); }],
        [1800, (r) => { r.progress = 45; r.log('Cross-referencing trade data and import records…'); }],
        [1800, (r) => { r.progress = 72; r.log('Analyzing brand portfolio and buying signals…'); }],
        [1400, (r) => {
          const existing = db.research.find(x => x.lead_id === lead.id);
          const entry = {
            id: existing ? existing.id : id('res'),
            lead_id: lead.id,
            status: 'completed',
            created_at: new Date().toISOString(),
            summary: `${lead.company_name} is a ${lead.industry.toLowerCase()} in ${lead.city || COUNTRY_NAMES[lead.country]}, estimated ${lead.size_hint || '50-100'} employees. The company distributes mid-range appliance brands and shows active procurement signals; no Turkish manufacturer is currently in its portfolio.`,
            insights: [
              { title: 'Portfolio gap', body: 'No Turkish supplier in current line-up — a 25–35% landed-cost pitch is credible.' },
              { title: 'Decision maker', body: 'Purchasing decisions appear centralized; target the Purchasing Manager or Head of Imports.' },
              { title: 'Entry product', body: `Recommended door-opener: ${db.products[0].name} based on assortment overlap.` },
            ],
          };
          if (existing) Object.assign(existing, entry); else db.research.push(entry);
          if (lead.status === 'new') lead.status = 'researched';
          emit('leads', [lead]);
          r.log(`Research complete for ${lead.company_name} ✓`, 'ok');
          log('agent', `Research completed: ${lead.company_name}`, { lead_id: lead.id });
        }],
      ],
    });
    return { run_id: run.id };
  },
  'leads.findContacts': ({ params }) => {
    const lead = findOr404(db.leads, params.leadId, 'Lead');
    const run = startRun({
      type: 'contact_discovery',
      label: `Find contacts — ${lead.company_name}`,
      related: { lead_id: lead.id },
      script: [
        [900, (r) => { r.progress = 25; r.log('Scanning public team pages and registries…'); }],
        [1700, (r) => { r.progress = 60; r.log('Matching buyer roles from Company Brain (Purchasing, Imports)…'); }],
        [1500, (r) => {
          const domain = (lead.website || 'https://example.com').replace(/^https?:\/\//, '').replace(/\/.*/, '');
          const names = [['Alex Verhoeven', 'Purchasing Manager'], ['Sam Carter', 'Head of Imports']];
          const created = names.slice(0, 1 + Math.floor(Math.random() * 2)).map(([name, title], i) => ({
            id: id('contact'),
            lead_id: lead.id,
            name, title,
            email: `${name.split(' ')[0].toLowerCase()}@${domain}`,
            email_status: i === 0 ? 'verified' : 'unverified',
            linkedin_url: `https://www.linkedin.com/in/${name.toLowerCase().replace(/ /g, '-')}`,
            phone: null,
            do_not_contact: false,
            created_at: new Date().toISOString(),
          }));
          db.contacts.push(...created);
          emit('contacts', created);
          r.log(`${created.length} contact(s) found at ${lead.company_name} ✓`, 'ok');
          log('agent', `Contact discovery: ${created.length} contact(s) at ${lead.company_name}`, { lead_id: lead.id });
        }],
      ],
    });
    return { run_id: run.id };
  },
  'leads.generateOutreach': ({ params, body }) => {
    const lead = findOr404(db.leads, params.leadId, 'Lead');
    const contact = db.contacts.find(c => c.lead_id === lead.id && !c.do_not_contact);
    const product = db.products.find(p => p.id === body?.product_id) || db.products[0];
    const language = body?.language || (lead.country === 'DE' ? 'de' : 'en');
    const email = generateEmail({ lead, contact, product, language });
    const msg = {
      id: id('msg'),
      campaign_id: null,
      lead_id: lead.id,
      contact_id: contact ? contact.id : null,
      channel: 'email',
      language,
      ...email,
      status: 'draft_generated',
      cc: ccEmailsForRule(body?.cc_rule_id),
      sent_at: null,
      created_at: new Date().toISOString(),
    };
    db.messages.unshift(msg);
    emit('messages', msg);
    log('agent', `Outreach email generated for ${lead.company_name}`, { lead_id: lead.id, message_id: msg.id });
    return msg;
  },
  'leads.markDoNotContact': ({ params }) => {
    const lead = findOr404(db.leads, params.leadId, 'Lead');
    lead.status = 'do_not_contact';
    emit('leads', [lead]);
    return lead;
  },
  'leads.archive': ({ params }) => {
    const lead = findOr404(db.leads, params.leadId, 'Lead');
    lead.status = 'archived';
    emit('leads', [lead]);
    return lead;
  },

  /* ---------- lead scoring ---------- */
  'leads.score': ({ params }) => findOr404(db.leads, params.leadId, 'Lead').score,
  'leads.scoreRecalculate': ({ params }) => {
    const lead = findOr404(db.leads, params.leadId, 'Lead');
    const v = Math.min(98, Math.max(20, lead.score.value + Math.round((Math.random() - 0.4) * 12)));
    lead.score.value = v;
    lead.score.band = v >= 75 ? 'high' : v >= 50 ? 'mid' : 'low';
    emit('leads', [lead]);
    return lead.score;
  },
  'leads.scoreExplanation': ({ params }) => {
    const lead = findOr404(db.leads, params.leadId, 'Lead');
    return { value: lead.score.value, band: lead.score.band, factors: lead.score.factors };
  },

  /* ---------- research ---------- */
  'research.list': () => listOf(db.research),
  'research.get': ({ params }) => findOr404(db.research, params.researchId, 'Research'),
  'research.company': () => ok({ message: 'Company research queued (mock)' }),
  'research.lead': ({ params }) => handlers['leads.research']({ params: { leadId: params.leadId } }),
  'research.bulk': ({ body }) => ok({ queued: (body?.lead_ids || []).length }),
  'research.leadInsights': ({ params }) => {
    const entry = db.research.find(r => r.lead_id === params.leadId);
    return entry || { lead_id: params.leadId, status: 'none', summary: null, insights: [] };
  },
  'research.regenerateInsights': ({ params }) => handlers['leads.research']({ params: { leadId: params.leadId } }),

  /* ---------- contacts ---------- */
  'contacts.list': ({ query }) => {
    let items = db.contacts;
    if (query.lead_id) items = items.filter(c => c.lead_id === query.lead_id);
    if (query.email_status) items = items.filter(c => c.email_status === query.email_status);
    if (query.q) {
      const q = query.q.toLowerCase();
      items = items.filter(c => c.name.toLowerCase().includes(q) || (c.email || '').toLowerCase().includes(q));
    }
    return listOf(items);
  },
  'contacts.create': ({ body }) => {
    const contact = {
      id: id('contact'),
      lead_id: body?.lead_id || null,
      name: body?.full_name || body?.name || 'Unnamed contact',
      title: body?.title || '',
      email: body?.email || '',
      email_status: body?.email ? 'unverified' : 'not_found',
      linkedin_url: body?.linkedin_url || null,
      phone: body?.phone || null,
      do_not_contact: false,
      created_at: new Date().toISOString(),
    };
    db.contacts.unshift(contact);
    emit('contacts', [contact]);
    return contact;
  },
  'contacts.get': ({ params }) => findOr404(db.contacts, params.contactId, 'Contact'),
  'contacts.update': ({ params, body }) => Object.assign(findOr404(db.contacts, params.contactId, 'Contact'), body || {}),
  'contacts.delete': ({ params }) => {
    const i = db.contacts.findIndex(c => c.id === params.contactId);
    if (i < 0) notFound('Contact');
    db.contacts.splice(i, 1);
    return ok();
  },
  'contacts.discover': ({ body }) => handlers['leads.findContacts']({ params: { leadId: body?.lead_id } }),
  'contacts.verify': ({ params }) => {
    const c = findOr404(db.contacts, params.contactId, 'Contact');
    c.email_status = Math.random() < 0.85 ? 'verified' : 'not_found';
    emit('contacts', [c]);
    return c;
  },
  'contacts.markDoNotContact': ({ params }) => {
    const c = findOr404(db.contacts, params.contactId, 'Contact');
    c.do_not_contact = true;
    emit('contacts', [c]);
    return c;
  },

  /* ---------- campaigns ---------- */
  'campaigns.list': () => listOf(db.campaigns),
  'campaigns.create': ({ body }) => {
    const camp = {
      id: id('camp'),
      name: body?.name || 'New campaign',
      country: body?.country || null,
      product_id: body?.product_id || null,
      language: body?.language || 'en',
      send_mode: body?.send_mode || 'create_draft',
      cc_rule_id: body?.cc_rule_id || 'ccrule_default',
      status: 'draft',
      created_at: new Date().toISOString(),
      stats: { messages: 0, sent: 0, replied: 0, interested: 0 },
    };
    db.campaigns.unshift(camp);
    return camp;
  },
  'campaigns.get': ({ params }) => findOr404(db.campaigns, params.campaignId, 'Campaign'),
  'campaigns.update': ({ params, body }) => Object.assign(findOr404(db.campaigns, params.campaignId, 'Campaign'), body || {}),
  'campaigns.delete': ({ params }) => {
    const i = db.campaigns.findIndex(c => c.id === params.campaignId);
    if (i < 0) notFound('Campaign');
    db.campaigns.splice(i, 1);
    return ok();
  },
  'campaigns.generateMessages': ({ params }) => {
    const camp = findOr404(db.campaigns, params.campaignId, 'Campaign');
    const pool = db.leads.filter(l => (!camp.country || l.country === camp.country) && !db.messages.some(m => m.lead_id === l.id && m.campaign_id === camp.id)).slice(0, 5);
    const product = db.products.find(p => p.id === camp.product_id);
    const created = pool.map(lead => {
      const contact = db.contacts.find(c => c.lead_id === lead.id);
      const email = generateEmail({ lead, contact, product, language: camp.language });
      const msg = {
        id: id('msg'), campaign_id: camp.id, lead_id: lead.id, contact_id: contact ? contact.id : null,
        channel: 'email', language: camp.language, ...email,
        status: 'draft_generated', cc: ccEmailsForRule(camp.cc_rule_id), sent_at: null,
        created_at: new Date().toISOString(),
      };
      db.messages.unshift(msg);
      return msg;
    });
    camp.stats.messages += created.length;
    camp.status = created.length ? 'awaiting_approval' : camp.status;
    emit('messages', created);
    log('agent', `Generated ${created.length} messages for campaign "${camp.name}"`, { campaign_id: camp.id });
    return listOf(created);
  },
  'campaigns.approve': ({ params }) => {
    const camp = findOr404(db.campaigns, params.campaignId, 'Campaign');
    db.messages.filter(m => m.campaign_id === camp.id && m.status === 'draft_generated').forEach(m => { m.status = 'approved'; });
    camp.status = 'approved';
    emit('messages', null);
    log('user', `Campaign approved: ${camp.name}`, { campaign_id: camp.id });
    return camp;
  },
  'campaigns.send': ({ params }) => {
    const camp = findOr404(db.campaigns, params.campaignId, 'Campaign');
    const queue = db.messages.filter(m => m.campaign_id === camp.id && ['approved', 'draft_generated'].includes(m.status));
    const asDrafts = camp.send_mode === 'create_draft';
    camp.status = 'sending';
    const run = startRun({
      type: 'email_send',
      label: `${asDrafts ? 'Create drafts' : 'Send emails'} — ${camp.name} (${queue.length})`,
      related: { campaign_id: camp.id },
      script: [
        [700, (r) => { r.progress = 15; r.log(`Connected mailbox: ${db.company.sales_preferences.connected_mailbox} (Google Workspace)`); }],
        ...queue.map((m, i) => [900, (r) => {
          m.status = asDrafts ? 'draft_created' : 'sent';
          m.sent_at = new Date().toISOString();
          const lead = db.leads.find(l => l.id === m.lead_id);
          if (lead && ['new', 'researched'].includes(lead.status)) lead.status = 'contacted';
          if (!asDrafts) camp.stats.sent += 1;
          r.progress = 15 + Math.round(((i + 1) / queue.length) * 78);
          r.log(`${asDrafts ? 'Draft created' : 'Sent'} → ${m.subject.slice(0, 56)}… ${m.cc.length ? `(CC: ${m.cc.join(', ')})` : ''}`, 'ok');
          emit('messages', m);
        }]),
        [700, (r) => {
          camp.status = asDrafts ? 'drafts_created' : 'completed';
          emit('campaigns', camp);
          r.log(`Campaign ${asDrafts ? 'drafts ready in mailbox' : 'sent'} ✓`, 'ok');
          log('agent', `${asDrafts ? 'Drafts created' : 'Emails sent'} for campaign "${camp.name}"`, { campaign_id: camp.id });
        }],
      ],
    });
    return { campaign: camp, run_id: run.id };
  },
  'campaigns.pause': ({ params }) => { const c = findOr404(db.campaigns, params.campaignId, 'Campaign'); c.status = 'paused'; return c; },
  'campaigns.cancel': ({ params }) => { const c = findOr404(db.campaigns, params.campaignId, 'Campaign'); c.status = 'cancelled'; return c; },

  /* ---------- messages ---------- */
  'messages.list': ({ query }) => {
    let items = db.messages;
    if (query.campaign_id) items = items.filter(m => m.campaign_id === query.campaign_id);
    if (query.lead_id) items = items.filter(m => m.lead_id === query.lead_id);
    if (query.status) items = items.filter(m => m.status === query.status);
    return listOf(items);
  },
  'messages.get': ({ params }) => findOr404(db.messages, params.messageId, 'Message'),
  'messages.update': ({ params, body }) => {
    const m = findOr404(db.messages, params.messageId, 'Message');
    Object.assign(m, body || {});
    emit('messages', m);
    return m;
  },
  'messages.regenerate': ({ params }) => {
    const m = findOr404(db.messages, params.messageId, 'Message');
    const lead = db.leads.find(l => l.id === m.lead_id);
    const contact = db.contacts.find(c => c.id === m.contact_id);
    if (lead) {
      const email = generateEmail({ lead, contact, product: db.products[Math.floor(Math.random() * db.products.length)], language: m.language });
      m.subject = email.subject;
      m.body = email.body;
      m.status = 'draft_generated';
    }
    emit('messages', m);
    return m;
  },
  'messages.approve': ({ params }) => {
    const m = findOr404(db.messages, params.messageId, 'Message');
    m.status = 'approved';
    emit('messages', m);
    return m;
  },
  'messages.createDraft': ({ params }) => sendMessage(params.messageId, true),
  'messages.send': ({ params }) => sendMessage(params.messageId, false),
  'messages.markSentManually': ({ params }) => {
    const m = findOr404(db.messages, params.messageId, 'Message');
    m.status = 'sent';
    m.sent_at = new Date().toISOString();
    emit('messages', m);
    return m;
  },
  'messages.markReplied': ({ params }) => {
    const m = findOr404(db.messages, params.messageId, 'Message');
    m.status = 'replied';
    const lead = db.leads.find(l => l.id === m.lead_id);
    if (lead) { lead.status = 'replied'; emit('leads', [lead]); }
    emit('messages', m);
    log('reply', `Reply received${lead ? ` from ${lead.company_name}` : ''}`, { lead_id: m.lead_id, message_id: m.id });
    return m;
  },

  /* ---------- custom outreach ---------- */
  'customOutreach.createLeadAndMessage': ({ body }) => {
    const lead = handlers['leads.create']({ body: body?.lead });
    let contact = null;
    if (body?.contact?.full_name || body?.contact?.email) {
      contact = handlers['contacts.create']({ body: { ...body.contact, lead_id: lead.id } });
    }
    return { lead, contact };
  },
  'customOutreach.generateEmail': ({ body }) => {
    const lead = findOr404(db.leads, body?.lead_id, 'Lead');
    const contact = body?.contact_id ? db.contacts.find(c => c.id === body.contact_id) : db.contacts.find(c => c.lead_id === lead.id);
    const product = db.products.find(p => p.id === body?.product_id) || db.products[0];
    const email = generateEmail({ lead, contact, product, language: body?.language || 'en' });
    const msg = {
      id: id('msg'), campaign_id: null, lead_id: lead.id, contact_id: contact ? contact.id : null,
      channel: 'email', language: body?.language || 'en', ...email,
      status: 'draft_generated', cc: ccEmailsForRule(body?.cc_rule), sent_at: null,
      created_at: new Date().toISOString(),
    };
    db.messages.unshift(msg);
    emit('messages', msg);
    return msg;
  },
  'customOutreach.sendEmail': ({ body }) => sendMessage(body?.message_id, false),
  'customOutreach.createDraft': ({ body }) => sendMessage(body?.message_id, true),

  /* ---------- email integrations ---------- */
  'emailIntegrations.list': () => listOf(db.integrations.email),
  'emailIntegrations.connectGoogle': () => connectEmail('google', 'Google Workspace'),
  'emailIntegrations.connectMicrosoft': () => connectEmail('microsoft', 'Microsoft 365'),
  'emailIntegrations.connectZoho': () => connectEmail('zoho', 'Zoho Mail'),
  'emailIntegrations.connectSmtp': ({ body }) => connectEmail('smtp', `SMTP (${body?.host || 'custom'})`),
  'emailIntegrations.get': ({ params }) => findOr404(db.integrations.email, params.integrationId, 'Integration'),
  'emailIntegrations.update': ({ params, body }) => Object.assign(findOr404(db.integrations.email, params.integrationId, 'Integration'), body || {}),
  'emailIntegrations.delete': ({ params }) => {
    const i = db.integrations.email.findIndex(x => x.id === params.integrationId);
    if (i < 0) notFound('Integration');
    db.integrations.email.splice(i, 1);
    emit('integrations', null);
    return ok();
  },
  'emailIntegrations.test': ({ params }) => {
    const integ = findOr404(db.integrations.email, params.integrationId, 'Integration');
    integ.last_test = { ok: true, at: new Date().toISOString() };
    emit('integrations', integ);
    return { ok: true, message: 'Test email delivered to the connected mailbox (mock).' };
  },
  'emailIntegrations.refreshToken': ({ params }) => {
    findOr404(db.integrations.email, params.integrationId, 'Integration');
    return ok({ refreshed_at: new Date().toISOString() });
  },

  /* ---------- email sending ---------- */
  'email.createDraft': ({ body }) => sendMessage(body?.message_id, true),
  'email.send': ({ body }) => sendMessage(body?.message_id, false),
  'email.sendBulk': ({ body }) => {
    const ids = body?.message_ids || [];
    ids.forEach(mid => { try { sendMessage(mid, body?.mode === 'create_draft'); } catch { /* skip missing */ } });
    return ok({ processed: ids.length });
  },
  'email.sent': () => listOf(db.messages.filter(m => ['sent', 'replied'].includes(m.status))),
  'email.replies': () => listOf(db.messages.filter(m => m.status === 'replied')),
  'email.status': ({ params }) => ({ provider_message_id: params.providerMessageId, status: 'delivered' }),

  /* ---------- cc rules ---------- */
  'ccRules.list': () => listOf(db.ccRules),
  'ccRules.create': ({ body }) => {
    const rule = { id: id('ccrule'), name: 'New rule', market_country: null, market_region: null, product_id: null, industry: null, cc_emails: [], is_default: false, ...body };
    db.ccRules.push(rule);
    return rule;
  },
  'ccRules.get': ({ params }) => findOr404(db.ccRules, params.ruleId, 'CC rule'),
  'ccRules.update': ({ params, body }) => Object.assign(findOr404(db.ccRules, params.ruleId, 'CC rule'), body || {}),
  'ccRules.delete': ({ params }) => {
    const i = db.ccRules.findIndex(r => r.id === params.ruleId);
    if (i < 0) notFound('CC rule');
    if (db.ccRules[i].is_default) throw new ApiError('Cannot delete the default CC rule', 422);
    db.ccRules.splice(i, 1);
    return ok();
  },

  /* ---------- whatsapp ---------- */
  'whatsapp.integrations': () => listOf(db.integrations.whatsapp),
  'whatsapp.profile': () => db.integrations.whatsapp[0] || null,
  'whatsapp.saveProfile': ({ body }) => {
    const profile = db.integrations.whatsapp[0] || {
      id: id('wa'),
      template_status: 'not_configured',
      verification: null,
    };
    Object.assign(profile, {
      business_name: body?.business_name?.trim() || db.company.name,
      whatsapp_business_account_id: body?.whatsapp_business_account_id?.trim() || '',
      phone_number_id: body?.phone_number_id?.trim() || '',
      display_phone_number: body?.display_phone_number?.trim() || '',
      business_country: body?.business_country || 'TR',
      default_language: body?.default_language || 'en',
      profile_state: 'saved',
      credential_state: 'server_required',
      status: 'not_connected',
      profile_saved_at: new Date().toISOString(),
    });
    db.integrations.whatsapp = [profile];
    emit('integrations', profile);
    log('user', 'WhatsApp Business profile saved', {});
    return profile;
  },
  'whatsapp.verifyProfile': () => {
    const profile = db.integrations.whatsapp[0];
    if (!profile) throw new ApiError('Save a WhatsApp Business profile before verifying it', 422);
    profile.verification = {
      status: 'verified',
      checked_at: new Date().toISOString(),
      message: 'Profile identifiers passed the mock readiness check. Server credentials are still required.',
    };
    profile.profile_state = 'verified';
    emit('integrations', profile);
    return profile.verification;
  },
  'whatsapp.connect': ({ body }) => {
    return handlers['whatsapp.saveProfile']({ body });
  },
  'whatsapp.getIntegration': ({ params }) => findOr404(db.integrations.whatsapp, params.integrationId, 'WhatsApp integration'),
  'whatsapp.updateIntegration': ({ params, body }) => Object.assign(findOr404(db.integrations.whatsapp, params.integrationId, 'WhatsApp integration'), body || {}),
  'whatsapp.deleteIntegration': ({ params }) => {
    db.integrations.whatsapp = db.integrations.whatsapp.filter(w => w.id !== params.integrationId);
    emit('integrations', null);
    return ok();
  },
  'whatsapp.testIntegration': () => ok({ message: 'Server credentials are required before a live template test can run.' }),
  'whatsapp.messages': () => listOf(db.messages.filter(m => m.channel === 'whatsapp')),
  'whatsapp.generate': ({ body }) => {
    const lead = findOr404(db.leads, body?.lead_id, 'Lead');
    const msg = {
      id: id('msg'), campaign_id: null, lead_id: lead.id, contact_id: body?.contact_id || null,
      channel: 'whatsapp', language: body?.language || 'en',
      subject: null,
      body: `Hello! This is Meltem from Silverine (Istanbul). We manufacture CE-certified kitchen appliances and currently supply partners across the EU and GCC. I'd love to share our export catalog with ${lead.company_name} — may I send it here?`,
      status: 'draft_generated', cc: [], sent_at: null, created_at: new Date().toISOString(),
    };
    db.messages.unshift(msg);
    emit('messages', msg);
    return msg;
  },
  'whatsapp.approve': ({ params }) => handlers['messages.approve']({ params: { messageId: params.messageId } }),
  'whatsapp.send': ({ params }) => sendMessage(params.messageId, false),
  'whatsapp.status': ({ params }) => ({ message_id: params.messageId, status: 'delivered' }),
  'whatsapp.markReplied': ({ params }) => handlers['messages.markReplied']({ params: { messageId: params.messageId } }),
  'whatsapp.markOptOut': ({ params }) => {
    const m = findOr404(db.messages, params.messageId, 'Message');
    m.status = 'opt_out';
    emit('messages', m);
    return m;
  },

  /* ---------- linkedin ---------- */
  'linkedin.actions': () => listOf(db.integrations.linkedin_actions),
  'linkedin.findProfile': ({ body }) => {
    const contact = body?.contact_id ? db.contacts.find(c => c.id === body.contact_id) : null;
    const action = {
      id: id('li'),
      contact_id: contact ? contact.id : null,
      lead_id: body?.lead_id || (contact ? contact.lead_id : null),
      profile_url: contact?.linkedin_url || 'https://www.linkedin.com/in/example-profile',
      status: 'profile_found',
      note: null,
      created_at: new Date().toISOString(),
    };
    db.integrations.linkedin_actions.unshift(action);
    emit('linkedin', action);
    return action;
  },
  'linkedin.generateNote': ({ body }) => {
    const action = findOr404(db.integrations.linkedin_actions, body?.action_id, 'LinkedIn action');
    action.note = 'Hi! Silverine manufactures CE-certified kitchen appliances in Istanbul with 3-week EU delivery — I think there is a strong sourcing fit and would be glad to connect.';
    action.status = 'note_generated';
    emit('linkedin', action);
    return action;
  },
  'linkedin.markOpened': ({ params }) => liMark(params.actionId, 'opened'),
  'linkedin.markConnectionSent': ({ params }) => liMark(params.actionId, 'connection_sent'),
  'linkedin.markConnected': ({ params }) => liMark(params.actionId, 'connected'),
  'linkedin.markReplied': ({ params }) => liMark(params.actionId, 'replied'),

  /* ---------- agent runs ---------- */
  'agentRuns.list': ({ query }) => {
    let items = db.agentRuns;
    if (query.type) items = items.filter(r => r.type === query.type);
    if (query.status) items = items.filter(r => r.status === query.status);
    return listOf(items);
  },
  'agentRuns.create': ({ body }) => {
    const run = startRun({ type: body?.type || 'analytics_refresh', label: body?.label || 'Manual run', related: body?.related || {}, script: [[1200, (r) => { r.progress = 60; r.log('Working…'); }], [1200, (r) => r.log('Done ✓', 'ok')]] });
    return run;
  },
  'agentRuns.get': ({ params }) => findOr404(db.agentRuns, params.runId, 'Run'),
  'agentRuns.start': ({ params }) => findOr404(db.agentRuns, params.runId, 'Run'),
  'agentRuns.cancel': ({ params }) => cancelRun(params.runId) || notFound('Run'),
  'agentRuns.retry': ({ params }) => {
    const old = findOr404(db.agentRuns, params.runId, 'Run');
    if (old.type === 'lead_scan' && old.related.scan_id) {
      const scan = db.leadScans.find(s => s.id === old.related.scan_id);
      if (scan) return { run_id: startLeadScanRun(scan).id };
    }
    const run = startRun({
      type: old.type, label: old.label, related: old.related,
      script: [
        [900, (r) => { r.progress = 30; r.log('Retrying previous task…'); }],
        [1600, (r) => { r.progress = 75; r.log('Re-processing inputs…'); }],
        [1100, (r) => r.log('Completed ✓', 'ok')],
      ],
    });
    return { run_id: run.id };
  },
  'agentRuns.logs': ({ params }) => ({ items: findOr404(db.agentRuns, params.runId, 'Run').logs }),
  'agentRuns.events': ({ params }) => ({ items: findOr404(db.agentRuns, params.runId, 'Run').logs }),
  'agent.capabilities': () => db.agent,
  'agent.status': () => ({
    adapter: db.agent.adapter,
    status: db.agent.status,
    detail: db.agent.detail,
  }),

  /* ---------- exports (mock: rows returned, page triggers CSV) ---------- */
  'exports.leads': () => exportRows('leads', db.leads.map(l => ({
    company: l.company_name, country: l.country, city: l.city, industry: l.industry,
    website: l.website, status: l.status, score: l.score.value, source: l.source, created_at: l.created_at,
  }))),
  'exports.contacts': () => exportRows('contacts', db.contacts.map(c => {
    const lead = db.leads.find(l => l.id === c.lead_id);
    return { name: c.name, title: c.title, email: c.email, email_status: c.email_status, company: lead ? lead.company_name : '', country: lead ? lead.country : '', linkedin: c.linkedin_url || '', phone: c.phone || '' };
  })),
  'exports.research': () => exportRows('research', db.research.map(r => {
    const lead = db.leads.find(l => l.id === r.lead_id);
    return { company: lead ? lead.company_name : r.lead_id, status: r.status, summary: r.summary, created_at: r.created_at };
  })),
  'exports.outreach': () => exportRows('outreach', db.messages.map(m => {
    const lead = db.leads.find(l => l.id === m.lead_id);
    return { company: lead ? lead.company_name : '', channel: m.channel, subject: m.subject || '', status: m.status, language: m.language, sent_at: m.sent_at || '', cc: m.cc.join('; ') };
  })),
  'exports.analytics': () => exportRows('analytics', db.analytics.market.country_scores.map(c => ({
    country: COUNTRY_NAMES[c.country] || c.country, opportunity_score: c.score,
    leads: db.leads.filter(l => l.country === c.country).length,
  }))),
  'exports.get': ({ params }) => ({ id: params.exportId, status: 'ready' }),
  'exports.download': ({ params }) => ({ id: params.exportId, status: 'ready' }),

  /* ---------- data sources ---------- */
  'dataSources.list': () => listOf([
    { id: 'ds_web', type: 'web_search', label: 'Web directories', status: 'enabled' },
    { id: 'ds_trade', type: 'trade_data', label: 'Trade databases', status: 'enabled' },
    { id: 'ds_exhib', type: 'exhibitor_lists', label: 'Trade fair exhibitors', status: 'enabled' },
    { id: 'ds_registry', type: 'company_registries', label: 'Company registries', status: 'enabled' },
    { id: 'ds_li', type: 'linkedin_reference', label: 'LinkedIn references', status: 'enabled' },
    { id: 'ds_internal', type: 'uploaded_internal_data', label: 'Uploaded internal data', status: 'enabled' },
  ]),
  'dataSources.create': ({ body }) => ({ id: id('ds'), status: 'enabled', ...body }),
  'dataSources.get': () => ({ id: 'ds_web', type: 'web_search', label: 'Web directories', status: 'enabled' }),
  'dataSources.update': ({ body }) => ok(body || {}),
  'dataSources.delete': () => ok(),
  'dataSources.test': () => ok({ latency_ms: 240 }),
  'dataSources.enable': () => ok(),
  'dataSources.disable': () => ok(),

  /* ---------- activity ---------- */
  'activity.list': ({ query }) => listOf(db.activity.slice(0, query.limit ? Number(query.limit) : 50)),
  'activity.get': ({ params }) => findOr404(db.activity, params.activityId, 'Activity'),
  'activity.forLead': ({ params }) => listOf(collectLeadActivity(params.leadId)),
  'activity.forContact': ({ params }) => {
    const contact = db.contacts.find(c => c.id === params.contactId);
    return listOf(contact ? collectLeadActivity(contact.lead_id) : []);
  },
  'activity.forCampaign': ({ params }) => listOf(db.activity.filter(a => a.ref.campaign_id === params.campaignId)),

  /* ---------- dashboard / analytics aggregates ---------- */
  'dashboard.summary': () => {
    const sent = db.messages.filter(m => ['sent', 'replied'].includes(m.status)).length;
    const replied = db.messages.filter(m => m.status === 'replied').length;
    return {
      sales: {
        leads_found: db.leads.length,
        contacts_found: db.contacts.length,
        emails_sent: sent,
        replies: replied,
        interested: db.leads.filter(l => l.status === 'interested').length,
        whatsapp_messages: db.messages.filter(m => m.channel === 'whatsapp').length,
        active_campaigns: db.campaigns.filter(c => !['completed', 'cancelled'].includes(c.status)).length,
      },
      sparks: {
        leads: [2, 5, 9, 14, 14, 20, 25, db.leads.length],
        emails: [0, 0, 4, 13, 18, 18, 18, sent],
      },
      market: {
        best_countries: db.analytics.market.country_scores.slice(0, 5),
        top_industries: db.analytics.market.top_industries.slice(0, 5),
        source_performance: db.analytics.market.source_performance.slice(0, 4),
      },
      recent_activity: db.activity.slice(0, 8),
      recommended_actions: buildRecommendedActions(),
      country_scores: Object.fromEntries(db.analytics.market.country_scores.map(c => [c.country, c.score])),
      selected_countries: db.leadMap.selected,
    };
  },
  'analytics.pipeline': () => db.analytics.pipeline,
  'analytics.market': () => db.analytics.market,
};

/* ---------------- helpers used above ---------------- */
function adminCompanyStatus(companyId, status) {
  const company = findOr404(db.admin.companies, companyId, 'Company');
  company.status = status;
  emit('admin', company);
  log('user', `Admin set ${company.name} to ${status}`, { company_id: company.id });
  return company;
}

function markStep(key) {
  const step = db.onboarding.steps.find(s => s.key === key);
  if (step) step.status = 'done';
  const idx = db.onboarding.steps.findIndex(s => s.status !== 'done');
  db.onboarding.current_step = idx < 0 ? db.onboarding.steps.length : idx;
  emit('onboarding', db.onboarding);
  return db.onboarding;
}

function brainRebuild(label) {
  db.brain.status = 'building';
  emit('brain', db.brain);
  const run = startRun({
    type: 'company_brain_build',
    label,
    related: {},
    script: [
      [900, (r) => { r.progress = 15; r.log('Loading company profile, products and documents…'); }],
      [1800, (r) => { r.progress = 45; r.log('Synthesizing ideal customer profile and buyer roles…'); }],
      [1800, (r) => { r.progress = 75; r.log('Deriving market assumptions and sales arguments…'); }],
      [1200, (r) => {
        db.brain.status = 'ready_for_review';
        db.brain.built_at = new Date().toISOString();
        db.brain.version += 1;
        db.brain.snapshots.unshift({ id: id('snap'), version: db.brain.version, created_at: db.brain.built_at, note: 'Rebuilt on demand', approved: false });
        emit('brain', db.brain);
        r.log(`Company Brain v${db.brain.version} ready for review ✓`, 'ok');
        log('agent', `Company Brain rebuilt (v${db.brain.version}) — awaiting review`, {});
      }],
    ],
  });
  return { run_id: run.id, brain: db.brain };
}

function sendMessage(messageId, asDraft) {
  const m = db.messages.find(x => x.id === messageId);
  if (!m) notFound('Message');
  m.status = asDraft ? 'draft_created' : 'sent';
  m.sent_at = new Date().toISOString();
  const lead = db.leads.find(l => l.id === m.lead_id);
  if (lead && ['new', 'researched'].includes(lead.status)) { lead.status = 'contacted'; emit('leads', [lead]); }
  emit('messages', m);
  log('agent', `${asDraft ? 'Draft created in mailbox' : 'Email sent'}${lead ? ` — ${lead.company_name}` : ''}`, { lead_id: m.lead_id, message_id: m.id });
  return m;
}

function connectEmail(provider, label) {
  const integ = {
    id: id('int'),
    provider,
    label,
    mailbox: provider === 'google' ? 'sales@silverine.com.tr' : `sales+${provider}@silverine.com.tr`,
    status: 'connected',
    connected_at: new Date().toISOString(),
    last_test: null,
  };
  db.integrations.email.push(integ);
  emit('integrations', integ);
  log('user', `${label} mailbox connected`, {});
  return integ;
}

function liMark(actionId, status) {
  const action = db.integrations.linkedin_actions.find(a => a.id === actionId);
  if (!action) notFound('LinkedIn action');
  action.status = status;
  emit('linkedin', action);
  return action;
}

function collectLeadActivity(leadId) {
  const items = db.activity.filter(a => a.ref.lead_id === leadId);
  const lead = db.leads.find(l => l.id === leadId);
  if (lead) {
    items.push({ id: `act_created_${leadId}`, kind: lead.source === 'manual' ? 'user' : 'agent', label: lead.source === 'manual' ? 'Lead created manually' : `Lead discovered via ${lead.source.replace(/_/g, ' ')}`, ref: { lead_id: leadId }, at: lead.created_at });
  }
  db.messages.filter(m => m.lead_id === leadId && m.sent_at).forEach(m => {
    items.push({ id: `act_msg_${m.id}`, kind: 'agent', label: m.status === 'draft_created' ? 'Email draft created in mailbox' : `Email ${m.status === 'replied' ? 'sent (later replied)' : m.status}`, ref: { lead_id: leadId }, at: m.sent_at });
  });
  return items.sort((a, b) => new Date(b.at) - new Date(a.at));
}

function buildRecommendedActions() {
  const actions = [];
  const awaiting = db.messages.filter(m => m.status === 'draft_generated').length;
  if (awaiting) actions.push({ icon: 'mail', title: `Review ${awaiting} generated emails awaiting approval`, sub: 'UAE HoReCa campaign is ready to go out', href: '/app/outreach' });
  if (db.onboarding.status !== 'complete') actions.push({ icon: 'upload', title: 'Finish onboarding — import your current contacts', sub: 'The Company Brain flagged missing contact data', href: '/app/onboarding' });
  const newLeads = db.leads.filter(l => l.status === 'new').length;
  if (newLeads) actions.push({ icon: 'search', title: `Research ${newLeads} new leads`, sub: 'Prioritized by lead score', href: '/app/leads?status=new' });
  if (db.leadMap.selected.length < db.leadMap.max_selected) actions.push({ icon: 'map', title: 'Expand to Netherlands and United Kingdom', sub: 'Both are recommended markets not yet scanned', href: '/app/lead-map' });
  return actions.slice(0, 4);
}

function exportRows(kind, rows) {
  const exp = { id: id('exp'), kind, status: 'ready', filename: `silverine_${kind}_${new Date().toISOString().slice(0, 10)}.csv`, rows };
  log('user', `Exported ${kind} (${rows.length} rows) to CSV`, {});
  return exp;
}
