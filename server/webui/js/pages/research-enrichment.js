import { chipSelect, el, field, input, select } from '../ui.js';

const FEATURES = [
  ['identity_scale', 'Identity & company scale'], ['market_coverage', 'Market coverage'],
  ['trade_activity', 'Import/export activity'], ['commercial_capacity', 'Commercial & physical capacity'],
  ['public_financial_value', 'Revenue & public financial value'],
  ['private_value_range', 'Documented private value range'],
  ['locations', 'Stores, offices, warehouses & factories'], ['buying_intent', 'Buying/procurement intent'],
  ['product_fit', 'Product, brand, certification, OEM & private-label fit'],
];

export function renderEnrichment(config, modelProfiles, { onChange } = {}) {
  const current = structuredClone(config);
  const featureSelect = chipSelect(FEATURES.map(([value, label]) => ({ value, label })), current.features, {
    onChange: value => { current.features = value; onChange?.(current); },
  });
  const enabled = el('input', { type: 'checkbox', checked: current.enrichment.enabled });
  const model = select([
    { value: '', label: modelProfiles.length ? 'Choose a model profile' : 'No local model configured' },
    ...modelProfiles.map(item => ({ value: item.id, label: `${item.name}${item.local ? ' · local' : ''}` })),
  ], { value: current.enrichment.model_profile || '', disabled: !modelProfiles.length });
  enabled.addEventListener('change', () => {
    current.enrichment.enabled = enabled.checked;
    if (!enabled.checked) current.enrichment.model_profile = null;
    onChange?.(current);
  });
  model.addEventListener('change', () => { current.enrichment.model_profile = model.value || null; onChange?.(current); });
  const numeric = (label, key, hint, min, max) => {
    const control = input({ type: 'number', value: current.enrichment[key], min, max });
    control.addEventListener('input', () => { current.enrichment[key] = Number(control.value); onChange?.(current); });
    return field(label, control, { hint });
  };
  const schedule = select(['none', 'weekly', 'monthly', 'quarterly'].map(value => ({ value, label: value })), {
    value: current.refresh.schedule,
  });
  schedule.addEventListener('change', () => { current.refresh.schedule = schedule.value; onChange?.(current); });
  return el('div', { class: 'ifz-research-stack' },
    el('section', {}, el('div', { class: 'ifz-research-section-kicker' }, 'Applicable feature families'), featureSelect),
    el('section', { class: 'ifz-research-ai-panel' },
      el('div', { class: 'ifz-research-toggle-line' }, enabled,
        el('div', {}, el('strong', {}, 'Enable local-AI fallback'),
          el('p', { class: 'ifz-hint' }, 'Runs only after structured sources and accepts evidence-backed schema-valid claims.'))),
      field('Model profile', model, { hint: modelProfiles.length
        ? 'A paid remote model is never selected silently.'
        : 'Configure a local Hermes model profile to enable this fallback.' }),
      el('div', { class: 'ifz-grid cols-4' },
        numeric('Completeness target', 'completeness_target', '% of applicable priority fields', 0, 100),
        numeric('Companies per campaign', 'max_companies', 'Hard maximum', 1, 500),
        numeric('Pages per company', 'max_pages_per_company', 'Hard maximum', 1, 50),
        numeric('Token budget', 'max_tokens', 'Hard maximum', 100, 100000))),
    el('section', {}, el('div', { class: 'ifz-research-section-kicker' }, 'Refresh & retention'),
      el('div', { class: 'ifz-grid cols-3' },
        field('Refresh schedule', schedule, { hint: 'Volatile claims can be refreshed without overwriting prior snapshots.' }),
        field('Raw snapshot retention (days)', input({ type: 'number', min: 1, value: current.retention.raw_snapshot_days,
          oninput: e => { current.retention.raw_snapshot_days = Number(e.target.value); onChange?.(current); } }), { hint: 'Immutable provider pages.' }),
        field('Web snapshot retention (days)', input({ type: 'number', min: 1, value: current.retention.web_snapshot_days,
          oninput: e => { current.retention.web_snapshot_days = Number(e.target.value); onChange?.(current); } }), { hint: 'Content-addressed research cache.' }))));
}
