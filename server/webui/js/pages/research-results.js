/* Customer research results: verdict-led review with cited evidence. */

import { call } from '../api.js';
import {
  badge, blobDownload, button, el, emptyState, fmt, pageHead, setBusy, tabs, toast,
} from '../ui.js';

const itemsOf = value => Array.isArray(value) ? value : value?.items || [];
const TERMINAL_CAMPAIGN_STATES = new Set(['succeeded', 'completed', 'partial', 'failed']);
// States worth polling in. Deliberately not the inverse of TERMINAL: that set
// also decides which campaign opens by default, and `cancelled` belongs in
// neither — it is finished, so polling it would never stop.
const LIVE_CAMPAIGN_STATES = new Set(['queued', 'running']);
// A campaign writes one company at a time and the results endpoint never gated
// on completion, so the data was already arriving live — the page just told the
// customer to reload. Five seconds is slower than a company takes to verify, so
// nothing is missed, and it is two requests per tick against a run measured in
// minutes.
const LIVE_REFRESH_MS = 5000;
const SUPPORTING_CLAIM_STATUSES = new Set(['observed', 'estimated_range']);
const COPY = Object.freeze({
  en: {
    fit: 'Fit', confidence: 'Confidence', band: 'Band', known: 'Known', unknown: 'Unknown',
    sources: 'Sources', criterion: 'Criterion', displayed: 'Displayed value',
    canonical: 'Canonical English', original: 'Original source text', sourceLanguage: 'Source language',
    observed: 'Observed', retrieved: 'Retrieved', archived: 'Archived', snapshot: 'Snapshot', hash: 'SHA-256',
    verified: 'Exact source span verified', unverified: 'Source span not mechanically verified',
    openSource: 'Open source', noUnknowns: 'No weighted criteria remain unknown.',
    reference: 'Dataset record', publisher: 'Published by',
  },
  tr: {
    fit: 'Uyum', confidence: 'Kanıt güveni', band: 'Grup', known: 'Bilinen', unknown: 'Bilinmeyen',
    sources: 'Kaynaklar', criterion: 'Kriter', displayed: 'Gösterilen değer',
    canonical: 'Kanonik İngilizce', original: 'Orijinal kaynak metni', sourceLanguage: 'Kaynak dili',
    observed: 'Gözlemlendi', retrieved: 'Alındı', archived: 'Arşivlendi', snapshot: 'Anlık görüntü', hash: 'SHA-256',
    verified: 'Kaynak alıntısı birebir doğrulandı', unverified: 'Kaynak alıntısı mekanik olarak doğrulanmadı',
    openSource: 'Kaynağı aç', noUnknowns: 'Ağırlıklı bilinmeyen kriter kalmadı.',
    reference: 'Veri kümesi kaydı', publisher: 'Yayınlayan',
  },
});
const DIMENSION_LABELS = Object.freeze({
  en: {
    product_sector_fit: 'Product and sector fit', buyer_channel_fit: 'Buyer and channel fit',
    buying_intent: 'Buying intent', market_coverage: 'Market coverage',
    commercial_scale: 'Commercial scale', trade_activity: 'Trade activity', contactability: 'Contactability',
  },
  tr: {
    product_sector_fit: 'Ürün ve sektör uyumu', buyer_channel_fit: 'Alıcı ve kanal uyumu',
    buying_intent: 'Satın alma niyeti', market_coverage: 'Pazar kapsamı',
    commercial_scale: 'Ticari ölçek', trade_activity: 'Ticaret faaliyeti', contactability: 'Ulaşılabilirlik',
  },
});
const LANGUAGE_NAMES = Object.freeze({
  en: { en: 'English', tr: 'Turkish', de: 'German', pl: 'Polish', fr: 'French', ar: 'Arabic', ro: 'Romanian', nl: 'Dutch' },
  tr: { en: 'İngilizce', tr: 'Türkçe', de: 'Almanca', pl: 'Lehçe', fr: 'Fransızca', ar: 'Arapça', ro: 'Romence', nl: 'Felemenkçe' },
});

function supportedLocale(value) {
  return String(value || 'en').toLowerCase().startsWith('tr') ? 'tr' : 'en';
}

