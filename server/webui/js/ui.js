/* Shared UI primitives — plain functions returning DOM nodes.
   Each maps 1:1 to a future React component. */

import { ICON_PATHS } from './icons.js';

/* ---------- DOM helper ---------- */
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2).toLowerCase(), v);
    else if (v === true) node.setAttribute(k, '');
    else node.setAttribute(k, v);
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

/* ---------- Icons (Heroicons Solid) ---------- */
export function icon(name, size = 16) {
  const paths = ICON_PATHS[name] || ICON_PATHS.sparkle;
  const dList = Array.isArray(paths) ? paths : [paths];
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', size);
  svg.setAttribute('height', size);
  svg.setAttribute('fill', 'currentColor');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  for (const d of dList) {
    const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    p.setAttribute('d', d);
    svg.append(p);
  }
  return svg;
}

/* ---------- Formatters ---------- */
export const fmt = {
  num(n) { return (n ?? 0).toLocaleString('en-US'); },
  pct(n) { return `${Math.round(n)}%`; },
  date(iso) {
    if (!iso) return '—';
    const value = typeof iso === 'number' && iso < 100000000000 ? iso * 1000 : iso;
    return new Date(value).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
  },
  time(iso) {
    if (!iso) return '—';
    const value = typeof iso === 'number' && iso < 100000000000 ? iso * 1000 : iso;
    return new Date(value).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
  },
  ago(iso) {
    if (!iso) return '—';
    const value = typeof iso === 'number' && iso < 100000000000 ? iso * 1000 : iso;
    const s = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000);
    if (s < 60) return 'just now';
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    if (s < 86400 * 30) return `${Math.floor(s / 86400)}d ago`;
    return fmt.date(iso);
  },
};

/* ---------- Card ---------- */
export function card({ title, actions, body, flush = false, class: cls } = {}) {
  const children = [];
  if (title || actions) {
    children.push(el('div', { class: 'ifz-card-head' },
      el('div', { class: 'ifz-card-title' }, title || ''),
      actions ? el('div', { class: 'ifz-row' }, actions) : null));
  }
  children.push(el('div', { class: `ifz-card-body${flush ? ' flush' : ''}` }, body));
  return el('div', { class: `ifz-card${cls ? ' ' + cls : ''}` }, children);
}

/* ---------- Stat card ---------- */
export function statCard({ label, value, delta, deltaDir = 'flat', spark }) {
  const node = el('div', { class: 'ifz-card ifz-stat' },
    el('div', { class: 'ifz-overline' }, label),
    el('div', { class: 'ifz-stat-value' }, value),
    delta != null ? el('div', { class: `ifz-stat-delta ${deltaDir}` }, delta) : null);
  if (spark && spark.length > 1) node.append(el('div', { class: 'ifz-stat-spark' }, sparkline(spark)));
  return node;
}

/* ---------- Badges ---------- */
const STATUS_KIND = {
  // generic
  active: 'success', completed: 'success', connected: 'success', verified: 'success', approved: 'success',
  processed: 'success',
  sent: 'success', sent_manually: 'success', replied: 'accent', interested: 'accent', done: 'success',
  running: 'info', processing: 'info', generating: 'info', queued: 'neutral', in_progress: 'info',
  draft_created: 'info',
  qualified: 'info', researched: 'info', contacted: 'info',
  pending: 'warning', pending_approval: 'warning', awaiting_approval: 'warning', partially_sent: 'warning',
  draft_generated: 'warning', unverified: 'warning', paused: 'warning',
  not_connected: 'neutral', not_configured: 'neutral', not_built: 'neutral',
  new: 'neutral', draft: 'neutral', archived: 'neutral', uploaded: 'neutral',
  opted_out: 'neutral',
  failed: 'error', error: 'error', qa_failed: 'error', paused_bounce_rate: 'error',
  cancelled: 'error', do_not_contact: 'error', not_found: 'error', lost: 'error',
};

/* Customer-facing language for persisted/API status values. Keep this table
   at the shared boundary so pages never need to print database enums. */
