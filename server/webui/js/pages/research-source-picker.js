import { badge, button, el } from '../ui.js';

const GROUP_LABELS = {
  trade: 'Government trade & market data', registry: 'Company registries & filings',
  procurement: 'Procurement & buying opportunities', exhibition: 'Exhibitions & official directories',
  matchmaking: 'Matchmaking & hosted buyers', licensed: 'Licensed databases',
  customer_upload: 'Customer uploads', opportunity: 'Buying opportunities',
};

function correctiveLabel(source) {
  if (source.access_tier === 'customer_upload') return 'Upload data';
  if (source.access_tier === 'credentialed_public' || source.access_tier === 'licensed') return 'Configure access';
  return 'Ask admin';
}

export function renderSourcePicker(sources, selected, { onChange } = {}) {
  const selection = new Set(selected);
  const groups = new Map();
  for (const source of sources) {
    // ponytail: licensed databases are hidden, not disabled — no deal, nothing to configure.
    if (source.access_tier === 'licensed' || source.categories?.[0] === 'licensed') continue;
    const group = source.categories?.[0] || 'other';
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(source);
  }
  const host = el('div', { class: 'ifz-research-source-groups' });
  for (const [group, rows] of groups) {
    host.append(el('section', { class: 'ifz-research-source-group' },
      el('div', { class: 'ifz-research-section-kicker' }, GROUP_LABELS[group] || group.replace(/_/g, ' ')),
      rows.map(source => {
        const checked = selection.has(source.source_id);
        const checkbox = el('input', {
          type: 'checkbox', checked, disabled: !source.available,
          'aria-label': `Select ${source.display_name}`,
        });
        checkbox.addEventListener('change', () => {
          if (checkbox.checked) selection.add(source.source_id); else selection.delete(source.source_id);
          onChange?.([...selection]);
        });
        return el('article', { class: `ifz-research-source ${checked ? 'selected' : ''}` },
          el('div', { class: 'ifz-research-source-check' }, checkbox),
          el('div', { class: 'ifz-research-source-main' },
            el('div', { class: 'ifz-research-source-title' }, source.display_name),
            el('div', { class: 'ifz-research-source-publisher' }, source.publisher),
            el('div', { class: 'ifz-research-source-meta' },
              badge(source.health, source.health),
              el('span', {}, source.access_tier.replace(/_/g, ' ')),
              el('span', {}, (source.entity_levels || []).join(' · ')),
              el('span', {}, `${source.freshness_days}d freshness`))),
          el('div', { class: 'ifz-research-source-side' },
            source.available
              ? el('span', { class: 'ifz-evidence-state' }, 'Available')
              : button(correctiveLabel(source), { kind: 'ghost', size: 'sm', disabled: true }),
            source.unavailable_reason ? el('span', { class: 'ifz-hint' }, source.unavailable_reason.replace(/_/g, ' ')) : null));
      })));
  }
  return host;
}