function copy(locale) {
  return COPY[supportedLocale(locale)];
}

function dimensionLabel(dimension, locale = 'en') {
  return DIMENSION_LABELS[supportedLocale(locale)][dimension] || sentence(dimension);
}

function sentence(value, fallback = 'Not known') {
  const text = String(value || '').replace(/[_-]+/g, ' ').trim();
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : fallback;
}

function claimValue(value) {
  if (value == null || value === '') return 'Not known';
  if (Array.isArray(value)) return value.length ? value.join(', ') : 'Not known';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  return String(value);
}

function confidencePercent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return 'Not known';
  return `${Math.round(number <= 1 ? number * 100 : number)}%`;
}

// What a curated buyer list actually proved. It states that this company buys
// in the sector; it does not state that it is specifically an importer or a
// distributor, and rendering it as "Sector buyer" invited the reader to assume
// the narrower claim.
const ROLE_LABELS = Object.freeze({
  en: { sector_buyer: 'Curated sector buyer · exact channel not confirmed' },
  tr: { sector_buyer: 'Derlenmiş sektör alıcısı · kesin kanal doğrulanmadı' },
});

function buyerRoleLabel(value, locale = 'en') {
  const key = String(value || '').trim().toLowerCase().replace(/[\s-]+/g, '_');
  return ROLE_LABELS[supportedLocale(locale)][key] || sentence(value);
}

function verdictLabel(verdict) {
  return {
    strong_fit: 'Strong fit',
    review: 'Review',
    reject: 'Reject',
  }[verdict] || sentence(verdict);
}

function verdictBadge(verdict) {
  const tone = { strong_fit: 'completed', review: 'partial', reject: 'failed' }[verdict] || verdict;
  return badge(tone, verdictLabel(verdict));
}

function campaignNotice(campaign) {
  const missing = campaign?.credential_required_source_ids
    || campaign?.missing_credential_source_ids
    || [];
  if (missing.length) return {
    tone: 'warning',
    title: 'A research source needs credentials',
    copy: `${missing.map(source => sentence(source)).join(', ')} cannot provide evidence until an administrator connects it.`,
  };
  if (['queued', 'running'].includes(campaign?.status)) return {
    tone: 'neutral',
    title: 'Research is running',
    copy: 'Each company is verified against its sources before it appears. New results arrive here as they qualify.',
  };
  if (campaign?.status === 'cancelled') return {
    tone: 'warning',
    title: 'Research was cancelled',
    copy: 'Whatever had already been verified is kept and shown below.',
  };
  if (campaign?.status === 'partial') return {
    tone: 'warning',
    title: 'Research completed with partial coverage',
    copy: 'Available evidence is shown. Missing coverage remains visible in each result.',
  };
  if (campaign?.status === 'failed') return {
    tone: 'error',
    title: 'Research did not complete',
    copy: 'No assumptions were added. Review the saved evidence or choose another brief.',
  };
  if (['succeeded', 'completed'].includes(campaign?.status)) return {
    tone: 'success',
    title: 'Research completed',
    copy: 'Verdicts are ordered by fit and evidence confidence.',
  };
  return {
    tone: 'neutral',
    title: 'Research has not completed',
    copy: 'Results will appear here when the selected brief has finished.',
  };
}

function noticeNode(campaign) {
  const notice = campaignNotice(campaign);
  return el('div', {
    class: `ifz-results-notice ${notice.tone}`,
    role: notice.tone === 'error' ? 'alert' : 'status',
  },
  el('strong', {}, notice.title),
  el('span', { class: 'ifz-prose' }, notice.copy));
}

function loadingNode(label = 'Loading research results') {
  return el('div', { class: 'ifz-results-loading', role: 'status', 'aria-label': label },
    el('span', { class: 'ifz-results-loading-line' }),
    el('span', { class: 'ifz-results-loading-line short' }),
    el('span', { class: 'ifz-results-loading-block' }));
}

function safeSourceLink(evidence, locale = 'en') {
  const value = String(evidence?.provenance_url || '');
  if (!value.startsWith('https://')) return null;
  return el('a', {
    href: value,
    target: '_blank',
    rel: 'noreferrer',
    class: 'ifz-result-source-link',
  }, copy(locale).openSource);
}

