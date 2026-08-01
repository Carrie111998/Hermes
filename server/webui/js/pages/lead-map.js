/* Lead Map — the flagship page.
   World country selection (max 5), country intelligence side panel,
   scan configuration modal → creates a lead scan + live agent run. */

import {
  el, icon, button, setBusy, toast, badge, modal, radioCards, chipSelect,
  field, input, emptyState,
} from '../ui.js';
import { call, ApiError } from '../api.js';
import { db } from '../mocks/db.js';
import { COUNTRY_NAMES, BUYER_INDUSTRIES, SCAN_DATA_SOURCES } from '../catalog.js';

/* ---------------- SVG loading + normalization ---------------- */
let _svgPromise = null;
function loadMapSvg() {
  if (!_svgPromise) {
    _svgPromise = fetch(new URL('../../assets/world.svg', import.meta.url))
      .then(r => {
        if (!r.ok) throw new Error(`world.svg failed to load (${r.status})`);
        return r.text();
      })
      .then(text => {
        const doc = new DOMParser().parseFromString(text, 'image/svg+xml');
        const svg = doc.querySelector('svg');
        if (!svg) throw new Error('world.svg contains no <svg> element');
        normalizeMap(svg);
        return svg;
      });
    _svgPromise.catch(() => { _svgPromise = null; }); // allow retry on failure
  }
  return _svgPromise;
}

function normalizeMap(svg, { fill = false } = {}) {
  svg.removeAttribute('width');
  svg.removeAttribute('height');
  // Full Lead Map uses slice to fill the stage; mini-map keeps meet so nothing crops.
  svg.setAttribute('preserveAspectRatio', fill ? 'xMidYMid slice' : 'xMidYMid meet');
  if (fill) {
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', 'World map of target markets. Click a country to inspect it.');
  }
  // Countries in this source are <path id="xx"> or <g id="xx"> (lowercase ISO
  // alpha-2). Non-ISO territories use "_name" ids — leave them unclassified.
  for (const node of svg.querySelectorAll('[id]')) {
    const nid = node.getAttribute('id');
    if (/^[a-z]{2}$/.test(nid)) {
      const code = nid.toUpperCase();
      node.dataset.iso = code;
      node.classList.add('country');
      // strip per-path fills so CSS owns the coloring
      node.removeAttribute('fill');
      node.querySelectorAll('[fill]').forEach(p => p.removeAttribute('fill'));
      node.removeAttribute('style');
    }
  }
}

function freshMapClone({ fill = false } = {}) {
  return loadMapSvg().then(svg => {
    const clone = svg.cloneNode(true);
    // Re-apply fill mode on the clone (source is normalized once with meet).
    clone.setAttribute('preserveAspectRatio', fill ? 'xMidYMid slice' : 'xMidYMid meet');
    if (fill) {
      clone.setAttribute('role', 'img');
      clone.setAttribute('aria-label', 'World map of target markets. Click a country to inspect it.');
      for (const node of clone.querySelectorAll('[data-iso]')) {
        const code = node.dataset.iso;
        node.setAttribute('tabindex', '0');
        node.setAttribute('role', 'button');
        node.setAttribute('aria-label', COUNTRY_NAMES[code] || code);
      }
    }
    return clone;
  });
}

/* ---------------- Dashboard mini-map ---------------- */
export async function renderMiniMap(container, countryScores = {}, selected = []) {
  try {
    const svg = await freshMapClone();
    const sel = new Set(selected);
    for (const node of svg.querySelectorAll('[data-iso]')) {
      const code = node.dataset.iso;
      if (sel.has(code)) node.classList.add('t2');
      else if ((countryScores[code] || 0) >= 70) node.classList.add('t1');
      node.style.cursor = 'default';
    }
    container.replaceChildren(svg);
  } catch (e) {
    console.error(e);
    container.replaceChildren(el('div', { class: 'ifz-hint' }, 'Map preview unavailable.'));
  }
}

