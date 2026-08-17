import { button, el } from '../ui.js';

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

export function transferWeight(weights, key, delta) {
  const next = { ...(weights || {}) };
  if (!SCORE_DIMENSIONS.some(([dimension]) => dimension === key) || ![5, -5].includes(delta)) return next;
  const value = Number(next[key]);
  if (!Number.isInteger(value) || value < 0 || value > 100 || value % 5) return next;
  const others = SCORE_DIMENSIONS.map(([dimension], index) => ({
    key: dimension, index, value: Number(next[dimension]),
  })).filter(item => item.key !== key && Number.isInteger(item.value) && item.value >= 0 && item.value <= 100 && item.value % 5 === 0);
  if (others.length !== SCORE_DIMENSIONS.length - 1) return next;
  const eligible = delta === 5
    ? others.filter(item => item.value >= 5).sort((a, b) => b.value - a.value || a.index - b.index)
    : others.filter(item => item.value <= 95).sort((a, b) => a.value - b.value || a.index - b.index);
  if ((delta === 5 && value > 95) || (delta === -5 && value < 5) || !eligible.length) return next;
  const counterpart = eligible[0];
  next[key] = value + delta;
  next[counterpart.key] = counterpart.value - delta;
  return next;
}

export function renderScoring(scoring, { onChange } = {}) {
  const weights = { ...scoring.weights };
  const totalNode = el('strong', { class: 'ifz-weight-total', 'aria-live': 'polite' });
  const changeNode = el('div', { class: 'ifz-sr-only', role: 'status', 'aria-live': 'polite' });
  const rows = new Map();
  const controls = new Map();
  function refresh() {
    const total = weightTotal(weights);
    totalNode.textContent = `${total} / 100`;
    totalNode.classList.toggle('invalid', total !== 100);
    for (const [key, control] of controls) {
      control.value.textContent = `${weights[key]}%`;
      control.decrease.disabled = Object.keys(transferWeight(weights, key, -5))
        .every(dimension => transferWeight(weights, key, -5)[dimension] === weights[dimension]);
      control.increase.disabled = Object.keys(transferWeight(weights, key, 5))
        .every(dimension => transferWeight(weights, key, 5)[dimension] === weights[dimension]);
    }
  }
  function emit() {
    onChange?.({ ...scoring, weights: { ...weights } });
  }
  function transfer(key, delta) {
    const before = { ...weights };
    const next = transferWeight(weights, key, delta);
    const changed = SCORE_DIMENSIONS
      .filter(([dimension]) => next[dimension] !== before[dimension])
      .map(([dimension, label]) => [dimension, label]);
    if (changed.length !== 2) return;
    Object.assign(weights, next);
    refresh();
    const direction = delta === 5 ? 'increased' : 'decreased';
    const counterpartDirection = delta === 5 ? 'decreased' : 'increased';
    changeNode.textContent = `${changed.find(([dimension]) => dimension === key)[1]} ${direction} to ${weights[key]}%; ${changed.find(([dimension]) => dimension !== key)[1]} ${counterpartDirection} to ${weights[changed.find(([dimension]) => dimension !== key)[0]]}%.`;
    for (const [dimension] of changed) {
      const row = rows.get(dimension);
      row.classList.remove('changed');
      void row.offsetWidth;
      row.classList.add('changed');
      setTimeout(() => row.classList.remove('changed'), 700);
    }
    emit();
  }
  const grid = el('div', { class: 'ifz-weight-grid' }, SCORE_DIMENSIONS.map(([key, label, hint]) => {
    const value = el('output', { class: 'ifz-weight-value', 'aria-label': `${label} weight` });
    const decrease = el('button', {
      class: 'ifz-btn ghost sm ifz-weight-step', type: 'button', 'aria-label': `Decrease ${label} by 5`,
      onclick: () => transfer(key, -5),
    }, '-5');
    const increase = el('button', {
      class: 'ifz-btn ghost sm ifz-weight-step', type: 'button', 'aria-label': `Increase ${label} by 5`,
      onclick: () => transfer(key, 5),
    }, '+5');
    const row = el('div', { class: 'ifz-weight-row', role: 'group', 'aria-label': label },
      el('div', { class: 'ifz-weight-copy' }, el('div', { class: 'ifz-label' }, label), el('div', { class: 'ifz-hint' }, hint)),
      el('div', { class: 'ifz-weight-actions' }, decrease, value, increase));
    rows.set(key, row);
    controls.set(key, { decrease, value, increase });
    return row;
  }));
  const restore = button('Restore defaults', { kind: 'ghost', size: 'sm', onClick: () => {
    const defaults = [25, 20, 15, 15, 10, 10, 5];
    SCORE_DIMENSIONS.forEach(([key], index) => { weights[key] = defaults[index]; });
    refresh();
    changeNode.textContent = 'Scoring weights restored to their defaults.';
    emit();
  } });
  refresh();
  return el('div', {},
    el('div', { class: 'ifz-research-inline-head' },
      el('div', {}, el('div', { class: 'ifz-overline' }, scoring.name || 'Scoring profile'),
        el('p', { class: 'ifz-hint' }, 'Fit score measures business relevance. Evidence confidence is calculated separately.')),
      el('div', { class: 'ifz-weight-control' }, totalNode, restore)),
    changeNode, grid);
}