function dated(value) {
  return value ? `${fmt.date(value)} ${fmt.time(value)}` : 'Not recorded';
}

export function renderEvidence(evidence, locale = 'en') {
  const labels = copy(locale);
  const link = safeSourceLink(evidence, locale);
  const canonical = evidence.value_en ?? evidence.facts?.[0]?.value_en;
  const displayed = evidence.display_value ?? evidence.facts?.[0]?.display_value ?? canonical;
  const original = evidence.original_text ?? evidence.facts?.[0]?.original_text;
  const language = evidence.source_language ?? evidence.facts?.[0]?.source_language;
  const criteria = evidence.criteria || [];
  const definitionRows = [
    criteria.length ? [labels.criterion, criteria.map(item =>
      `${dimensionLabel(item.dimension, locale)} · ${item.weight}%`).join(', ')] : null,
    supportedLocale(locale) !== 'en' && displayed !== canonical ? [labels.displayed, claimValue(displayed)] : null,
    // Internal evidence has no page to open, so its identity is shown as a
    // value the reader can quote back. Never an anchor: a link a customer
    // cannot follow is worse than an identifier.
    !link && evidence.source_reference ? [labels.reference, evidence.source_reference] : null,
    evidence.publisher_label ? [labels.publisher, evidence.publisher_label] : null,
    [labels.canonical, claimValue(canonical)],
    [labels.original, original || 'Not recorded'],
    [labels.sourceLanguage, LANGUAGE_NAMES[supportedLocale(locale)][language] || sentence(language)],
    [labels.observed, dated(evidence.observed_at)],
    [labels.retrieved, dated(evidence.retrieved_at)],
    evidence.archive_snapshot_at ? [labels.archived, dated(evidence.archive_snapshot_at)] : null,
    [labels.snapshot, evidence.snapshot_id || 'Not recorded'],
    [labels.hash, evidence.raw_hash || 'Not recorded'],
  ].filter(Boolean);
  return el('div', { class: 'ifz-result-citation' },
    el('div', { class: 'ifz-result-citation-head' },
      el('strong', {}, sentence(evidence.source_id, 'Saved source')),
      link),
    el('dl', { class: 'ifz-result-citation-meta' }, definitionRows.map(([label, value]) =>
      el('div', {}, el('dt', {}, label), el('dd', {},
        label === labels.hash || label === labels.reference
          ? el('code', {}, value)
          : value)))),
    el('p', {
      class: `ifz-result-validation ${evidence.mechanically_validated ? 'verified' : 'unverified'}`,
    }, evidence.mechanically_validated ? labels.verified : labels.unverified));
}

function claimNode(claim, locale = 'en') {
  return el('article', { class: 'ifz-result-claim' },
    el('div', { class: 'ifz-result-claim-head' },
      el('strong', {}, sentence(claim.field)),
      el('span', {}, confidencePercent(claim.confidence))),
    el('div', { class: 'ifz-result-claim-value ifz-prose' }, claimValue(claim.value)),
    (claim.evidence || []).length
      ? el('div', { class: 'ifz-result-citations' }, claim.evidence.map(item => renderEvidence(item, locale)))
      : el('p', { class: 'ifz-result-uncited ifz-prose' }, 'No cited source is attached to this claim.'));
}

export function renderResearchResult(result, locale = 'en') {
  const labels = copy(locale);
  const unknown = Object.entries(result.unknown_dimensions || {});
  return el('section', { class: 'ifz-result-summary', 'aria-label': `${labels.fit} and evidence coverage` },
    el('dl', { class: 'ifz-result-evidence-metrics' },
      el('div', {}, el('dt', {}, labels.fit), el('dd', {}, String(result.fit_score ?? 0))),
      el('div', {}, el('dt', {}, labels.confidence), el('dd', {}, confidencePercent(result.evidence_confidence))),
      el('div', {}, el('dt', {}, labels.band), el('dd', {}, result.priority_band || '—')),
      el('div', {}, el('dt', {}, labels.known), el('dd', {}, `${result.known_weight ?? 0}%`)),
      el('div', {}, el('dt', {}, labels.unknown), el('dd', {}, `${result.unknown_weight ?? 0}%`))),
    unknown.length
      ? el('ul', { class: 'ifz-result-unknowns ifz-prose' }, unknown.map(([dimension, weight]) =>
          el('li', {}, `${dimensionLabel(dimension, locale)} · ${weight}%`)))
      : el('p', { class: 'ifz-result-none ifz-prose' }, labels.noUnknowns));
}