export const LABELS = Object.freeze({
  active: 'Active',
  completed: 'Completed',
  connected: 'Connected',
  verified: 'Verified',
  processed: 'Ready to use',
  approved: 'Approved',
  sent: 'Sent',
  sent_manually: 'You sent this',
  replied: 'They replied',
  interested: 'Interested',
  done: 'Complete',
  running: 'In progress',
  processing: 'In progress',
  generating: 'Generating messages',
  queued: 'Waiting',
  in_progress: 'In progress',
  pending: 'Pending',
  pending_approval: 'Waiting for you',
  awaiting_approval: 'Waiting for you',
  draft_generated: 'Waiting for you',
  draft: 'Saved as a draft',
  draft_created: 'Saved as a draft',
  unverified: 'Not verified',
  paused: 'Paused',
  paused_bounce_rate: 'Sending paused — too many bounces',
  not_connected: 'Not connected',
  not_configured: 'Not configured',
  not_built: 'Not built yet',
  uploaded: 'Uploaded',
  partially_sent: 'Partly sent',
  new: 'Not looked at yet',
  qualified: 'Worth approaching',
  researched: 'Researched',
  contacted: 'Emailed',
  archived: 'Set aside',
  qa_failed: 'Needs a second look',
  opted_out: 'Asked not to be contacted',
  do_not_contact: 'Never contact',
  unqualified_after_source_removal: 'No longer supported by its source',
  failed: 'Failed',
  error: 'Error',
  cancelled: 'Cancelled',
  not_found: 'Not found',
  lost: 'Lost',
  blocked: 'Blocked',
  access_pending: 'Access pending',
  enabled: 'Enabled',
  disabled: 'Disabled',
  suspended: 'Suspended',
  partial: 'Partly completed',
  succeeded: 'Completed',
  observed: 'Verified',
  estimated: 'Estimated',
  unknown: 'Not known',
  profile_found: 'Profile found',
  connection_sent: 'Connection sent',
  note_generated: 'Note ready',
  opened: 'Opened',
});

export const QA_LABELS = Object.freeze({
  missing_recipient: "The email address is missing.",
  missing_body_or_template: "The message itself is missing.",
  subject_mismatch: "The subject line doesn't match your standard subject.",
  operator_language_contamination: 'Some Turkish slipped into a non-Turkish email.',
  unresolved_placeholder: "A blank didn't get filled in.",
  unknown_placeholder: "A blank didn't get filled in.",
  invalid_or_missing_recipient: "The email address doesn't look right.",
  invalid_cc_recipient: "One of the CC addresses doesn't look right.",
  double_dash: "Contains a double dash, which your style rules don't allow.",
  internal_marker: 'An internal note was left in the text.',
  unapproved_link: "Contains a link that isn't on your approved list.",
});

export const APPROVAL_STATUSES = Object.freeze([
  'pending_approval',
  'draft_generated',
  'qa_failed',
  'approved',
]);

/* A rewrite retires the message it replaced. The backend owns that relationship
   on outreach_messages.superseded_by (see supabase migration 006), so the queue
   agrees with itself across devices and survives a cleared browser store —
   which the previous localStorage workaround could not. */
export function isMessageSuperseded(message) {
  if (!message || typeof message !== 'object') return false;
  const replacement = message.superseded_by;
  return replacement != null && replacement !== '';
}

export function isApprovalActionable(message, { lead, contact } = {}) {
  if (!message || !APPROVAL_STATUSES.includes(message.status)) return false;
  const channel = message.channel == null ? '' : String(message.channel).trim().toLowerCase();
  if (channel && channel !== 'email') return false;
  if (isMessageSuperseded(message)) return false;
  if (lead?.do_not_contact || lead?.status === 'do_not_contact' || contact?.do_not_contact) return false;
  return true;
}

export function labelFor(status) {
  if (status == null || status === '') return 'Not known';
  return LABELS[status] || String(status).replace(/_/g, ' ');
}

export function badge(status, textOverride) {
  const kind = STATUS_KIND[status] || 'neutral';
  const label = textOverride ?? labelFor(status);
  const cls = ['ifz-badge', kind];
  if (status === 'running' || status === 'processing') cls.push('running');
  return el('span', { class: cls.join(' ') }, label);
}

/**
 * Turn a background task into a short customer-facing sentence.
 * Progress values may select a sentence, but they are never printed.
 */
