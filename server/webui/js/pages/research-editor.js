import { call } from '../api.js';
import { COUNTRY_NAMES } from '../catalog.js';
import {
  button, card, chipSelect, el, field, input, pageHead, radioCards, select, setBusy, stepper, toast,
} from '../ui.js';
import { createResearchState } from '../research-state.js';
import { renderEnrichment } from './research-enrichment.js';
import { renderScoring, weightTotal } from './research-scoring.js';
import { renderSourcePicker } from './research-source-picker.js';

const unwrap = value => value?.items || value || [];
const STEPS = ['Scope', 'Sources', 'Qualification', 'Enrichment', 'Review & run'];
const BUYER_LABELS = {
  importer: 'Importer', distributor: 'Distributor', retailer: 'Retailer', brand: 'Brand',
  wholesaler: 'Wholesaler', procurement_organization: 'Procurement organization',
};

function sentenceList(items) { return items?.length ? items.join(', ') : 'None selected'; }

export async function mount(root, ctx) {
  const campaignId = ctx.params.campaignId;
  const [campaign, configuration, sectorsRes, sourcesRes, modelsRes] = await Promise.all([
    campaignId ? call('researchCampaigns.get', { params: { campaignId } }) : Promise.resolve(null),
    call('research.configuration'), call('research.sectors'), call('dataSources.catalog'), call('research.modelProfiles'),
  ]);
  const sectors = unwrap(sectorsRes);
  const sources = unwrap(sourcesRes);
  const modelProfiles = unwrap(modelsRes);
  const state = createResearchState({ campaign });
  if (!campaign) {
    const prefilled = (ctx.query.countries || '').split(',').map(value => value.trim().toUpperCase()).filter(Boolean);
    const available = sources.filter(source => source.available).map(source => source.source_id);
    state.updateConfig({
      target_countries: prefilled,
      enabled_source_ids: available.slice(0, 1),
    });
  }
  let active = 0;
  let errorSummary = null;
  let estimateRequest = 0;
  const stepHost = el('div');
  const formHost = el('div', { class: 'ifz-research-editor-body' });
  const actionHost = el('div', { class: 'ifz-research-editor-actions' });

  function update(patch) { state.updateConfig(patch); }
  function validation(step = active) {
    const current = state.get();
    const cfg = current.config;
    const errors = [];
    if (step === 0 || step === 4) {
      if (cfg.name.trim().length < 3) errors.push(['name', 'Campaign name must contain at least 3 characters.']);
      if (!cfg.seller_countries.length) errors.push(['seller', 'Select at least one seller country.']);
      if (!cfg.target_countries.length) errors.push(['targets', 'Select at least one target country.']);
      if (!(cfg.sector_ids.length || cfg.hs_codes.length || cfg.product_ids.length)) {
        errors.push(['sectors', 'Select a sector, HS code, or tenant product.']);
      }
      if (!cfg.buyer_types.length) errors.push(['buyers', 'Select at least one plausible buyer type.']);
    }
    if ((step === 1 || step === 4) && !cfg.enabled_source_ids.length) {
      errors.push(['sources', 'Select at least one available evidence source.']);
    }
    if ((step === 2 || step === 4) && weightTotal(cfg.scoring.weights) !== 100) {
      errors.push(['weights', 'Scoring weights must total exactly 100.']);
    }
    if ((step === 3 || step === 4) && cfg.enrichment.enabled && !cfg.enrichment.model_profile) {
      errors.push(['model', 'Choose an available model profile or disable local-AI fallback.']);
    }
    return errors;
  }

  function showErrors(errors) {
    errorSummary?.remove();
    if (!errors.length) { errorSummary = null; return; }
    errorSummary = el('div', { class: 'ifz-research-errors', role: 'alert', tabindex: '-1' },
      el('strong', {}, 'Resolve these items before continuing'),
      el('ul', {}, errors.map(([, message]) => el('li', {}, message))));
    formHost.prepend(errorSummary);
    errorSummary.focus();
  }

  function scopeStep() {
    const cfg = state.get().config;
    const name = input({ name: 'campaign-name', value: cfg.name, maxlength: 120, placeholder: 'e.g. DACH appliance distributors' });
    name.addEventListener('input', () => update({ name: name.value }));
    const countries = Object.entries(COUNTRY_NAMES).map(([value, label]) => ({ value, label }));
    const sellers = chipSelect(countries, cfg.seller_countries, { onChange: value => update({ seller_countries: value }) });
    const targets = chipSelect(countries, cfg.target_countries, { onChange: value => update({ target_countries: value }) });
    const sectorPicker = chipSelect(sectors.map(item => ({ value: item.sector_id, label: item.name })), cfg.sector_ids, {
      onChange: value => update({ sector_ids: value }),
    });
    const buyers = chipSelect(configuration.buyer_types.map(value => ({ value, label: BUYER_LABELS[value] || value })), cfg.buyer_types, {
      onChange: value => update({ buyer_types: value }),
    });
    const precision = radioCards([
      { value: 'high_precision', title: 'High precision', desc: 'Tighter evidence gates; fewer false positives.', meta: 'Default' },
      { value: 'balanced', title: 'Balanced', desc: 'Broader discovery with review queues.' },
      { value: 'exploratory', title: 'Exploratory', desc: 'Maximum coverage; material uncertainty is explicit.' },
    ], cfg.precision_profile, { onChange: value => update({ precision_profile: value }) });
    const max = input({ type: 'number', min: 1, max: 200, value: cfg.max_qualified_leads_per_country });
    max.addEventListener('input', () => update({ max_qualified_leads_per_country: Number(max.value) }));
    const hs = input({ value: cfg.hs_codes.join(', '), placeholder: '8418, 8516' });
    hs.addEventListener('change', () => update({ hs_codes: hs.value.split(',').map(value => value.trim()).filter(Boolean) }));
    return el('div', { class: 'ifz-research-stack' },
      el('div', { class: 'ifz-research-step-intro' },
        el('span', { class: 'ifz-research-step-no' }, '01'),
        el('div', {}, el('h2', {}, 'Define the buyer universe'),
          el('p', {}, 'Required scope first. The ceiling controls workload; it is never a promised result count.'))),
      card({ body: el('div', {},
        field('Campaign name', name, { required: true, hint: '3–120 characters; use a market and buyer-role description.' }),
        field('Seller countries', sellers, { required: true, hint: 'Defaults to Türkiye. Effective value origin: system-safe default.' }),
        field('Target countries', targets, { required: true, hint: 'Large selections create bounded country × sector × source partitions.' }),
        el('div', { class: 'ifz-row' }, button('Select on map', { kind: 'ghost', icon: 'map', onClick: () => ctx.navigate('/app/buyers?map=1') })),
        field('Sectors', sectorPicker, { hint: 'Canonical taxonomy controls classification coverage and applicable features.' }),
        field('HS codes', hs, { hint: 'Optional narrowing; interpreted against HS 2022.' }),
        field('Buyer types', buyers, { required: true }),
        field('Precision profile', precision),
        field('Maximum qualified leads per target country', max, { hint: 'A hard ceiling, not a forecast.' })) }),
    );
  }

  function sourcesStep() {
    const cfg = state.get().config;
    return el('div', { class: 'ifz-research-stack' },
      el('div', { class: 'ifz-research-step-intro' },
        el('span', { class: 'ifz-research-step-no' }, '02'),
        el('div', {}, el('h2', {}, 'Choose defensible evidence'),
          el('p', {}, 'Market signals, named-company records, opportunities and events keep their distinct meanings.'))),
      renderSourcePicker(sources, cfg.enabled_source_ids, { onChange: value => update({ enabled_source_ids: value }) }),
      el('details', { class: 'ifz-research-advanced' },
        el('summary', {}, 'Advanced per-source settings'),
        el('p', {}, 'Date windows, safe page caps, timeouts, rate limits, freshness thresholds and last-valid snapshot behavior are provider capabilities. Unsupported overrides are not shown.')),
    );
  }

  function qualificationStep() {
    const cfg = state.get().config;
    const gates = [
      ['require_resolved_identity', 'Require resolved legal identity'],
      ['require_official_domain', 'Require official domain'],
      ['require_target_presence', 'Require target-country presence'],
      ['require_buyer_role', 'Require plausible buyer role'],
      ['exclude_inactive', 'Exclude inactive or dissolved entities'],
    ];
    const gateGrid = el('div', { class: 'ifz-gate-grid' }, gates.map(([key, label]) => {
      const control = el('input', { type: 'checkbox', checked: cfg.eligibility[key] });
      control.addEventListener('change', () => update({ eligibility: { [key]: control.checked } }));
      return el('label', { class: 'ifz-gate' }, control, el('span', {}, label));
    }));
    // Enforced, so it has to be adjustable: a hidden minimum is a policy the
    // tenant cannot see and cannot loosen. Zero switches the gate off.
    const minimumSources = el('input', {
      type: 'number', min: '0', max: '5', step: '1',
      value: String(cfg.eligibility.minimum_independent_sources ?? 1),
    });
    minimumSources.addEventListener('change', () => update({
      eligibility: { minimum_independent_sources: Math.max(0, Number(minimumSources.value) || 0) },
    }));
    return el('div', { class: 'ifz-research-stack' },
      el('div', { class: 'ifz-research-step-intro' },
        el('span', { class: 'ifz-research-step-no' }, '03'),
        el('div', {}, el('h2', {}, 'Set qualification rules'),
          el('p', {}, 'Eligibility runs before scoring. Compliance gates remain explicit and non-overridable.'))),
      card({ title: 'Eligibility gates', body: el('div', {}, gateGrid,
        el('label', { class: 'ifz-gate' }, minimumSources,
          el('span', {}, 'Independent sources required (0 switches this off)')),
        el('div', { class: 'ifz-policy-lock' }, 'No sanctions screening source is connected, so compliance is reported as unknown rather than passed. Research never authorizes outreach.')) }),
      card({ title: 'Fit score', body: renderScoring(cfg.scoring, { onChange: value => update({ scoring: value }) }) }),
    );
  }

  function enrichmentStep() {
    const cfg = state.get().config;
    return el('div', { class: 'ifz-research-stack' },
      el('div', { class: 'ifz-research-step-intro' },
        el('span', { class: 'ifz-research-step-no' }, '04'),
        el('div', {}, el('h2', {}, 'Research only what is missing'),
          el('p', {}, 'Structured evidence runs first; local research is bounded by pages, time, tokens and claim validation.'))),
      renderEnrichment(cfg, modelProfiles, { onChange: value => update(value) }),
    );
  }

  function estimatePanel(estimate) {
    if (!estimate || estimate.status !== 'available') {
      return el('div', { class: 'ifz-estimate-panel unavailable' },
        el('strong', {}, 'No defensible lead-volume estimate yet'),
        el('p', {}, estimate?.basis || 'Save the draft and request a source-backed estimate.'),
        estimate?.unavailable_source_ids?.length
          ? el('span', { class: 'ifz-hint' }, `Unavailable: ${estimate.unavailable_source_ids.join(', ')}`) : null);
    }
    return el('div', { class: 'ifz-estimate-panel' },
      el('div', {}, el('span', {}, 'Estimated named candidates'), el('strong', {}, estimate.named_candidate_range.join('–'))),
      el('div', {}, el('span', {}, 'Estimated eligible companies'), el('strong', {}, estimate.eligible_range.join('–'))),
      el('div', {}, el('span', {}, 'Estimated qualified leads'), el('strong', {}, estimate.qualified_range.join('–'))),
      el('p', {}, `Basis: ${estimate.basis}`),
      el('span', { class: 'ifz-hint' }, `Confidence: ${estimate.confidence} · ${estimate.expected_partitions} partitions`));
  }

  function reviewStep() {
    const current = state.get();
    const cfg = current.config;
    const chosenSources = sources.filter(source => cfg.enabled_source_ids.includes(source.source_id));
    return el('div', { class: 'ifz-research-stack' },
      el('div', { class: 'ifz-research-step-intro' },
        el('span', { class: 'ifz-research-step-no' }, '05'),
        el('div', {}, el('h2', {}, 'Review the evidence contract'),
          el('p', {}, 'The campaign can finish partial when one source fails; usable results remain inspectable.'))),
      el('div', { class: 'ifz-grid cols-2' },
        card({ title: 'Scope', body: el('dl', { class: 'ifz-review-list' },
          el('dt', {}, 'Seller'), el('dd', {}, sentenceList(cfg.seller_countries)),
          el('dt', {}, 'Targets'), el('dd', {}, sentenceList(cfg.target_countries)),
          el('dt', {}, 'Sectors'), el('dd', {}, sentenceList(cfg.sector_ids)),
          el('dt', {}, 'Buyer types'), el('dd', {}, sentenceList(cfg.buyer_types)),
          el('dt', {}, 'Ceiling'), el('dd', {}, `Up to ${cfg.max_qualified_leads_per_country} qualified leads per country`)) }),
        card({ title: 'Evidence & scoring', body: el('dl', { class: 'ifz-review-list' },
          el('dt', {}, 'Sources'), el('dd', {}, chosenSources.map(source => source.display_name).join(', ') || 'None'),
          el('dt', {}, 'Source levels'), el('dd', {}, [...new Set(chosenSources.flatMap(source => source.entity_levels))].join(', ')),
          el('dt', {}, 'Fit weights'), el('dd', {}, `${weightTotal(cfg.scoring.weights)} / 100`),
          el('dt', {}, 'Evidence confidence'), el('dd', {}, 'Calculated independently'),
          el('dt', {}, 'Local-AI fallback'), el('dd', {}, cfg.enrichment.enabled ? cfg.enrichment.model_profile : 'Off')) })),
      estimatePanel(current.estimate),
    );
  }

  const renderers = [scopeStep, sourcesStep, qualificationStep, enrichmentStep, reviewStep];

  async function saveDraft({ quiet = false } = {}) {
    const errors = validation(4);
    if (errors.length) { showErrors(errors); return null; }
    try {
      const saved = await state.save();
      if (!quiet) toast('Research draft saved', 'success');
      return saved;
    } catch (error) {
      showErrors([['server', error.message]]);
      return null;
    }
  }

  async function requestEstimate(btn) {
    const saved = await saveDraft({ quiet: true });
    if (!saved) return;
    const requestId = ++estimateRequest;
    setBusy(btn, true, 'Estimating…');
    try {
      const estimate = await state.estimate();
      if (requestId === estimateRequest) render();
    } catch (error) { showErrors([['estimate', error.message]]); }
    finally { setBusy(btn, false); }
  }

  function render() {
    errorSummary = null;
    stepHost.replaceChildren(stepper(STEPS, active, { onStep: index => { active = index; render(); } }));
    formHost.replaceChildren(renderers[active]());
    const actions = [];
    if (active > 0) actions.push(button('Back', { kind: 'ghost', onClick: () => { active -= 1; render(); } }));
    actions.push(button('Save draft', { kind: 'ghost', onClick: () => saveDraft() }));
    if (active === 4) {
      const estimateBtn = button('Refresh estimate', { kind: 'ghost', onClick: () => requestEstimate(estimateBtn) });
      actions.push(estimateBtn);
      actions.push(button('Start research', { kind: 'primary', onClick: async () => {
        const saved = await saveDraft({ quiet: true });
        if (!saved) return;
        await call('researchCampaigns.start', { params: { campaignId: saved.id } });
        // `/start` queues and returns; the campaign's own page carries its
        // status and partial-coverage notice once it settles.
        toast('Research queued. Progress and coverage appear on the campaign.', 'success');
        ctx.navigate(`/admin/research/${saved.id}`);
      } }));
    } else {
      actions.push(button('Continue', { kind: 'primary', onClick: () => {
        const errors = validation(active);
        if (errors.length) { showErrors(errors); return; }
        active += 1; render();
      } }));
    }
    actionHost.replaceChildren(...actions);
  }

  root.append(
    pageHead({
      title: campaign ? `Edit · ${campaign.name}` : 'New research campaign',
      sub: 'Configure a reusable, evidence-first buyer research workflow. Every behavioral setting remains visible.',
      actions: [button('Close', { kind: 'ghost', onClick: () => ctx.navigate(campaign ? `/admin/research/${campaign.id}` : '/admin/research') })],
    }),
    el('div', { class: 'ifz-research-editor' }, stepHost, formHost, actionHost),
  );
  render();
}