function textList(values, emptyCopy) {
  return values?.length
    ? el('ul', { class: 'ifz-result-text-list ifz-prose' },
        values.map(value => el('li', {}, sentence(value))))
    : el('p', { class: 'ifz-result-none ifz-prose' }, emptyCopy);
}

function evidencePanel(result, claimState, locale = 'en', onDiscover = null) {
  if (!result) return emptyState({
    icon: 'search',
    title: 'Select a company',
    hint: 'Choose a row to inspect the evidence behind its verdict.',
  });
  if (!claimState || claimState.status === 'loading') {
    return loadingNode(`Loading evidence for ${result.company_name}`);
  }
  if (claimState.status === 'error') return el('div', { class: 'ifz-results-error', role: 'alert' },
    el('strong', {}, 'Evidence could not be loaded'),
    el('p', { class: 'ifz-prose' }, 'The verdict is unchanged. Select the company again to retry.'));

  const claims = claimState.items || [];
  const supporting = claims.filter(claim => SUPPORTING_CLAIM_STATUSES.has(claim.status));
  const conflicting = claims.filter(claim => claim.status === 'conflicted');
  const neutral = claims.filter(claim =>
    !SUPPORTING_CLAIM_STATUSES.has(claim.status) && claim.status !== 'conflicted');
  const conflictNames = new Set(result.conflicting_claims || []);
  for (const claim of conflicting) conflictNames.delete(claim.field);
  const discoverAction = result.lead_id && onDiscover
    ? button('Find decision-maker', { kind: 'ghost', size: 'sm', icon: 'search' })
    : null;
  if (discoverAction) {
    discoverAction.addEventListener('click', () => void onDiscover(result, discoverAction));
  }

  return el('section', { class: 'ifz-result-evidence', 'aria-label': `Evidence for ${result.company_name}` },
    el('header', { class: 'ifz-result-evidence-head' },
      el('h2', {}, result.company_name),
      el('div', { class: 'ifz-result-evidence-actions' }, verdictBadge(result.verdict), discoverAction)),
    renderResearchResult(result, locale),
    el('section', { class: 'ifz-result-evidence-section' },
      el('h3', {}, 'Why this verdict'),
      textList(result.reasons, 'No verdict reason was recorded.')),
    el('section', { class: 'ifz-result-evidence-section' },
      el('h3', {}, 'Supporting claims'),
      supporting.length
        ? el('div', { class: 'ifz-result-claim-list' }, supporting.map(claim => claimNode(claim, locale)))
        : el('p', { class: 'ifz-result-none ifz-prose' }, 'No supporting claims were recorded.')),
    el('section', { class: 'ifz-result-evidence-section' },
      el('h3', {}, 'Conflicting claims'),
      conflicting.length || conflictNames.size
        ? el('div', { class: 'ifz-result-claim-list' },
            conflicting.map(claim => claimNode(claim, locale)),
            [...conflictNames].map(field => el('p', { class: 'ifz-result-conflict ifz-prose' }, sentence(field))))
        : el('p', { class: 'ifz-result-none ifz-prose' }, 'No conflicting claims were recorded.')),
    el('section', { class: 'ifz-result-evidence-section' },
      el('h3', {}, 'Unknown or not applicable'),
      neutral.length
        ? el('div', { class: 'ifz-result-claim-list neutral' }, neutral.map(claim => claimNode(claim, locale)))
        : el('p', { class: 'ifz-result-none ifz-prose' }, 'No neutral claims were recorded.')),
    el('section', { class: 'ifz-result-evidence-section' },
      el('h3', {}, 'Missing evidence'),
      // Not all of these disqualify a lead — a strong fit backed by a registry
      // notice still reports that the company's own page was never read — so
      // the empty state no longer calls them "required".
      textList(result.missing_evidence, 'Nothing is marked missing.')));
}

