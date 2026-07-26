import { button, el, field, input } from '../ui.js';

export const SCORE_DIMENSIONS = [
  ['product_sector_fit', 'Product and sector fit', 'Category, HS/product and buyer assortment alignment.'],
  ['buyer_channel_fit', 'Buyer and channel fit', 'Importer, distributor, retailer or procurement role evidence.'],
  ['buying_intent', 'Buying intent', 'Sourcing briefs, tenders, hosted-buyer or meeting-goal evidence.'],
  ['market_coverage', 'Market coverage', 'Countries served, locations and route-to-market evidence.'],
  ['commercial_scale', 'Commercial scale and capacity', 'Stores, facilities, workforce and supported financial claims.'],
  ['trade_activity', 'Relevant trade activity', 'Company-specific trade evidence, never aggregate market totals.'],
  ['contactability', 'Contactability', 'Reachable company channels; downstream contact discovery remains separate.'],
];

export function weightTotal(weights) {
  return Object.values(weights || {}).reduce((sum, value) => sum + Number(value || 0), 0);
}

export function renderScoring(scoring, { onChange } = {}) {
  const weights = { ...scoring.weights };
  const totalNode = el('strong', { class: 'ifz-weight-total', 'aria-live': 'polite' });
  function announce() {
    const total = weightTotal(weights);
    totalNode.textContent = `${total} / 100`;
    totalNode.classList.toggle('invalid', total !== 100);
    onChange?.({ ...scoring, weights: { ...weights } });
  }
  const grid = el('div', { class: 'ifz-weight-grid' }, SCORE_DIMENSIONS.map(([key, label, hint]) => {
    const control = input({ type: 'number', min: 0, max: 100, value: weights[key], name: `weight-${key}` });
    control.addEventListener('input', () => { weights[key] = Number(control.value); announce(); });
    return field(label, control, { hint });
  }));
  const restore = button('Restore defaults', { kind: 'ghost', size: 'sm', onClick: () => {
    const defaults = [25, 20, 15, 15, 10, 10, 5];
    SCORE_DIMENSIONS.forEach(([key], index) => { weights[key] = defaults[index]; });
    grid.querySelectorAll('input').forEach((control, index) => { control.value = defaults[index]; });
    announce();
  } });
  announce();
  return el('div', {},
    el('div', { class: 'ifz-research-inline-head' },
      el('div', {}, el('div', { class: 'ifz-overline' }, scoring.name || 'Scoring profile'),
        el('p', { class: 'ifz-hint' }, 'Fit score measures business relevance. Evidence confidence is calculated separately.')),
      el('div', { class: 'ifz-weight-control' }, totalNode, restore)),
    grid);
}