export function runSentence(run = {}) {
  const type = run.type || run.run_type;
  const status = run.status === 'succeeded' ? 'completed' : run.status;
  const progress = Number(run.progress) || 0;

  if (status === 'failed' || status === 'error') {
    const failures = {
      lead_scan: "I couldn't finish looking for buyers.",
      lead_research: "I couldn't finish learning about one of the buyers.",
      contact_discovery: "I couldn't finish looking for the right contacts.",
      outreach_generation: "I couldn't finish writing one of the emails.",
      company_brain_build: "I couldn't finish reading your company information.",
      document_processing: "I couldn't finish reading one of your files.",
      product_extraction: "I couldn't finish finding products in your files.",
    };
    return failures[type] || "I couldn't finish that work.";
  }
  if (status === 'cancelled') return 'The work was stopped. Nothing was sent.';
  if (status === 'queued' || status === 'created') {
    const queued = {
      lead_scan: 'Getting the buyer search ready.',
      lead_research: 'Getting the buyer research ready.',
      contact_discovery: 'Getting the contact search ready.',
      outreach_generation: 'Getting the email drafts ready.',
      company_brain_build: 'Getting your company information ready.',
      document_processing: 'Getting the file ready to read.',
      product_extraction: 'Getting the product files ready.',
    };
    return queued[type] || 'Getting the work ready.';
  }
  if (status === 'completed') {
    const completed = {
      lead_scan: 'The buyer search is complete.',
      lead_research: 'The buyer research is complete.',
      contact_discovery: 'The contact search is complete.',
      outreach_generation: 'The email draft is ready for review.',
      analytics_refresh: 'Your workspace summary is up to date.',
      company_brain_build: 'Your company information is ready for review.',
      document_processing: 'The file is ready to use.',
      product_extraction: 'The product list is ready for review.',
    };
    return completed[type] || 'The work is complete.';
  }

  if (type === 'lead_scan') {
    if (progress >= 75) return 'Saving the strongest buyers to your workspace.';
    if (progress >= 30) return 'Comparing possible buyers with your company profile.';
    return 'Looking through your target markets for buyers.';
  }
  const running = {
    lead_research: 'Learning enough about each buyer to write a useful email.',
    contact_discovery: 'Looking for the right person at each company.',
    outreach_generation: 'Writing an email for review.',
    analytics_refresh: 'Updating your workspace summary.',
    company_brain_build: 'Reading your company information.',
    document_processing: 'Reading the file and saving what matters.',
    product_extraction: 'Finding products in your uploaded files.',
  };
  return running[type] || 'Work is underway.';
}

const PIPELINE_STAGES = Object.freeze([
  ['found', 'Found'],
  ['researched', 'Researched'],
  ['contacts', 'Contacts'],
  ['written', 'Written'],
  ['sent', 'Sent'],
  ['replied', 'Replied'],
]);

/**
 * A compact, job-shaped view of the buyer journey.
 * Counts do not have to be a strict funnel: Contacts is intentionally a people
 * count, while the other stages describe company or message progress.
 */
export function pipelineRail(counts = {}, nextAction = null, { active = '', onSelect } = {}) {
  const stages = PIPELINE_STAGES.map(([key, label]) => {
    const count = Math.max(0, Number(counts[key]) || 0);
    const content = [
      el('span', { class: 'ifz-pipeline-count' }, String(count)),
      el('span', { class: 'ifz-pipeline-label' }, label),
    ];
    return el('li', {
      class: `ifz-pipeline-stage is-${key}${active === key ? ' is-active' : ''}`,
    }, onSelect
      ? el('button', {
          type: 'button',
          'aria-pressed': active === key ? 'true' : 'false',
          onclick: () => onSelect(key),
        }, content)
      : content);
  });

  const action = nextAction
    ? el('div', { class: `ifz-pipeline-next${nextAction.tone ? ` ${nextAction.tone}` : ''}` },
        el('div', { class: 'ifz-pipeline-next-copy' },
          el('span', { class: 'ifz-pipeline-next-mark', 'aria-hidden': 'true' }, '›'),
          el('div', {},
            el('strong', {}, nextAction.text || 'Your buyers are moving forward.'),
            nextAction.detail ? el('span', {}, nextAction.detail) : null)),
        nextAction.href
          ? el('a', { class: 'ifz-btn', href: nextAction.href }, nextAction.label || 'Open')
          : nextAction.onClick
            ? button(nextAction.busyLabel && nextAction.busy ? nextAction.busyLabel : (nextAction.label || 'Continue'), {
                kind: nextAction.kind || '',
                icon: nextAction.icon,
                onClick: nextAction.onClick,
                disabled: nextAction.disabled || nextAction.busy,
              })
            : null)
    : null;

  return el('section', {
    class: 'ifz-pipeline',
    'aria-label': 'Buyer pipeline',
  },
  el('ol', { class: 'ifz-pipeline-stages' }, stages),
  action);
}