function resultTable(results, selectedId, onSelect, locale = 'en') {
  if (!results.length) return null;
  const labels = copy(locale);
  const headers = ['Company', 'Verdict', labels.fit, labels.confidence, labels.unknown, 'Country', 'Buyer role', labels.sources];
  return el('div', { class: 'ifz-result-tablewrap' },
    el('table', { class: 'ifz-result-table' },
      el('thead', {}, el('tr', {}, headers.map(label => el('th', {}, label)))),
      el('tbody', {}, results.map(result => {
        const selected = result.id === selectedId;
        return el('tr', { class: selected ? 'selected' : '' },
        el('td', {}, el('button', {
          class: 'ifz-result-company-button',
          type: 'button',
          'aria-label': `Inspect evidence for ${result.company_name}`,
          'aria-current': selected ? 'true' : null,
          onclick: () => onSelect(result),
        }, result.company_name)),
        el('td', {}, verdictBadge(result.verdict)),
        el('td', { class: 'cell-num' }, `${result.fit_score} / 100`),
        el('td', { class: 'cell-num' }, confidencePercent(result.evidence_confidence)),
        el('td', { class: 'cell-num' }, `${result.unknown_weight ?? 0}%`),
        el('td', {}, result.country || 'Not known'),
        el('td', {}, buyerRoleLabel(result.buyer_role, locale)),
        el('td', { class: 'cell-num' }, String(result.source_count ?? 0)));
      }))));
}

// The four questions a customer asks about one run, in the order they matter:
// which companies am I working, which nearly made it, which cleared the bar but
// did not fit in the list, and which were ruled out. `active` keeps its wire
// name for compatibility.
const VIEWS = Object.freeze(['active', 'review', 'outside_limit', 'rejected']);
const VIEW_LABELS = Object.freeze({
  active: 'Strong fits', review: 'Review',
  outside_limit: 'Not selected', rejected: 'Rejected',
});
const VIEW_COUNT_METRIC = Object.freeze({
  active: 'qualified_leads', review: 'review_candidates',
  outside_limit: 'outside_result_limit',
});

function emptyResultStates() {
  return Object.fromEntries(VIEWS.map(view => [view, { status: 'idle', items: [] }]));
}

function emptySelection() {
  return Object.fromEntries(VIEWS.map(view => [view, '']));
}

function overallMetrics(rows) {
  const items = Array.isArray(rows) ? rows : rows?.items || [];
  return items.find(row => row.dimension === 'overall') || items[0] || null;
}

function countryDistribution(metrics) {
  const entries = Object.entries(metrics?.leads_by_country || {});
  entries.sort(([left], [right]) => left.localeCompare(right));
  return entries;
}

// The one sentence that has to be true. A short list is reported as short: the
// engine never promotes a review candidate to reach the target, so copy that
// implied it could was the lie this row exists to remove.
function selectionSummary(metrics) {
  const qualified = Number(metrics?.qualified_leads ?? 0);
  const target = Number(metrics?.result_target_min ?? 5);
  const shortfall = Number(metrics?.result_shortfall ?? 0);
  if (shortfall > 0) {
    return {
      headline: `${qualified} strong ${qualified === 1 ? 'fit' : 'fits'} qualified `
        + `\u00b7 ${shortfall} below the target of ${target}`,
      detail: 'Review candidates do not fill the target. Widen the markets or the '
        + 'product terms, or connect another source.',
    };
  }
  return {
    headline: `${qualified} of ${target} target leads qualified`,
    detail: null,
  };
}

