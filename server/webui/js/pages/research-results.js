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

function safeSourceLink(evidence) {
  const value = String(evidence?.provenance_url || '');
  if (!value.startsWith('https://')) return null;
  return el('a', {
    href: value,
    target: '_blank',
    rel: 'noreferrer',
    class: 'ifz-result-source-link',
  }, 'Open source');
}

function evidenceNode(evidence) {
  const link = safeSourceLink(evidence);
  const checked = evidence.retrieved_at
    ? `${fmt.date(evidence.retrieved_at)} at ${fmt.time(evidence.retrieved_at)}`
    : 'Not recorded';
  return el('div', { class: 'ifz-result-citation' },
    el('div', { class: 'ifz-result-citation-head' },
      el('strong', {}, sentence(evidence.source_id, 'Saved source')),
      link),
    el('dl', { class: 'ifz-result-citation-meta' },
      el('div', {}, el('dt', {}, 'Checked'), el('dd', {}, checked)),
      el('div', {}, el('dt', {}, 'Snapshot'), el('dd', {}, evidence.snapshot_id || 'Not recorded')),
      el('div', {}, el('dt', {}, 'SHA-256'),
        el('dd', {}, el('code', {}, evidence.raw_hash || 'Not recorded')))));
}

function claimNode(claim) {
  return el('article', { class: 'ifz-result-claim' },
    el('div', { class: 'ifz-result-claim-head' },
      el('strong', {}, sentence(claim.field)),
      el('span', {}, confidencePercent(claim.confidence))),
    el('div', { class: 'ifz-result-claim-value ifz-prose' }, claimValue(claim.value)),
    (claim.evidence || []).length
      ? el('div', { class: 'ifz-result-citations' }, claim.evidence.map(evidenceNode))
      : el('p', { class: 'ifz-result-uncited ifz-prose' }, 'No cited source is attached to this claim.'));
}

function textList(values, emptyCopy) {
  return values?.length
    ? el('ul', { class: 'ifz-result-text-list ifz-prose' },
        values.map(value => el('li', {}, sentence(value))))
    : el('p', { class: 'ifz-result-none ifz-prose' }, emptyCopy);
}

function evidencePanel(result, claimState) {
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

  return el('section', { class: 'ifz-result-evidence', 'aria-label': `Evidence for ${result.company_name}` },
    el('header', { class: 'ifz-result-evidence-head' },
      el('div', {},
        el('span', { class: 'ifz-overline' }, 'Selected company'),
        el('h2', {}, result.company_name)),
      verdictBadge(result.verdict)),
    el('div', { class: 'ifz-result-evidence-metrics' },
      el('div', {}, el('span', {}, 'Fit'), el('strong', {}, `${result.fit_score} / 100`)),
      el('div', {}, el('span', {}, 'Confidence'), el('strong', {}, confidencePercent(result.evidence_confidence))),
      el('div', {}, el('span', {}, 'Sources'), el('strong', {}, String(result.source_count ?? 0)))),
    el('section', { class: 'ifz-result-evidence-section' },
      el('h3', {}, 'Why this verdict'),
      textList(result.reasons, 'No verdict reason was recorded.')),
    el('section', { class: 'ifz-result-evidence-section' },
      el('h3', {}, 'Supporting claims'),
      supporting.length
        ? el('div', { class: 'ifz-result-claim-list' }, supporting.map(claimNode))
        : el('p', { class: 'ifz-result-none ifz-prose' }, 'No supporting claims were recorded.')),
    el('section', { class: 'ifz-result-evidence-section' },
      el('h3', {}, 'Conflicting claims'),
      conflicting.length || conflictNames.size
        ? el('div', { class: 'ifz-result-claim-list' },
            conflicting.map(claimNode),
            [...conflictNames].map(field => el('p', { class: 'ifz-result-conflict ifz-prose' }, sentence(field))))
        : el('p', { class: 'ifz-result-none ifz-prose' }, 'No conflicting claims were recorded.')),
    el('section', { class: 'ifz-result-evidence-section' },
      el('h3', {}, 'Unknown or not applicable'),
      neutral.length
        ? el('div', { class: 'ifz-result-claim-list neutral' }, neutral.map(claimNode))
        : el('p', { class: 'ifz-result-none ifz-prose' }, 'No neutral claims were recorded.')),
    el('section', { class: 'ifz-result-evidence-section' },
      el('h3', {}, 'Missing evidence'),
      // Not all of these disqualify a lead — a strong fit backed by a registry
      // notice still reports that the company's own page was never read — so
      // the empty state no longer calls them "required".
      textList(result.missing_evidence, 'Nothing is marked missing.')));
}

function resultTable(results, selectedId, onSelect) {
  if (!results.length) return null;
  const headers = ['Company', 'Verdict', 'Fit', 'Confidence', 'Country', 'Buyer role', 'Sources'];
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
        el('td', {}, result.country || 'Not known'),
        el('td', {}, sentence(result.buyer_role)),
        el('td', { class: 'cell-num' }, String(result.source_count ?? 0)));
      }))));
}

export async function mount(root, ctx) {
  let disposed = false;
  const pageEl = root.closest('.ifz-page') || root;
  pageEl.classList.add('ifz-page--research-results');
  const state = {
    campaigns: [],
    campaignId: ctx.query.campaign || '',
    view: 'active',
    resultStates: { active: { status: 'idle', items: [] }, rejected: { status: 'idle', items: [] } },
    selected: { active: '', rejected: '' },
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
      const response = await call('researchResults.claims', { params: { resultId: result.id } });
      if (!contextMatches(context, { selection: true })) return;
      state.claims.set(result.id, { status: 'loaded', items: itemsOf(response) });
    } catch (error) {
      if (!contextMatches(context, { selection: true })) return;
      state.claims.set(result.id, { status: 'error', error, items: [] });
    }
    render();
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
    state.resultStates = {
      active: { status: 'idle', items: [] },
      rejected: { status: 'idle', items: [] },
    };
    state.selected = { active: '', rejected: '' };
    state.claims.clear();
    render();
    await loadResults('active');
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
        el('div', {}, el('dt', {}, state.view === 'active' ? 'Active results' : 'Rejected results'),
          el('dd', {}, String(state.resultStates[state.view].items.length)))));
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
    const tabHost = tabs([
      { key: 'active', label: 'Active' },
      { key: 'rejected', label: 'Rejected' },
    ], state.view, view => void switchView(view));

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
        title: state.view === 'active' ? 'No active results' : 'No rejected results',
        // A brief that researched nothing at all also lands here, so the copy
        // must not claim an evidence judgement that never ran.
        hint: state.view === 'active'
          ? 'No company reached the Active threshold. A brief that researched none lands here too — check the run funnel counts.'
          : 'This brief did not reject any researched companies.',
      });
    } else {
      listBody = resultTable(viewState.items, state.selected[state.view], chooseResult);
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
      tabHost,
      el('div', { class: 'ifz-results-workspace' },
        el('section', { class: 'ifz-results-list', 'aria-label': `${sentence(state.view)} company results` }, listBody),
        el('aside', { class: 'ifz-results-detail', 'aria-live': 'polite' },
          evidencePanel(selected, selected ? state.claims.get(selected.id) : null))),
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
      // The customer may have switched brief or tab while this was in flight;
      // landing a stale list on the new selection is worse than not refreshing.
      if (disposed || state.campaignId !== campaignId || state.view !== view) return;
      state.campaigns = itemsOf(campaigns);
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
      await loadResults('active');
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
