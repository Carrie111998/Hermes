/* In-memory mock database + the startRun() job simulator that makes
   demo flows live (scan → run logs → leads appear → messages sent).
   Reseeded on every page load — deterministic demo state. */

import { COUNTRY_NAMES, BUYER_INDUSTRIES } from '../catalog.js';

export const db = {};

const ONBOARDING_STEPS = [
  ['company-identity', 'Company identity'],
  ['positioning', 'Positioning'],
  ['products', 'Product catalog'],
  ['internal-sales-data', 'Internal sales data'],
  ['current-contacts', 'Current contacts'],
  ['target-markets', 'Target markets'],
  ['integrations', 'Integration setup'],
  ['brain-review', 'Company Brain review'],
];

function emptyState() {
  return {
    user: { id: null, name: '', email: '', role: 'customer', company_id: null },
    company: {
      id: null, name: '', legal_name: '', website: '', headquarters_country: '', city: '',
      founded_year: '', industry: '', employee_count: '', business_model: '', main_language: '',
      sales_regions_current: [], sales_regions_target: [],
      positioning: {
        what_company_sells: '', main_value_proposition: '', quality_position: '', price_position: '',
        premium_or_mass_market: '', main_differentiators: [], certifications: [],
        manufacturing_capacity: '', export_capacity: '', delivery_capabilities: '', after_sales_support: '',
      },
      sales_preferences: {
        default_send_mode: 'create_draft', default_language: 'en', languages: ['en'],
        default_cc_rule_id: '', connected_mailbox: '',
      },
    },
    products: [], documents: [], leads: [], research: [], contacts: [], campaigns: [], messages: [],
    ccRules: [], leadScans: [], agentRuns: [], activity: [],
    onboarding: {
      status: 'not_started', current_step: 0,
      steps: ONBOARDING_STEPS.map(([key, label]) => ({ key, label, status: 'pending' })),
    },
    brain: {
      id: null, status: 'not_built', version: 0, built_at: null, approved_at: null,
      sections: {
        product_understanding: [], ideal_customer_profile: [], buyer_roles: [],
        market_assumptions: [], sales_arguments: [], business_rules_digest: [], missing_data: [],
      },
      snapshots: [],
    },
    leadMap: { countries: [], selected: [], max_selected: 5, summaries: {} },
    integrations: { email: [], whatsapp: [], linkedin_actions: [] },
    analytics: {
      pipeline: { leads_by_status: [], emails_sent_weekly: { labels: [], values: [] }, replies_weekly: { labels: [], values: [] }, funnel: [] },
      market: { country_scores: [], product_market_fit: [], top_industries: [], source_performance: [] },
    },
    admin: { companies: [], users: [], errors: [], logs: [] },
  };
}

/* ---------- events ---------- */
const _subs = new Map(); // topic -> Set<fn>
export function subscribe(topic, fn) {
  if (!_subs.has(topic)) _subs.set(topic, new Set());
  _subs.get(topic).add(fn);
  return () => _subs.get(topic).delete(fn);
}
export function emit(topic, payload) {
  for (const t of [topic, '*']) {
    const set = _subs.get(t);
    if (set) for (const fn of [...set]) { try { fn(payload, topic); } catch (e) { console.error(e); } }
  }
}

/* ---------- ids ---------- */
let _idSeq = 1000;
export function id(prefix) {
  return `${prefix}_${(_idSeq++).toString(36)}${Math.floor(Math.random() * 46656).toString(36)}`;
}

/* ---------- activity ---------- */
export function log(kind, label, ref = {}) {
  const entry = { id: id('act'), kind, label, ref, at: new Date().toISOString() };
  db.activity.unshift(entry);
  emit('activity', entry);
  return entry;
}

/* ---------- run simulator ---------- */
const _timers = new Set();
function schedule(fn, ms) {
  const t = setTimeout(() => { _timers.delete(t); fn(); }, ms);
  _timers.add(t);
  return t;
}