export async function mount(root, ctx) {
  let disposed = false;
  const locale = supportedLocale(ctx.locale || document.documentElement?.lang || 'en');
  const pageEl = root.closest('.ifz-page') || root;
  pageEl.classList.add('ifz-page--research-results');
  const state = {
    campaigns: [],
    campaignId: ctx.query.campaign || '',
    view: 'active',
    resultStates: emptyResultStates(),
    selected: emptySelection(),
    metrics: null,
    claims: new Map(),
    campaignError: null,
    contextVersion: 0,
  };
  const page = el('div', { class: 'ifz-research-results' }, loadingNode('Loading research briefs'));
  root.append(page);

  function campaign() {
    return state.campaigns.find(item => item.id === state.campaignId) || null;
  }

  function selectedResult() {
    const viewState = state.resultStates[state.view];
    return viewState.items.find(item => item.id === state.selected[state.view]) || null;
  }

  function contextMatches(context, { selection = false } = {}) {
    return !disposed
      && state.contextVersion === context.version
      && state.campaignId === context.campaignId
      && state.view === context.view
      && (!selection || state.selected[context.view] === context.resultId);
  }

  async function loadClaims(result) {
    if (!result || state.claims.get(result.id)?.status === 'loaded') return;
    const context = {
      version: state.contextVersion,
      campaignId: state.campaignId,
      view: state.view,
      resultId: result.id,
    };
    state.claims.set(result.id, { status: 'loading', items: [] });
    render();
    try {
      const response = await call('researchResults.claims', {
        params: { resultId: result.id },
        ...(locale === 'tr' ? { query: { locale } } : {}),
      });
      if (!contextMatches(context, { selection: true })) return;
      state.claims.set(result.id, { status: 'loaded', items: itemsOf(response) });
    } catch (error) {
      if (!contextMatches(context, { selection: true })) return;
      state.claims.set(result.id, { status: 'error', error, items: [] });
    }
    render();
  }

  async function discoverContacts(result, action) {
    if (!result?.lead_id) return;
    setBusy(action, true, 'Starting');
    try {
      await call('contacts.discover', { body: { lead_ids: [result.lead_id] } });
      toast(`Contact research started for ${result.company_name}.`, 'success');
    } catch {
      toast('Contact research could not be started. The lead is unchanged.', 'error');
    } finally {
      setBusy(action, false);
    }
  }

  function chooseResult(result) {
    state.contextVersion += 1;
    state.selected[state.view] = result.id;
    render();
    void loadClaims(result);
  }

  async function loadResults(view) {
    if (!state.campaignId) return;
    const context = {
      version: state.contextVersion,
      campaignId: state.campaignId,
      view,
    };
    if (state.resultStates[view].status === 'loaded') {
      const selected = state.resultStates[view].items
        .find(item => item.id === state.selected[view]);
      if (selected) await loadClaims(selected);
      return;
    }
    state.resultStates[view] = { status: 'loading', items: [] };
    render();
    try {
      const response = await call('researchCampaigns.results', {
        params: { campaignId: context.campaignId },
        query: { view },
      });
      if (!contextMatches(context)) return;
      const items = itemsOf(response);
      state.resultStates[view] = { status: 'loaded', items };
      if (!state.selected[view] && items.length) state.selected[view] = items[0].id;
      render();
      const first = items.find(item => item.id === state.selected[view]);
      if (first) await loadClaims(first);
    } catch (error) {
      if (!contextMatches(context)) return;
      state.resultStates[view] = { status: 'error', error, items: [] };
      render();
    }
  }

  async function loadMetrics() {
    if (!state.campaignId) return;
    const campaignId = state.campaignId;
    try {
      const response = await call('researchCampaigns.metrics', { params: { campaignId } });
      if (disposed || state.campaignId !== campaignId) return;
      state.metrics = overallMetrics(response);
      render();
    } catch {
      // The counts are a summary of the list already on screen. Losing them is
      // not worth replacing the results with an error.
    }
  }

  async function switchView(view) {
    if (state.view === view) return;
    state.contextVersion += 1;
    state.view = view;
    render();
    await loadResults(view);
  }

  async function switchCampaign(campaignId) {
    state.contextVersion += 1;
    state.campaignId = campaignId;
    state.view = 'active';
    state.resultStates = emptyResultStates();
    state.selected = emptySelection();
    state.metrics = null;
    state.claims.clear();
    render();
    await Promise.all([loadResults('active'), loadMetrics()]);
  }

  async function exportView(action) {
    const context = {
      version: state.contextVersion,
      campaignId: state.campaignId,
      view: state.view,
    };
    setBusy(action, true, 'Preparing');
    try {
      const file = await call('researchCampaigns.export', {
        params: { campaignId: context.campaignId },
        query: { view: context.view },
      });
      if (!contextMatches(context)) return;
      blobDownload(file.filename, file.blob, `Downloaded ${file.filename}`);
    } catch {
      if (!contextMatches(context)) return;
      toast('The research export could not be prepared.', 'error');
    } finally {
      setBusy(action, false);
    }
  }

  function briefNode(current) {
    const config = current.config || {};
    return el('section', { class: 'ifz-results-brief', 'aria-label': 'Active research brief' },
      el('div', { class: 'ifz-results-brief-copy' },
        el('span', { class: 'ifz-overline' }, 'Active research brief'),
        el('strong', {}, current.name),
        el('p', { class: 'ifz-prose' },
          `${(config.sector_ids || []).map(value => sentence(value)).join(', ') || 'All configured sectors'}. `
          + `${(config.buyer_types || []).map(value => sentence(value)).join(', ') || 'Configured buyer roles'}.`)),
      el('dl', { class: 'ifz-results-coverage' },
        el('div', {}, el('dt', {}, 'Run'), el('dd', {}, sentence(current.status))),
        el('div', {}, el('dt', {}, 'Markets'), el('dd', {}, String((config.target_countries || []).length))),
        el('div', {}, el('dt', {}, 'Configured sources'), el('dd', {}, String((config.enabled_source_ids || []).length))),
        el('div', {}, el('dt', {}, `${VIEW_LABELS[state.view]} shown`),
          el('dd', {}, String(state.resultStates[state.view].items.length)))));
  }

  function selectionNode() {
    const metrics = state.metrics;
    const summary = selectionSummary(metrics);
    const distribution = countryDistribution(metrics);
    return el('section', {
      class: 'ifz-results-selection',
      'aria-label': 'Primary list coverage',
    },
    el('div', { class: 'ifz-results-selection-headline' },
      el('strong', {}, summary.headline),
      el('span', {}, `Limit ${Number(metrics?.result_limit ?? 15)}`)),
    el('dl', { class: 'ifz-results-selection-spread' },
      el('div', {},
        el('dt', {}, 'Markets represented'),
        el('dd', {}, String(Number(metrics?.countries_represented ?? distribution.length)))),
      ...distribution.map(([code, count]) => el('div', { class: 'ifz-results-market' },
        el('dt', {}, code),
        el('dd', {}, String(count))))),
    summary.detail
      ? el('p', { class: 'ifz-results-selection-note ifz-prose' }, summary.detail)
      : null);
  }

  function tabLabel(view) {
    const metric = VIEW_COUNT_METRIC[view];
    if (!metric || !state.metrics) return VIEW_LABELS[view];
    return `${VIEW_LABELS[view]} ${Number(state.metrics[metric] ?? 0)}`;
  }

  function render() {
    if (disposed) return;
    if (state.campaignError) {
      page.replaceChildren(emptyState({
        icon: 'warning',
        title: 'Research briefs could not be loaded',
        hint: 'Your saved research is unchanged. Reload the page to try again.',
      }));
      return;
    }
    if (!state.campaigns.length) {
      page.replaceChildren(emptyState({
        icon: 'search',
        title: 'No research brief yet',
        hint: 'Say which markets to look in and what makes a good lead, and the search runs against every source connected for you.',
        action: button('New lead search', {
          kind: 'primary',
          onClick: () => ctx.navigate('/app/research/new'),
        }),
      }));
      return;
    }
    const current = campaign();
    if (!current) return;
    const viewState = state.resultStates[state.view];
    const selected = selectedResult();
    const campaignSelect = el('select', {
      class: 'ifz-select ifz-results-campaign-select',
      'aria-label': 'Active research brief',
      onchange: event => void switchCampaign(event.target.value),
    }, state.campaigns.map(item => el('option', {
      value: item.id,
      selected: item.id === state.campaignId,
    }, item.name)));
    campaignSelect.value = state.campaignId;
    const exportAction = button(`Export ${state.view}`, {
      kind: 'primary',
      icon: 'download',
      disabled: viewState.status !== 'loaded' || !viewState.items.length,
    });
    exportAction.addEventListener('click', () => void exportView(exportAction));
    const tabHost = tabs(
      VIEWS.map(view => ({ key: view, label: tabLabel(view) })),
      state.view,
      view => void switchView(view),
    );

    let listBody;
    if (viewState.status === 'loading' || viewState.status === 'idle') {
      listBody = loadingNode(`Loading ${state.view} research results`);
    } else if (viewState.status === 'error') {
      listBody = el('div', { class: 'ifz-results-error', role: 'alert' },
        el('strong', {}, 'Results could not be loaded'),
        el('p', { class: 'ifz-prose' }, 'The selected brief is unchanged. Choose it again to retry.'));
    } else if (!viewState.items.length) {
      listBody = emptyState({
        icon: 'search',
        title: `No ${VIEW_LABELS[state.view].toLowerCase()}`,
        // A brief that researched nothing at all also lands here, so the copy
        // must not claim an evidence judgement that never ran.
        hint: {
          active: 'No company cleared the strong-fit floor. A brief that researched '
            + 'none lands here too — check the run funnel counts.',
          review: 'Every researched company either qualified or was ruled out.',
          outside_limit: 'Every qualifying company fits inside the list.',
          rejected: 'This brief did not reject any researched company.',
        }[state.view],
      });
    } else {
      listBody = resultTable(viewState.items, state.selected[state.view], chooseResult, locale);
    }

    const newSearchAction = button('New lead search', {
      kind: 'primary',
      onClick: () => ctx.navigate('/app/research/new'),
    });

    page.replaceChildren(
      pageHead({
        title: 'Research results',
        sub: 'Review fit and evidence separately. Unknowns stay visible and every source remains traceable.',
        actions: [campaignSelect, newSearchAction, exportAction],
      }),
      noticeNode(current),
      briefNode(current),
      selectionNode(),
      tabHost,
      el('div', { class: 'ifz-results-workspace' },
        el('section', { class: 'ifz-results-list', 'aria-label': `${sentence(state.view)} company results` }, listBody),
        el('aside', { class: 'ifz-results-detail', 'aria-live': 'polite' },
          evidencePanel(selected, selected ? state.claims.get(selected.id) : null, locale, discoverContacts))),
    );
  }

  async function refreshLive() {
    if (disposed || !state.campaignId) return;
    const campaignId = state.campaignId;
    const view = state.view;
    try {
      const [campaigns, results] = await Promise.all([
        call('researchCampaigns.list'),
        call('researchCampaigns.results', { params: { campaignId }, query: { view } }),
      ]);
      // Separate, and separately allowed to fail: the counts summarize the
      // list, so losing them must never cost the customer the list itself.
      const metrics = await call('researchCampaigns.metrics', { params: { campaignId } })
        .catch(() => null);
      // The customer may have switched brief or tab while this was in flight;
      // landing a stale list on the new selection is worse than not refreshing.
      if (disposed || state.campaignId !== campaignId || state.view !== view) return;
      state.campaigns = itemsOf(campaigns);
      state.metrics = overallMetrics(metrics) || state.metrics;
      const viewState = state.resultStates[view];
      if (viewState.status === 'loaded') {
        const items = itemsOf(results);
        state.resultStates[view] = { status: 'loaded', items };
        // Keep whatever the customer was reading open. It only moves if the
        // result it pointed at is gone.
        if (!items.some(item => item.id === state.selected[view])) {
          state.selected[view] = items[0]?.id || '';
        }
      }
      render();
    } catch {
      // A failed tick is not an error state: the last good list stays on screen
      // and the next tick tries again. A run should not lose its results panel
      // to one dropped request.
    }
  }

  const liveTimer = setInterval(() => {
    if (LIVE_CAMPAIGN_STATES.has(campaign()?.status)) void refreshLive();
  }, LIVE_REFRESH_MS);

  try {
    const response = await call('researchCampaigns.list');
    if (!disposed) {
      state.campaigns = itemsOf(response);
      if (!state.campaigns.some(item => item.id === state.campaignId)) {
        state.campaignId = state.campaigns.find(item => TERMINAL_CAMPAIGN_STATES.has(item.status))?.id
          || state.campaigns[0]?.id
          || '';
      }
      render();
      await Promise.all([loadResults('active'), loadMetrics()]);
    }
  } catch (error) {
    if (!disposed) {
      state.campaignError = error;
      render();
    }
  }

  return () => {
    disposed = true;
    clearInterval(liveTimer);
    pageEl.classList.remove('ifz-page--research-results');
  };
}