/* ---------------- Main page ---------------- */
export async function mount(root, ctx) {
  let selected = [];
  let recommended = new Set();
  let maxSelected = 5;
  let activeCode = null;
  let disposed = false;

  const pageEl = root.closest('.ifz-page') || root;
  pageEl.classList.add('ifz-page--map');

  /* -- toolbar: left cluster + pinned CTA -- */
  const chipsHost = el('div', { class: 'ifz-map-chips' });
  const counter = el('span', { class: 'ifz-map-counter', 'aria-live': 'polite' });
  const scanBtn = button('Configure research', {
    kind: 'primary', icon: 'search',
    onClick: () => ctx.navigate(`/app/research/new?countries=${encodeURIComponent([...selected].join(','))}`),
    disabled: true,
  });
  const toolbar = el('div', { class: 'ifz-map-toolbar' },
    el('div', { class: 'ifz-map-toolbar-main' },
      el('span', { class: 'ifz-overline' }, 'Target markets'),
      chipsHost,
      counter),
    el('div', { class: 'ifz-map-toolbar-actions' }, scanBtn));

  /* -- map canvas (full-bleed) -- */
  const svgHost = el('div', { class: 'ifz-map-svghost' });
  const tooltip = el('div', { class: 'ifz-map-tooltip', role: 'tooltip' });
  const mapHint = el('div', { class: 'ifz-map-hint' },
    icon('map', 15),
    el('span', {}, 'Click a country to inspect it, then add it to your scan.'));
  const legend = el('div', { class: 'ifz-map-legend', 'aria-hidden': 'true' },
    el('span', {}, el('span', { class: 'ifz-legend-swatch', style: { background: 'var(--map-selected)' } }), 'Selected'),
    el('span', {}, el('span', { class: 'ifz-legend-swatch recommended' }), 'Recommended'),
    el('span', {}, el('span', { class: 'ifz-legend-swatch', style: { background: 'var(--map-land)' } }), 'Available'));
  const canvas = el('div', { class: 'ifz-map-canvas' }, svgHost, mapHint, legend, tooltip);

  /* -- side panel overlays map so canvas stays full width -- */
  const panelHost = el('div', { class: 'ifz-map-panel-host' });
  const stage = el('div', { class: 'ifz-map-stage' }, canvas, panelHost);

  root.append(el('div', { class: 'ifz-map-layout' }, toolbar, stage));

  /* ---------------- data + paint ---------------- */
  async function refreshSelection() {
    const res = await call('leadMap.selectedCountries');
    selected = res.items;
    maxSelected = res.max;
    paintToolbar();
    paintMap();
  }

  function paintToolbar() {
    const chipNodes = selected.length
      ? selected.map(code => el('span', { class: 'ifz-country-chip' },
          COUNTRY_NAMES[code] || code,
          el('button', {
            type: 'button',
            'aria-label': `Remove ${COUNTRY_NAMES[code] || code}`,
            onclick: () => deselect(code),
          }, '×')))
      : [];
    chipsHost.replaceChildren(...chipNodes);
    counter.replaceChildren(el('b', {}, String(selected.length)), ` of ${maxSelected} markets`);
    scanBtn.disabled = selected.length === 0;
    mapHint.hidden = selected.length > 0 || !!activeCode;
  }

  function paintMap() {
    const sel = new Set(selected);
    for (const node of svgHost.querySelectorAll('[data-iso]')) {
      const code = node.dataset.iso;
      const isSel = sel.has(code);
      const isRec = recommended.has(code) && !isSel;
      node.classList.toggle('selected', isSel);
      node.classList.toggle('recommended', isRec);
      node.classList.toggle('active', code === activeCode);
      const name = COUNTRY_NAMES[code] || code;
      let state = '';
      if (isSel) state = ', selected';
      else if (isRec) state = ', recommended';
      if (code === activeCode) state += ', viewing';
      node.setAttribute('aria-label', `${name}${state}`);
      node.setAttribute('aria-pressed', isSel ? 'true' : 'false');
    }
  }

  async function select(code) {
    try {
      const res = await call('leadMap.selectCountry', { body: { countries: [...selected, code] } });
      selected = res.items;
      paintToolbar(); paintMap();
      if (activeCode === code) openPanel(code); // refresh button state
    } catch (err) {
      if (err instanceof ApiError && err.code === 'max_countries') {
        toast(err.message, 'warning');
      } else {
        toast(err.message || 'Could not select country', 'error');
      }
    }
  }

  async function deselect(code) {
    await call('leadMap.deselectCountry', { params: { countryCode: code } });
    await refreshSelection();
    if (activeCode === code) openPanel(code);
  }

  /* ---------------- side panel ---------------- */
  function closePanel() {
    activeCode = null;
    panelHost.replaceChildren();
    paintMap();
    paintToolbar();
  }

  async function openPanel(code) {
    activeCode = code;
    mapHint.hidden = true;
    paintMap();
    panelHost.replaceChildren(el('div', { class: 'ifz-map-panel', role: 'dialog', 'aria-label': `${COUNTRY_NAMES[code] || code} intelligence` },
      el('div', { class: 'ifz-map-panel-body' },
        el('div', { class: 'ifz-hint' }, 'Loading country intelligence…'))));
    let summary;
    try {
      summary = await call('leadMap.countrySummary', { params: { countryCode: code } });
    } catch {
      closePanel();
      return;
    }
    if (disposed || activeCode !== code) return;

    const isSelected = selected.includes(code);
    const atCap = !isSelected && selected.length >= maxSelected;
    const toggleBtn = button(
      isSelected ? 'Remove from scan' : 'Add to scan',
      {
        kind: isSelected ? 'danger' : 'primary',
        icon: isSelected ? 'x' : 'plus',
        disabled: atCap,
        title: atCap ? `Maximum ${maxSelected} markets — remove one first` : undefined,
        onClick: () => (isSelected ? deselect(code) : select(code)),
      });

    const score = summary.opportunity_score;
    const scoreBadgeNode = el('span', {
      class: `ifz-score ${score >= 75 ? 'high' : score >= 50 ? 'mid' : 'low'}`,
      title: 'Opportunity score',
    }, `${score}`);

    panelHost.replaceChildren(el('div', { class: 'ifz-map-panel', role: 'dialog', 'aria-label': `${summary.name} intelligence` },
      el('div', { class: 'ifz-map-panel-head' },
        el('div', {},
          el('h2', { class: 'ifz-map-panel-country' }, summary.name),
          el('div', { class: 'ifz-row ifz-mt-2' },
            scoreBadgeNode,
            el('span', { class: 'ifz-small ifz-muted' }, 'opportunity'),
            summary.recommended ? badge('active', 'recommended') : null)),
        el('button', { class: 'ifz-modal-x', type: 'button', 'aria-label': 'Close panel', onclick: closePanel }, '×')),
      el('div', { class: 'ifz-map-panel-body' },
        el('div', {},
          el('div', { class: 'ifz-panel-section-title' }, 'Market'),
          el('div', { style: { lineHeight: 1.55 } }, summary.market_size)),
        el('div', {},
          el('div', { class: 'ifz-panel-section-title' }, 'Trade note'),
          el('div', { class: 'ifz-muted', style: { lineHeight: 1.55 } }, summary.trade_note)),
        summary.top_industries.length ? el('div', {},
          el('div', { class: 'ifz-panel-section-title' }, 'Top buyer industries'),
          el('div', { class: 'ifz-taglist' }, summary.top_industries.map(t => el('span', { class: 'ifz-tag' }, t)))) : null,
        summary.recommended_products.length ? el('div', {},
          el('div', { class: 'ifz-panel-section-title' }, 'Product-market fit'),
          summary.recommended_products.map(p =>
            el('div', { class: 'ifz-fitrow' },
              el('span', { class: 'name' }, p.name),
              el('span', { class: `ifz-score ${p.fit >= 85 ? 'high' : 'mid'}` }, p.fit)))) : null,
        el('div', {},
          el('div', { class: 'ifz-panel-section-title' }, 'Existing pipeline'),
          summary.existing_leads
            ? el('a', { href: `#/app/leads?country=${code}` }, `${summary.existing_leads} leads already discovered →`)
            : el('span', { class: 'ifz-muted' }, 'No leads in this market yet.'))),
      el('div', { class: 'ifz-map-panel-foot' }, toggleBtn)));
  }

  /* ---------------- scan modal ---------------- */
  function openScanModal() {
    if (!selected.length) return;
    const nameInput = input({ value: `Scan — ${selected.map(c => COUNTRY_NAMES[c] || c).join(', ')}` });
    const depthCards = radioCards([
      { value: 'quick', title: 'Quick', desc: 'Top directories only. Fast market taste-test.', meta: '~5 leads / country · ~1 min' },
      { value: 'standard', title: 'Standard', desc: 'Directories + exhibitor lists.', meta: '~8 leads / country · ~2 min' },
      { value: 'deep', title: 'Deep', desc: 'All sources, wider net, more research per lead.', meta: '~12 leads / country · ~4 min' },
    ], 'standard');
    const sourceChips = chipSelect(SCAN_DATA_SOURCES, ['web_search', 'exhibitor_lists']);
    const productChips = chipSelect(db.products.map(p => ({ value: p.id, label: p.name })), db.products.slice(0, 3).map(p => p.id));
    const industryChips = chipSelect(BUYER_INDUSTRIES, BUYER_INDUSTRIES.slice(0, 3));
    const leadCount = leadCountStepper(8);

    const startBtn = button('Start scan', { kind: 'primary', icon: 'bolt' });
    const m = modal({
      title: `Configure lead scan — ${selected.length} market${selected.length > 1 ? 's' : ''}`,
      wide: true,
      body: el('div', {},
        el('div', { class: 'ifz-row wrap ifz-mb-4' }, selected.map(c => el('span', { class: 'ifz-country-chip' }, COUNTRY_NAMES[c] || c))),
        field('Scan name', nameInput),
        field('Scan depth', depthCards),
        field('Data sources', sourceChips, { hint: 'Where the agent hunts for buyer companies.' }),
        field('Products to match', productChips, { hint: 'Leads are scored against the selected products.' }),
        field('Buyer industries', industryChips),
        field('Leads per country', leadCount, { hint: 'Maximum qualified companies to add per selected market.' })),
      actions: [
        button('Cancel', { onClick: () => m.close() }),
        startBtn,
      ],
    });

    startBtn.addEventListener('click', async () => {
      if (!sourceChips.getSelected().length) { toast('Pick at least one data source', 'warning'); return; }
      setBusy(startBtn, true, 'Starting…');
      try {
        const scan = await call('leadScans.create', {
          body: {
            name: nameInput.value,
            countries: selected,
            depth: depthCards.getSelected(),
            sources: sourceChips.getSelected(),
            products: productChips.getSelected(),
            industries: industryChips.getSelected(),
            leads_per_country: leadCount.getValue(),
          },
        });
        const res = await call('leadScans.start', { params: { scanId: scan.id } });
        m.close();
        toast('Lead scan started — the agent is hunting.', 'success', {
          actionLabel: 'View run',
          onAction: () => ctx.navigate(`/app/agent-runs/${res.run_id}`),
          duration: 7000,
        });
      } catch (err) {
        setBusy(startBtn, false);
        toast(err.message || 'Could not start scan', 'error');
      }
    });
  }

  /* ---------------- map interactions ---------------- */
  function activateCountry(code, { directToggle = false } = {}) {
    if (directToggle) {
      if (selected.includes(code)) deselect(code); else select(code);
    } else {
      openPanel(code);
    }
  }

  function wireMap(svg) {
    svg.addEventListener('pointermove', (e) => {
      const target = e.target.closest('[data-iso]');
      if (!target) { tooltip.classList.remove('show'); return; }
      const code = target.dataset.iso;
      tooltip.textContent = `${COUNTRY_NAMES[code] || code}${selected.includes(code) ? ' · selected' : ''}`;
      const rect = canvas.getBoundingClientRect();
      tooltip.style.left = `${e.clientX - rect.left}px`;
      tooltip.style.top = `${e.clientY - rect.top}px`;
      tooltip.classList.add('show');
    });
    svg.addEventListener('pointerleave', () => tooltip.classList.remove('show'));
    svg.addEventListener('click', (e) => {
      const target = e.target.closest('[data-iso]');
      if (!target) return;
      activateCountry(target.dataset.iso, { directToggle: e.ctrlKey || e.metaKey });
    });
    svg.addEventListener('keydown', (e) => {
      const target = e.target.closest('[data-iso]');
      if (!target) return;
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        activateCountry(target.dataset.iso, { directToggle: e.ctrlKey || e.metaKey });
      }
    });
  }

  document.addEventListener('keydown', onEsc);
  function onEsc(e) {
    if (e.key === 'Escape' && activeCode) closePanel();
  }

  /* ---------------- boot ---------------- */
  try {
    const [svg, countriesRes] = await Promise.all([
      freshMapClone({ fill: true }), call('leadMap.countries'), call('products.list'),
    ]);
    if (disposed) return () => { disposed = true; pageEl.classList.remove('ifz-page--map'); document.removeEventListener('keydown', onEsc); };
    recommended = new Set(countriesRes.items.filter(c => c.recommended).map(c => c.code));
    svgHost.replaceChildren(svg);
    wireMap(svg);
    await refreshSelection();
  } catch (e) {
    console.error(e);
    svgHost.replaceChildren(emptyState({
      icon: 'map',
      title: 'World map failed to load',
      hint: String(e.message || e),
      action: button('Retry', { kind: 'primary', onClick: () => { root.replaceChildren(); mount(root, ctx); } }),
    }));
  }

  return () => {
    disposed = true;
    pageEl.classList.remove('ifz-page--map');
    document.removeEventListener('keydown', onEsc);
  };
}

function leadCountStepper(initialValue) {
  let value = initialValue;
  const valueNode = el('span', { class: 'ifz-stepnum-value' }, String(value));
  const host = el('div', { class: 'ifz-stepnum', role: 'group', 'aria-label': 'Leads per country' });
  const dec = button('-', { size: 'sm', onClick: () => setValue(value - 1) });
  const inc = button('+', { size: 'sm', onClick: () => setValue(value + 1) });
  function setValue(next) {
    value = Math.max(3, Math.min(20, Number(next) || initialValue));
    valueNode.textContent = String(value);
    dec.disabled = value <= 3;
    inc.disabled = value >= 20;
  }
  host.append(dec, valueNode, inc);
  host.getValue = () => value;
  setValue(value);
  return host;
}