/**
 * startRun({ type, label, related, script }) -> run
 * script: array of [delayMs, step(run) => void] played sequentially.
 * Steps normally call run.log(line, cls) and set run.progress.
 * The final step should perform the payoff mutation (insert leads, etc.).
 */
export function startRun({ type, label, related = {}, script = [] }) {
  const run = {
    id: id('run'),
    type, label, related,
    status: 'running',
    progress: 2,
    created_at: new Date().toISOString(),
    finished_at: null,
    logs: [],
  };
  run.log = (line, cls = '') => {
    run.logs.push({ t: new Date().toISOString(), line, cls });
    emit('runs', run);
  };
  db.agentRuns.unshift(run);
  emit('runs', run);

  let elapsed = 0;
  script.forEach(([delay, step], i) => {
    elapsed += delay;
    schedule(() => {
      if (run.status !== 'running') return; // cancelled
      try { step(run); } catch (e) { console.error('[mock run]', e); }
      if (i === script.length - 1 && run.status === 'running') {
        run.status = 'completed';
        run.progress = 100;
        run.finished_at = new Date().toISOString();
        emit('runs', run);
      }
    }, elapsed);
  });
  return run;
}

export function cancelRun(runId) {
  const run = db.agentRuns.find(r => r.id === runId);
  if (run && run.status === 'running') {
    run.status = 'cancelled';
    run.finished_at = new Date().toISOString();
    run.log('Run cancelled by user.', 'warn');
    emit('runs', run);
  }
  return run;
}

/* ---------- canned generators used by scan runs ---------- */
const EXTRA_LEAD_NAMES = {
  NL: ['Van Dijk Keukens Import BV', 'Rotterdam Appliance House', 'Benelux Witgoed Groep', 'Amstel Interieur Projecten', 'Noordzee Home Supplies', 'Keukencentrum Utrecht BV', 'Delta Kitchen Partners', 'Hollandia Retail Groep'],
  GB: ['Thames Valley Appliances Ltd', 'Northern Kitchen Supplies', 'BritKitchen Distribution', 'Crown Interiors Group', 'Albion White Goods Ltd', 'Mercia Home Solutions', 'Kensington Design Studios', 'Yorkshire Trade Appliances'],
  SA: ['Riyadh Kitchen House', 'Al Salem Trading Est', 'Jeddah Home Appliances Co', 'Najd Distribution Group', 'Red Sea Hotel Supplies', 'Al Khobar Interiors', 'Kingdom Kitchen Projects', 'Dammam Import House'],
  _default: ['Global Kitchen Imports', 'Prime Appliance Trading', 'Metro Home Distribution', 'Crown Equipment Supply', 'Atlas Interior Projects', 'Unity Retail Group'],
};
const EXTRA_CITIES = {
  NL: ['Amsterdam', 'Rotterdam', 'Utrecht', 'Eindhoven'],
  GB: ['London', 'Manchester', 'Birmingham', 'Leeds'],
  SA: ['Riyadh', 'Jeddah', 'Dammam', 'Al Khobar'],
  _default: ['Capital City'],
};

export function generateLeadsForCountry(cc, count, scanId) {
  const names = EXTRA_LEAD_NAMES[cc] || EXTRA_LEAD_NAMES._default;
  const cities = EXTRA_CITIES[cc] || [COUNTRY_NAMES[cc] || cc];
  const created = [];
  for (let i = 0; i < count; i++) {
    const name = names[i % names.length] + (i >= names.length ? ` ${Math.floor(i / names.length) + 1}` : '');
    const score = Math.round(42 + Math.random() * 54);
    const lead = {
      id: id('lead'),
      company_name: name,
      country: cc,
      city: cities[i % cities.length],
      industry: BUYER_INDUSTRIES[i % BUYER_INDUSTRIES.length],
      website: `https://${name.toLowerCase().replace(/[^a-z0-9]+/g, '').slice(0, 16)}.example.com`,
      size_hint: ['10-50', '50-100', '100-250'][i % 3],
      source: ['web_search', 'trade_data', 'exhibitor_lists'][i % 3],
      status: 'new',
      scan_id: scanId,
      created_at: new Date().toISOString(),
      score: {
        value: score,
        band: score >= 75 ? 'high' : score >= 50 ? 'mid' : 'low',
        factors: [
          { label: 'Industry fit', weight: 30, note: 'Matches Silverine buyer categories' },
          { label: 'Import activity', weight: 25, note: 'Trade data shows appliance import volume' },
          { label: 'Company size', weight: 20, note: 'Within ICP employee range' },
          { label: 'Market opportunity', weight: 15, note: `${COUNTRY_NAMES[cc] || cc} opportunity score` },
          { label: 'Web signals', weight: 10, note: 'Product pages and brand portfolio' },
        ],
      },
    };
    db.leads.unshift(lead);
    created.push(lead);
  }
  emit('leads', created);
  return created;
}

