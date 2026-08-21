/* Customer lead-search brief: say what to look for and how to weigh it,
   before anything is searched.

   Source access stays admin-owned — a customer cannot install or enable a
   provider — so this page never asks which sources to use. It runs every
   source the tenant already has and says which ones those are. What the
   customer does own is the question: which markets, which sector, and what a
   good lead means to them, which is exactly what the weights encode. */

import { call } from '../api.js';
import {
  button, card, chipSelect, el, emptyState, field, pageHead, select, setBusy, toast,
} from '../ui.js';
import { countryName } from './_page-utils.js';
import { renderScoring, weightTotal } from './research-scoring.js';

const itemsOf = value => (Array.isArray(value) ? value : value?.items || []);

// Enough markets to be a real brief, few enough to finish while someone
// watches. The API's own ceiling is 25.
const MAX_MARKETS = 10;

function defaultWeights() {
  return {
    product_sector_fit: 25,
    buyer_channel_fit: 20,
    buying_intent: 15,
    market_coverage: 15,
    commercial_scale: 10,
    trade_activity: 10,
    contactability: 5,
  };
}

function sourceSummary(sources) {
  const ready = sources.filter(source => source.available);
  if (!ready.length) {
    return {
      ok: false,
      node: el('p', { class: 'ifz-hint' },
        'No research source is connected yet. An administrator has to enable one before a search can find anything.'),
    };
  }
  return {
    ok: true,
    ids: ready.map(source => source.source_id),
    node: el('p', { class: 'ifz-hint' },
      `Searching ${ready.map(source => source.display_name).join(', ')}.`),
  };
}

export async function mount(root, ctx) {
  const [catalog, sectors, configuration, selected] = await Promise.all([
    call('dataSources.catalog').catch(() => []),
    call('research.sectors').catch(() => []),
    call('research.configuration').catch(() => ({})),
    call('leadMap.selectedCountries').catch(() => []),
  ]);

  const sources = sourceSummary(itemsOf(catalog));
  const sectorList = itemsOf(sectors);
  const countryCodes = itemsOf(selected)
    .map(entry => entry.country || entry.code || entry)
    .filter(code => typeof code === 'string' && code.length === 2);

  const brief = {
    name: '',
    sector_id: sectorList[0]?.sector_id || '',
    markets: countryCodes.slice(0, MAX_MARKETS),
    weights: defaultWeights(),
    research_each_lead: true,
  };

  const nameInput = el('input', { type: 'text', placeholder: 'Nordic distributors, Q3' });
  nameInput.addEventListener('input', () => { brief.name = nameInput.value; });

  const sectorSelect = select(
    sectorList.map(sector => ({ value: sector.sector_id, label: sector.name })),
    { value: brief.sector_id },
  );
  sectorSelect.addEventListener('change', () => { brief.sector_id = sectorSelect.value; });

  const marketChips = chipSelect(
    countryCodes.map(code => ({ value: code, label: countryName(code) || code })),
    brief.markets,
    {
      max: MAX_MARKETS,
      onChange: value => { brief.markets = value; },
      onLimit: max => toast(`${max} markets at a time keeps a search finishable. Remove one to add another.`, 'info'),
    },
  );

  const deepToggle = el('input', { type: 'checkbox', checked: 'checked' });
  deepToggle.addEventListener('change', () => { brief.research_each_lead = deepToggle.checked; });

  const run = button('Run lead search', { kind: 'primary' });

  async function submit() {
    if (!sources.ok) return;
    if (!brief.markets.length) return toast('Choose at least one market.', 'error');
    if (!brief.sector_id) return toast('Choose a sector.', 'error');
    if (weightTotal(brief.weights) !== 100) return toast('Weights must total exactly 100.', 'error');

    const sector = sectorList.find(item => item.sector_id === brief.sector_id);
    setBusy(run, true, 'Searching…');
    try {
      const campaign = await call('researchCampaigns.create', {
        body: {
          config: {
            name: brief.name.trim() || `${sector?.name || 'Lead'} search`,
            seller_countries: configuration.default_seller_countries || ['TR'],
            target_countries: brief.markets,
            sector_ids: [brief.sector_id],
            // Buyer roles come from the sector rather than a free-text box:
            // eligibility intersects this list with what evidence actually
            // says, so a term nobody publishes silently rejects everyone.
            buyer_types: sector?.buyer_types?.length ? sector.buyer_types : undefined,
            enabled_source_ids: sources.ids,
            scoring: { weights: brief.weights },
            enrichment: { research_each_lead: brief.research_each_lead },
          },
        },
      });
      await call('researchCampaigns.start', { params: { campaignId: campaign.id } });
      // Queued, not finished: the search runs in the background and each
      // company is verified before it appears. Claiming it had finished sent
      // people to an empty list and read as a failed search.
      toast('Search started. Results appear as companies are verified.', 'success');
      ctx.navigate('/app/research');
    } catch (error) {
      toast(error?.message || 'The search could not start.', 'error');
    } finally {
      setBusy(run, false);
    }
  }
  run.addEventListener('click', submit);
  run.disabled = !sources.ok;

  root.replaceChildren(
    pageHead({
      title: 'New lead search',
      sub: 'Set what you are looking for and how much each signal counts. Nothing is searched until you run it.',
      actions: button('Back to results', { kind: 'ghost', onClick: () => ctx.navigate('/app/research') }),
    }),
    el('div', { class: 'ifz-research-stack' },
      card({
        title: 'What to look for',
        body: el('div', { class: 'ifz-form-grid' },
          field('Name this search', nameInput, { hint: 'Optional. Helps you find it again.' }),
          field('Sector', sectorSelect, { hint: 'Decides the buyer roles and the evidence worth chasing.', required: true }),
          field('Markets', marketChips.children.length
            ? marketChips
            : emptyState({
                icon: 'globe',
                title: 'No markets selected yet',
                hint: 'Choose target markets in Setup first; a search needs somewhere to look.',
                action: button('Go to Setup', { onClick: () => ctx.navigate('/app/setup') }),
              }),
            { hint: `Up to ${MAX_MARKETS} at a time.`, required: true }),
        ),
      }),
      card({
        title: 'What makes a good lead',
        body: el('div', {},
          el('p', { class: 'ifz-hint' },
            'These weights decide the ranking, not the verdict. Evidence still has to exist before a company scores at all.'),
          renderScoring({ weights: brief.weights }, { onChange: value => { brief.weights = value.weights; } })),
      }),
      card({
        title: 'How deep to go',
        body: el('div', {},
          el('label', { class: 'ifz-row' }, deepToggle,
            el('span', {}, 'Research each lead after it matches')),
          el('p', { class: 'ifz-hint' },
            'Runs a second pass per company aimed at what the first one could not establish — scale, brands carried, trade activity. Slower, and the reason a result can reach strong fit.'),
          sources.node),
      }),
      el('div', { class: 'ifz-row' }, run)),
  );
}