export function scoreBadge(score, {
  title,
  words = false,
  allowZero = false,
  emptyLabel = 'Not researched yet',
} = {}) {
  const raw = score && typeof score === 'object' ? score.value : score;
  const v = Number(raw);
  if (!Number.isFinite(v) || (allowZero ? v < 0 : v <= 0)) {
    return el('span', {
      class: 'ifz-score unscored',
      title: title || 'This buyer has not been researched yet',
    }, emptyLabel);
  }
  const band = v >= 75 ? 'high' : v >= 50 ? 'mid' : 'low';
  const label = band === 'high' ? 'Strong fit' : band === 'mid' ? 'Possible fit' : 'Weak fit';
  return el('span', {
    class: `ifz-score ${band}`,
    title: title || (words ? label : `Buyer score ${v}/100`),
  }, words ? label : Math.round(v));
}

/* ---------- Data table ---------- */
export function dataTable({ columns, rows, onRowClick, empty, rowKey }) {
  if (!rows || rows.length === 0) {
    return empty || emptyState({ title: 'Nothing here yet', hint: 'No records match.' });
  }
  const thead = el('thead', {}, el('tr', {}, columns.map(c =>
    el('th', { style: c.width ? { width: c.width } : null, class: c.align === 'right' ? 'cell-num' : '' }, c.label))));
  const tbody = el('tbody', {}, rows.map(row => {
    const tr = el('tr', {
      class: onRowClick ? 'clickable' : '',
      dataset: rowKey ? { key: row[rowKey] } : {},
      tabindex: onRowClick ? 0 : null,
      role: onRowClick ? 'button' : null,
      'aria-label': onRowClick ? 'Open row details' : null,
    },
      columns.map(c => {
        const v = typeof c.render === 'function' ? c.render(row) : row[c.key];
        return el('td', { class: c.align === 'right' ? 'cell-num' : '' }, v == null ? '—' : v);
      }));
    if (onRowClick) {
      tr.addEventListener('click', (e) => {
        if (e.target.closest('button, a, input, select')) return;
        onRowClick(row);
      });
      tr.addEventListener('keydown', (e) => {
        if (e.target.closest('button, a, input, select')) return;
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onRowClick(row);
        }
      });
    }
    return tr;
  }));
  return el('div', { class: 'ifz-tablewrap' }, el('table', { class: 'ifz-table' }, thead, tbody));
}

/* ---------- Empty state ---------- */
export function emptyState({ icon: iconName = 'search', title, hint, action }) {
  return el('div', { class: 'ifz-empty' },
    el('div', { class: 'ifz-empty-icon' }, icon(iconName, 20)),
    el('div', { class: 'ifz-empty-title', role: 'heading', 'aria-level': '2' },
      title || 'Nothing here yet'),
    hint ? el('div', { class: 'ifz-empty-hint' }, hint) : null,
    action || null);
}

/* ---------- Modal ---------- */
let _modalId = 0;
export function modal({ title, body, actions, wide = false, onClose }) {
  const previousFocus = document.activeElement;
  const titleId = `ifz-modal-title-${++_modalId}`;
  const overlay = el('div', { class: 'ifz-overlay' });
  let closed = false;
  function close() {
    if (closed) return;
    closed = true;
    document.removeEventListener('keydown', onKey);
    overlay.remove();
    if (onClose) onClose();
    if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true });
  }
  function onKey(e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      close();
      return;
    }
    if (e.key !== 'Tab') return;
    const focusable = [...box.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), '
      + 'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    )].filter(node => !node.hidden && node.getAttribute('aria-hidden') !== 'true');
    if (!focusable.length) {
      e.preventDefault();
      box.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }
  document.addEventListener('keydown', onKey);
  overlay.addEventListener('mousedown', (e) => { if (e.target === overlay) close(); });

  const box = el('div', {
    class: `ifz-modal${wide ? ' wide' : ''}`,
    role: 'dialog',
    'aria-modal': 'true',
    'aria-labelledby': titleId,
    tabindex: '-1',
  },
    el('div', { class: 'ifz-modal-head' },
      el('div', { class: 'ifz-modal-title', id: titleId }, title),
      el('button', { class: 'ifz-modal-x', 'aria-label': 'Close', onclick: close }, '×')),
    el('div', { class: 'ifz-modal-body' }, body),
    actions ? el('div', { class: 'ifz-modal-foot' }, actions) : null);
  overlay.append(box);
  document.body.append(overlay);
  const firstInput = box.querySelector('input, select, textarea, button.primary')
    || box.querySelector('button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])')
    || box;
  setTimeout(() => firstInput.focus(), 40);
  return { close, box };
}