/* Standard lead-scan run script */
export function startLeadScanRun(scan) {
  const depthDefault = scan.depth === 'quick' ? 5 : scan.depth === 'deep' ? 12 : 8;
  const perCountry = Math.max(3, Math.min(20, Number(scan.leads_per_country) || depthDefault));
  const script = [];
  script.push([600, (run) => { run.progress = 5; run.log(`Scan configuration loaded — depth: ${scan.depth}, sources: ${scan.sources.join(', ')}`); }]);
  script.push([1400, (run) => { run.progress = 12; run.log('Company Brain context attached (ICP, buyer roles, market assumptions).'); }]);
  const per = Math.floor(70 / Math.max(1, scan.countries.length));
  scan.countries.forEach((cc, ci) => {
    const cname = COUNTRY_NAMES[cc] || cc;
    script.push([1600, (run) => { run.progress = 15 + ci * per; run.log(`— ${cname}: querying trade databases and directories…`); }]);
    script.push([2200, (run) => { run.progress = 15 + ci * per + Math.floor(per * 0.4); run.log(`— ${cname}: ${8 + Math.floor(Math.random() * 20)} candidate companies found, filtering against ICP…`); }]);
    script.push([2400, (run) => {
      const leads = generateLeadsForCountry(cc, perCountry, scan.id);
      scan.leads_found += leads.length;
      run.progress = 15 + (ci + 1) * per;
      run.log(`— ${cname}: ${leads.length} qualified leads added ✓`, 'ok');
    }]);
  });
  script.push([1500, (run) => {
    run.progress = 96;
    run.log('Scoring leads against Company Brain…');
  }]);
  script.push([1200, (run) => {
    scan.status = 'completed';
    scan.completed_at = new Date().toISOString();
    emit('scans', scan);
    run.log(`Scan complete — ${scan.leads_found} leads across ${scan.countries.length} market(s).`, 'ok');
    log('agent', `Lead scan completed — ${scan.leads_found} leads discovered (${scan.countries.map(c => COUNTRY_NAMES[c] || c).join(', ')})`, { scan_id: scan.id });
  }]);
  const run = startRun({ type: 'lead_scan', label: `Lead scan — ${scan.countries.map(c => COUNTRY_NAMES[c] || c).join(', ')}`, related: { scan_id: scan.id }, script });
  scan.run_id = run.id;
  scan.status = 'running';
  emit('scans', scan);
  return run;
}

/* ---------- reset ---------- */
export async function reset() {
  for (const t of _timers) clearTimeout(t);
  _timers.clear();
  const { makeSeed } = await import('./seed.js');
  const seed = makeSeed();
  for (const k of Object.keys(db)) delete db[k];
  Object.assign(db, seed);

  // Bring the seeded "running" SA scan to life so the app shows motion on load.
  const saScan = db.leadScans.find(s => s.id === 'scan_sa');
  if (saScan) {
    saScan.leads_found = 0;
    const run = startLeadScanRun(saScan);
    // keep the seeded id referenced elsewhere in sync
    saScan.run_id = run.id;
  }
}

/** Initialize an authenticated real-backend session without demo tenant data. */
export function resetReal() {
  for (const t of _timers) clearTimeout(t);
  _timers.clear();
  for (const k of Object.keys(db)) delete db[k];
  Object.assign(db, emptyState());
  emit('reset', db);
}
