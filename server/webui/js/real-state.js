/* Mirror real API responses into the UI's existing shared view-model.

   The original SPA was written around mocks/db.js. Phase 2 keeps that small
   observable store as the page-level cache, but real responses replace (or
   upsert into) the corresponding collections. MOCK_ROUTES continue to mutate
   the same store through their handlers, so hybrid mode has one coherent
   source of rendered state instead of real records mixed with Silverine seed
   records. */

import { db, emit } from './mocks/db.js';

const REAL_ONBOARDING_STEPS = new Set([
  'company-identity', 'positioning', 'products', 'internal-sales-data',
  'current-contacts', 'target-markets', 'integrations', 'brain-review',
]);

function items(payload) {
  return Array.isArray(payload?.items) ? payload.items : [];
}

function replace(key, payload, topic = key) {
  db[key] = items(payload);
  emit(topic, db[key]);
}

function upsert(key, value, topic = key) {
  if (!value?.id) return;
  if (!Array.isArray(db[key])) db[key] = [];
  const index = db[key].findIndex(item => item.id === value.id);
  if (index === -1) db[key].unshift(value);
  else db[key][index] = value;
  emit(topic, value);
}

function remove(key, id, topic = key) {
  if (!id || !Array.isArray(db[key])) return;
  db[key] = db[key].filter(item => item.id !== id);
  emit(topic, null);
}

function syncOnboarding(payload) {
  if (!payload || !db.onboarding?.steps) return;
  const completed = new Set(payload.completed_steps || []);
  db.onboarding.status = payload.status === 'completed'
    ? 'complete'
    : payload.status || db.onboarding.status;
  for (const step of db.onboarding.steps) {
    if (REAL_ONBOARDING_STEPS.has(step.key)) {
      step.status = completed.has(step.key) ? 'done' : 'pending';
    }
  }
  const index = db.onboarding.steps.findIndex(step => step.key === payload.current_step);
  if (index >= 0) db.onboarding.current_step = index;
  else if (payload.status === 'completed') db.onboarding.current_step = db.onboarding.steps.length - 1;
  emit('onboarding', db.onboarding);
}

export function syncRealResponse(name, payload, { params = {} } = {}) {
  if (name === 'auth.login') {
    db.user = payload?.user || db.user;
    db.company = { ...db.company, ...(payload?.company || {}) };
    emit('user', db.user);
    emit('company', db.company);
  } else if (name === 'auth.me') {
    db.user = payload?.user || db.user;
    db.company = { ...db.company, ...(payload?.company || {}) };
    emit('user', db.user);
    emit('company', db.company);
  } else if (name === 'company.getProfile' || name === 'company.updateProfile') {
    const positioning = db.company?.positioning || {};
    const salesPreferences = db.company?.sales_preferences || {};
    db.company = {
      id: payload?.id || db.company?.id || null,
      name: '', legal_name: '', website: '', headquarters_country: '', city: '',
      founded_year: '', industry: '', employee_count: '', business_model: '', main_language: '',
      sales_regions_current: [], sales_regions_target: [],
      ...(payload || {}), positioning, sales_preferences: salesPreferences,
    };
    emit('company', db.company);
  } else if (name === 'company.getPositioning' || name === 'company.updatePositioning') {
    db.company.positioning = {
      what_company_sells: '', main_value_proposition: '', quality_position: '', price_position: '',
      premium_or_mass_market: '', main_differentiators: [], certifications: [],
      manufacturing_capacity: '', export_capacity: '', delivery_capabilities: '', after_sales_support: '',
      ...(payload || {}),
    };
    emit('company', db.company);
  } else if (name === 'company.getSalesPreferences' || name === 'company.updateSalesPreferences') {
    db.company.sales_preferences = {
      default_send_mode: 'create_draft', default_language: 'en', languages: ['en'],
      default_cc_rule_id: '', connected_mailbox: '', ...(payload || {}),
    };
    emit('company', db.company);
  } else if (name === 'onboarding.status' || name === 'onboarding.start' || name.startsWith('onboarding.update') || name === 'onboarding.reviewBrain' || name === 'onboarding.complete') {
    syncOnboarding(payload);
  } else if (name === 'products.list') replace('products', payload);
  else if (['products.create', 'products.get', 'products.update'].includes(name)) upsert('products', payload);
  else if (name === 'products.delete') remove('products', params.productId);
  else if (name === 'documents.list') replace('documents', payload);
  else if (name === 'documents.upload' || name === 'documents.get') upsert('documents', payload);
  else if (name === 'documents.delete') remove('documents', params.documentId);
  else if (name === 'brain.get' || name === 'brain.update' || name === 'brain.approve') {
    db.brain = payload;
    emit('brain', payload);
  } else if (name === 'leadMap.countries') {
    db.leadMap.countries = items(payload);
    emit('leadMap', db.leadMap);
  } else if (name === 'analytics.pipeline') {
    db.analytics.pipeline = payload;
    emit('analytics', db.analytics);
  } else if (name === 'analytics.market') {
    db.analytics.market = payload;
    emit('analytics', db.analytics);
  } else if (['leadMap.selectedCountries', 'leadMap.selectCountry'].includes(name)) {
    db.leadMap.selected = items(payload);
    emit('leadMap', db.leadMap.selected);
  } else if (name === 'leadScans.list') replace('leadScans', payload, 'scans');
  else if (['leadScans.create', 'leadScans.get'].includes(name)) upsert('leadScans', payload, 'scans');
  else if (name === 'leads.list' || name === 'leadScans.results') replace('leads', payload);
  else if (['leads.create', 'leads.get', 'leads.update'].includes(name)) upsert('leads', payload);
  else if (name === 'leads.delete') remove('leads', params.leadId);
  else if (name === 'research.list') replace('research', payload);
  else if (name === 'research.get') upsert('research', payload);
  else if (name === 'contacts.list') replace('contacts', payload);
  else if (['contacts.create', 'contacts.get', 'contacts.update'].includes(name)) upsert('contacts', payload);
  else if (name === 'contacts.delete') remove('contacts', params.contactId);
  else if (name === 'campaigns.list') replace('campaigns', payload);
  else if (['campaigns.create', 'campaigns.get', 'campaigns.update'].includes(name)) upsert('campaigns', payload);
  else if (name === 'campaigns.delete') remove('campaigns', params.campaignId);
  else if (name === 'messages.list') replace('messages', payload);
  else if (['messages.get', 'messages.update', 'messages.approve'].includes(name)) upsert('messages', payload);
  else if (name === 'ccRules.list') replace('ccRules', payload);
  else if (['ccRules.create', 'ccRules.get', 'ccRules.update'].includes(name)) upsert('ccRules', payload);
  else if (name === 'ccRules.delete') remove('ccRules', params.ruleId);
  else if (name === 'agentRuns.list') replace('agentRuns', payload, 'runs');
  else if (['agentRuns.create', 'agentRuns.get', 'agentRuns.start', 'agentRuns.cancel', 'agentRuns.retry'].includes(name)) upsert('agentRuns', payload, 'runs');
  else if (name === 'activity.list') replace('activity', payload);
}
