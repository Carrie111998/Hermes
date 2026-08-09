/* Caret — the state indicator.

   The blue underscore is the Interfaze house mark (DESIGN.json,
   "The Cursor Rule"). Here it does a second job: it reports what the
   agent is doing. One object, four states, each bound to something
   true. Nothing about it is decorative.

     idle     dim, still          nothing is running
     working  extends/retracts    a run is in progress
     waiting  blinks              needs a human — the only state
                                  that asks for attention
     sent     solid green, fades  mail left the building

   Deliberately not a glowing orb: the house donts rule out
   "glowing accent on black". The caret carries the same idea and
   scales down to a table row, a tab, and the favicon.
*/

import { el } from './ui.js';

const STATES = ['idle', 'working', 'waiting', 'sent'];

/* Copy is written for the operator, not the system. The machine
   reports; it never says "I". */
const DEFAULT_LABELS = {
  idle: 'Idle',
  working: 'Working',
  waiting: 'Awaiting your approval',
  sent: 'Sent',
};

function prefersReducedMotion() {
  return typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/**
 * Create a caret indicator.
 *
 * @param {object}  [options]
 * @param {string}  [options.state='idle']   one of idle | working | waiting | sent
 * @param {string}  [options.label]          overrides the default copy for the state
 * @param {number}  [options.progress]       0..1, only meaningful while working
 * @param {boolean} [options.showLabel=true] render the text beside the caret
 * @param {string}  [options.size='md']      sm | md | lg
 * @param {boolean} [options.live=false]     announce state changes to screen
 *   readers. Off by default: most pages already own a live region, and two
 *   polite regions on one page double-announce. Turn it on only when the
 *   caret is the sole reporter of state.
 * @returns {{ el: HTMLElement, set: Function, state: Function, destroy: Function }}
 */
export function createCaret({
  state = 'idle',
  label = null,
  progress = null,
  showLabel = true,
  size = 'md',
  live = false,
} = {}) {
  const bar = el('i', { class: 'ifz-caret-bar', 'aria-hidden': 'true' });
  const text = showLabel ? el('span', { class: 'ifz-caret-label' }) : null;

  const root = el('span', {
    class: `ifz-caret ifz-caret--${size}`,
    role: live ? 'status' : null,
    'aria-live': live ? 'polite' : null,
  }, bar, text);

  let current = null;

  function set(nextState, opts = {}) {
    const next = STATES.includes(nextState) ? nextState : 'idle';
    const nextProgress = opts.progress == null ? null : opts.progress;
    const nextLabel = opts.label != null
      ? opts.label
      : (next === current ? null : DEFAULT_LABELS[next]);

    if (next !== current) {
      root.dataset.state = next;
      current = next;
    }

    /* Length tracks real progress through the market list. When we
       don't know the fraction, fall back to the CSS sweep rather than
       inventing a number the user would read as fact. */
    if (next === 'working' && nextProgress != null) {
      const pct = Math.max(0, Math.min(1, nextProgress));
      root.style.setProperty('--caret-fill', String(pct));
      root.dataset.determinate = 'true';
    } else {
      root.style.removeProperty('--caret-fill');
      delete root.dataset.determinate;
    }

    if (text && nextLabel != null) text.textContent = nextLabel;
    return api;
  }

  function destroy() {
    root.remove();
  }

  const api = { el: root, set, state: () => current, destroy };

  set(state, { label, progress });
  if (prefersReducedMotion()) root.dataset.reducedMotion = 'true';
  return api;
}

/**
 * Standalone blinking caret for headline use — the house Cursor Rule
 * flourish. Static mark, no state reporting.
 *
 * @param {object} [options]
 * @param {boolean} [options.blink=true]
 */
export function headlineCaret({ blink = true } = {}) {
  return el('i', {
    class: `ifz-caret-mark${blink ? ' is-blinking' : ''}`,
    'aria-hidden': 'true',
  });
}
