/* Server-owned draft state for the persistent research editor. */
import { call } from './api.js';

export const DEFAULT_RESEARCH_CONFIG = {
  name: '',
  seller_countries: ['TR'],
  target_countries: [],
  sector_ids: [],
  hs_codes: [],
  product_ids: [],
  buyer_types: ['importer', 'distributor'],
  enabled_source_ids: [],
  precision_profile: 'high_precision',
  max_qualified_leads_per_country: 50,
  freshness_days: 180,
  exclusions: { company_ids: [], domains: [], seller_only: true, sanctioned_entities: true },
  eligibility: {
    require_resolved_identity: true, require_official_domain: false,
    require_target_presence: true, require_buyer_role: true,
    exclude_inactive: true, minimum_independent_sources: 1,
  },
  scoring: {
    profile_id: 'default-high-precision', name: 'High precision',
    weights: {
      product_sector_fit: 25, buyer_channel_fit: 20, buying_intent: 15,
      market_coverage: 15, commercial_scale: 10, trade_activity: 10, contactability: 5,
    },
    bands: {
      A: { min_fit: 80, min_confidence: .72 },
      B: { min_fit: 60, min_confidence: .45 },
      C: { min_fit: 35, min_confidence: .2 },
    },
  },
  enrichment: {
    profile_id: 'local-balanced', enabled: false, model_profile: null,
    trigger: 'missing_required', completeness_target: 80, max_companies: 25,
    max_pages_per_company: 8, max_seconds_per_company: 120, max_tokens: 6000,
    source_policy: 'official_and_credible',
  },
  features: ['identity_scale', 'market_coverage', 'trade_activity', 'buying_intent', 'product_fit'],
  refresh: { schedule: 'monthly', reuse_public_cache: true },
  retention: { raw_snapshot_days: 365, web_snapshot_days: 180, export_days: 90 },
  source_overrides: {},
};

export function deepMerge(base, patch) {
  if (!patch || typeof patch !== 'object' || Array.isArray(patch)) return structuredClone(patch);
  const next = structuredClone(base || {});
  for (const [key, value] of Object.entries(patch)) {
    next[key] = value && typeof value === 'object' && !Array.isArray(value)
      ? deepMerge(next[key] || {}, value)
      : structuredClone(value);
  }
  return next;
}

export function createResearchState({ campaign = null } = {}) {
  let current = campaign
    ? structuredClone(campaign)
    : { id: null, status: 'draft', version: 0, config: structuredClone(DEFAULT_RESEARCH_CONFIG), estimate: null };

  return {
    get: () => structuredClone(current),
    updateConfig(patch) { current.config = deepMerge(current.config, patch); return this.get(); },
    setEstimate(estimate) { current.estimate = structuredClone(estimate); return this.get(); },
    async save() {
      current = current.id
        ? await call('researchCampaigns.patch', {
            params: { campaignId: current.id },
            body: { version: current.version, config: current.config },
          })
        : await call('researchCampaigns.create', { body: current.config });
      return this.get();
    },
    async estimate() {
      if (!current.id) await this.save();
      current.estimate = await call('researchCampaigns.estimate', { params: { campaignId: current.id } });
      return structuredClone(current.estimate);
    },
  };
}
