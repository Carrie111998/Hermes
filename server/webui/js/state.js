/* Shared reactive cache for responses from the production API.

   Pages render from this cache while real-state.js mirrors each backend
   response into it. It deliberately starts empty: no tenant data belongs in
   the browser bundle. */

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

const subscribers = new Map();

export function subscribe(topic, listener) {
  if (!subscribers.has(topic)) subscribers.set(topic, new Set());
  subscribers.get(topic).add(listener);
  return () => subscribers.get(topic).delete(listener);
}

export function emit(topic, payload) {
  for (const subscription of [topic, '*']) {
    const listeners = subscribers.get(subscription);
    if (listeners) for (const listener of [...listeners]) {
      try { listener(payload, topic); } catch (error) { console.error(error); }
    }
  }
}

export function resetReal() {
  for (const key of Object.keys(db)) delete db[key];
  Object.assign(db, emptyState());
  emit('reset', db);
}