/* ---------- Toasts ---------- */
let _toastHost = null;
let _toastId = 0;
export function toast(message, kind = 'info', { actionLabel, onAction, duration = 4200 } = {}) {
  if (!_toastHost) {
    _toastHost = el('div', {
      class: 'ifz-toasts',
      role: 'region',
      'aria-label': 'Notifications',
      'aria-live': 'polite',
      'aria-relevant': 'additions',
    });
    document.body.append(_toastHost);
  }
  const id = `ifz-toast-${++_toastId}`;
  const node = el('div', {
    class: `ifz-toast ${kind}`,
    role: kind === 'error' ? 'alert' : 'status',
    id,
  },
    el('span', { class: 'ifz-toast-dot', 'aria-hidden': 'true' }),
    el('span', {}, message),
    actionLabel ? el('button', { class: 'ifz-toast-action', type: 'button', onclick: () => { dismiss(); if (onAction) onAction(); } }, actionLabel) : null);
  function dismiss() {
    node.classList.add('leaving');
    setTimeout(() => node.remove(), 260);
  }
  _toastHost.append(node);
  setTimeout(dismiss, duration);
  return dismiss;
}

/* ---------- Buttons ---------- */
export function button(label, { kind = '', size = '', icon: iconName, onClick, disabled = false, title } = {}) {
  return el('button', {
    class: `ifz-btn ${kind} ${size}`.trim(),
    type: 'button',
    onclick: onClick,
    disabled,
    title,
  }, iconName ? icon(iconName, 14) : null, label);
}
export function setBusy(btn, busy, busyLabel) {
  if (busy) {
    btn.dataset.label = btn.textContent;
    btn.disabled = true;
    btn.textContent = '';
    btn.append(el('span', { class: 'ifz-spin' }), busyLabel || btn.dataset.label);
  } else {
    btn.disabled = false;
    btn.textContent = btn.dataset.label || btn.textContent;
  }
}

/* ---------- Stepper ---------- */
export function stepper(steps, activeIdx, { onStep } = {}) {
  return el('div', { class: 'ifz-stepper', role: 'list', 'aria-label': 'Progress' }, steps.map((s, i) => {
    const state = i < activeIdx || s.done ? 'done' : i === activeIdx ? 'active' : '';
    const label = s.label || s;
    return el('button', {
      class: `ifz-step ${state}`,
      type: 'button',
      role: 'listitem',
      'aria-current': i === activeIdx ? 'step' : null,
      'aria-label': `Step ${i + 1}: ${label}${state === 'done' ? ' (completed)' : ''}`,
      onclick: onStep ? () => onStep(i) : null,
    },
      el('span', { class: 'ifz-step-dot', 'aria-hidden': 'true' }, state === 'done' ? '✓' : String(i + 1)),
      el('span', {}, label));
  }));
}

/* ---------- Tabs ---------- */
export function tabs(items, activeKey, onSelect) {
  return el('div', { class: 'ifz-tabs', role: 'tablist' }, items.map(t =>
    el('button', {
      class: `ifz-tab${t.key === activeKey ? ' active' : ''}`,
      type: 'button',
      role: 'tab',
      'aria-selected': t.key === activeKey ? 'true' : 'false',
      tabindex: t.key === activeKey ? 0 : -1,
      onclick: () => onSelect(t.key),
    }, t.label)));
}

/* ---------- Chip multi-select ---------- */
export function chipSelect(options, selected, { onChange, max, onLimit } = {}) {
  const sel = new Set(selected);
  const host = el('div', { class: 'ifz-chipselect', role: 'group' });
  for (const opt of options) {
    const value = opt.value ?? opt;
    const label = opt.label ?? opt;
    const chip = el('button', {
      class: `ifz-chip${sel.has(value) ? ' on' : ''}`,
      type: 'button',
      'aria-pressed': sel.has(value) ? 'true' : 'false',
    }, label);
    chip.addEventListener('click', () => {
      if (sel.has(value)) {
        sel.delete(value);
      } else {
        if (max && sel.size >= max) {
          onLimit?.(max);
          return;
        }
        sel.add(value);
      }
      chip.classList.toggle('on');
      chip.setAttribute('aria-pressed', sel.has(value) ? 'true' : 'false');
      if (onChange) onChange([...sel]);
    });
    host.append(chip);
  }
  host.getSelected = () => [...sel];
  return host;
}

