/* World map component.
   Phase 5 removed the standalone Lead Map page, and the Buyers page that
   replaced it went with the research workspace. Country selection now lives as
   the target-markets step inside Setup, with the map itself surviving as the
   shared renderer imported by today.js and setup.js. */

import { el } from '../ui.js';
import { COUNTRY_NAMES } from '../catalog.js';

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

/* ---------------- Shared map renderer ---------------- */
export async function renderMiniMap(
  container,
  countryScores = {},
  selected = [],
  { active = '', interactive = [], onSelect } = {},
) {
  try {
    const svg = await freshMapClone();
    const sel = new Set(selected);
    const interactiveCodes = new Set(interactive);
    for (const node of svg.querySelectorAll('[data-iso]')) {
      const code = node.dataset.iso;
      if (sel.has(code)) node.classList.add('t2');
      else if ((countryScores[code] || 0) >= 70) node.classList.add('t1');
      if (active === code) node.classList.add('active');
      const canSelect = Boolean(onSelect) && interactiveCodes.has(code);
      node.style.cursor = canSelect ? 'pointer' : 'default';
      if (canSelect) {
        const name = COUNTRY_NAMES[code] || code;
        node.setAttribute('tabindex', '0');
        node.setAttribute('role', 'button');
        node.setAttribute('aria-label', `Show buyers in ${name}`);
        node.setAttribute('aria-pressed', active === code ? 'true' : 'false');
        node.addEventListener('click', () => onSelect(code));
        node.addEventListener('keydown', event => {
          if (event.key !== 'Enter' && event.key !== ' ') return;
          event.preventDefault();
          onSelect(code);
        });
      }
    }
    container.replaceChildren(svg);
  } catch (e) {
    console.error(e);
    container.replaceChildren(el('div', { class: 'ifz-hint' }, 'Map preview unavailable.'));
  }
}