/* ---------- Radio cards ---------- */
export function radioCards(options, selectedValue, { onChange } = {}) {
  let current = selectedValue;
  const host = el('div', { class: 'ifz-radiocards', role: 'radiogroup' });
  const cards = options.map(opt => {
    const cardBtn = el('button', {
      class: `ifz-radiocard${opt.value === current ? ' on' : ''}`,
      type: 'button',
      role: 'radio',
      'aria-checked': opt.value === current ? 'true' : 'false',
    },
      el('span', { class: 'ifz-radiocard-title' }, opt.title),
      opt.desc ? el('span', { class: 'ifz-radiocard-desc' }, opt.desc) : null,
      opt.meta ? el('span', { class: 'ifz-radiocard-meta' }, opt.meta) : null);
    cardBtn.addEventListener('click', () => {
      current = opt.value;
      cards.forEach(c => {
        c.classList.remove('on');
        c.setAttribute('aria-checked', 'false');
      });
      cardBtn.classList.add('on');
      cardBtn.setAttribute('aria-checked', 'true');
      if (onChange) onChange(current);
    });
    return cardBtn;
  });
  host.append(...cards);
  host.getSelected = () => current;
  return host;
}

/* ---------- Timeline ---------- */
export function timeline(items) {
  if (!items || !items.length) return emptyState({ icon: 'clock', title: 'No activity yet' });
  return el('div', { class: 'ifz-timeline' }, items.map(it =>
    el('div', { class: `ifz-tl-item ${it.tone || ''}` },
      el('span', { class: 'ifz-tl-dot' }),
      el('div', { class: 'ifz-tl-body' },
        el('div', { class: 'ifz-tl-label' }, it.label),
        el('div', { class: 'ifz-tl-time' }, it.time)))));
}

/* ---------- KV list ---------- */
export function kv(pairs) {
  const host = el('dl', { class: 'ifz-kv' });
  for (const [k, v] of pairs) {
    host.append(el('dt', {}, k), el('dd', {}, v == null || v === '' ? '—' : v));
  }
  return host;
}

/* ---------- Charts ---------- */
export function sparkline(points, { width = 84, height = 26, stroke = 'var(--accent)', label = 'Trend' } = {}) {
  const max = Math.max(...points, 1);
  const min = Math.min(...points, 0);
  const range = max - min || 1;
  const step = width / (points.length - 1);
  const coords = points.map((p, i) => `${(i * step).toFixed(1)},${(height - 2 - ((p - min) / range) * (height - 4)).toFixed(1)}`);
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('width', width);
  svg.setAttribute('height', height);
  svg.setAttribute('class', 'ifz-chart');
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', `${label}: ${points.join(', ')}`);
  const poly = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
  poly.setAttribute('points', coords.join(' '));
  poly.setAttribute('fill', 'none');
  poly.setAttribute('stroke', stroke);
  poly.setAttribute('stroke-width', '1.6');
  poly.setAttribute('stroke-linejoin', 'round');
  poly.setAttribute('stroke-linecap', 'round');
  svg.append(poly);
  return svg;
}

export function barChart({ labels, values, height = 150, color = 'var(--accent)', title = 'Bar chart' }) {
  const max = Math.max(...values, 1);
  const n = values.length;
  const gap = 6;
  const width = 100 * n;
  const barW = (width - gap * (n + 1)) / n;
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', `0 0 ${width} ${height + 18}`);
  svg.setAttribute('class', 'ifz-chart');
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', `${title}. ${labels.map((l, i) => `${l}: ${values[i]}`).join('; ')}`);
  svg.style.height = `${height + 18}px`;
  values.forEach((v, i) => {
    const h = Math.max(2, (v / max) * (height - 14));
    const x = gap + i * (barW + gap);
    const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    rect.setAttribute('x', x);
    rect.setAttribute('y', height - h);
    rect.setAttribute('width', barW);
    rect.setAttribute('height', h);
    rect.setAttribute('rx', 3);
    rect.setAttribute('fill', color);
    const tip = document.createElementNS('http://www.w3.org/2000/svg', 'title');
    tip.textContent = `${labels[i]}: ${v}`;
    rect.append(tip);
    svg.append(rect);
    const t = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    t.setAttribute('x', x + barW / 2);
    t.setAttribute('y', height + 13);
    t.setAttribute('text-anchor', 'middle');
    t.textContent = labels[i];
    svg.append(t);
  });
  return svg;
}

export function hbarList(items, { color = 'var(--accent)', suffix = '' } = {}) {
  const max = Math.max(...items.map(i => i.value), 1);
  return el('div', {}, items.map(it =>
    el('div', { class: 'ifz-hbar-row' },
      el('span', { class: 'ifz-bars-label' }, it.label),
      el('div', { class: 'ifz-hbar-track' },
        el('div', { class: 'ifz-hbar-fill', style: { width: `${(it.value / max) * 100}%`, background: it.color || color } })),
      el('span', { class: 'ifz-hbar-val' }, `${fmt.num(it.value)}${suffix}`))));
}

/* ---------- CSV download ---------- */
export function blobDownload(filename, blob, message = `Downloaded ${filename}`) {
  const a = el('a', { href: URL.createObjectURL(blob), download: filename });
  document.body.append(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
  toast(message, 'success');
}

export function csvDownload(filename, rows) {
  if (!rows || !rows.length) { toast('Nothing to export', 'warning'); return; }
  const headers = Object.keys(rows[0]);
  const escape = (v) => {
    const s = v == null ? '' : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const csv = [headers.join(','), ...rows.map(r => headers.map(h => escape(r[h])).join(','))].join('\r\n');
  const blob = new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8' });
  blobDownload(filename, blob, `Exported ${rows.length} rows to ${filename}`);
}

/* ---------- Page scaffolding ---------- */
export function pageHead({ title, sub, actions }) {
  return el('div', { class: 'ifz-page-head' },
    el('div', {},
      el('h1', { class: 'ifz-page-title' }, title),
      sub ? el('p', { class: 'ifz-page-sub' }, sub) : null),
    actions ? el('div', { class: 'ifz-page-actions' }, actions) : null);
}

/* ---------- Form field helpers ---------- */
let _fieldId = 0;
function resolveControl(node) {
  if (!node) return null;
  if (typeof node.matches === 'function' && node.matches('input, select, textarea')) return node;
  return node.querySelector?.('input, select, textarea') || node;
}
export function field(labelText, inputNode, { hint, required } = {}) {
  const control = resolveControl(inputNode);
  const id = control.id || `ifz-field-${++_fieldId}`;
  control.id = id;
  if (required) control.setAttribute('aria-required', 'true');
  const hintId = hint ? `${id}-hint` : null;
  if (hintId) control.setAttribute('aria-describedby', hintId);
  return el('div', { class: 'ifz-field' },
    el('label', { class: 'ifz-label', for: id }, labelText, required ? el('span', { class: 'req', 'aria-hidden': 'true' }, ' *') : null),
    inputNode,
    hint ? el('div', { class: 'ifz-hint', id: hintId }, hint) : null);
}
export function input(attrs = {}) { return el('input', { class: 'ifz-input', ...attrs }); }
export function select(options, attrs = {}) {
  return el('select', { class: 'ifz-select', ...attrs }, options.map(o =>
    el('option', { value: o.value ?? o, selected: (o.value ?? o) === attrs.value }, o.label ?? o)));
}
export function textarea(attrs = {}) {
  const { value, ...rest } = attrs;
  const node = el('textarea', { class: 'ifz-textarea', ...rest });
  if (value != null) node.value = value;
  return node;
}

/** Password field with show/hide toggle. Returns { wrap, input }. */
export function passwordField(attrs = {}) {
  const inp = input({ type: 'password', autocomplete: 'current-password', ...attrs });
  const toggle = el('button', {
    class: 'ifz-iconbtn ifz-pw-toggle',
    type: 'button',
    'aria-label': 'Show password',
    title: 'Show password',
  }, icon('eye', 15));
  toggle.addEventListener('click', () => {
    const show = inp.type === 'password';
    inp.type = show ? 'text' : 'password';
    toggle.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
    toggle.title = show ? 'Hide password' : 'Show password';
  });
  const wrap = el('div', { class: 'ifz-pw-wrap' }, inp, toggle);
  return { wrap, input: inp };
}
