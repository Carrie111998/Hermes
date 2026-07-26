/* Leads — filterable table + rich lead detail. */

import {
  el, card, badge, scoreBadge, dataTable, button, setBusy, fmt, pageHead, emptyState,
  modal, field, input, select, toast, timeline, kv, icon,
} from '../ui.js';
import { call } from '../api.js';
import { db, subscribe } from '../mocks/db.js';
import { COUNTRY_NAMES, BUYER_INDUSTRIES } from '../catalog.js';
import { exportCsv, waitForRun } from './_page-utils.js';
import { openLeadEvidence } from './research-evidence.js';

const STATUSES = ['new', 'researched', 'contacted', 'replied', 'interested', 'do_not_contact', 'archived'];

/* ---------------- Manual lead modal (shared with custom outreach) ---------------- */
export function openLeadCreateModal({ onCreated } = {}) {
  const nameIn = input({ placeholder: 'Example Appliances GmbH', required: true });
  const websiteIn = input({ placeholder: 'https://example.com' });
  const countrySel = select(Object.entries(COUNTRY_NAMES).map(([v, l]) => ({ value: v, label: l })), { value: 'DE' });
  const cityIn = input({ placeholder: 'Berlin' });
  const industrySel = select(BUYER_INDUSTRIES, { value: BUYER_INDUSTRIES[0] });
  const createBtn = button('Create lead', { kind: 'primary', icon: 'plus' });

  const m = modal({
    title: 'Add lead manually',
    body: el('div', {},
      field('Company name', nameIn, { required: true }),
      el('div', { class: 'ifz-form-row' },
        field('Country', countrySel),
        field('City', cityIn)),
      field('Website', websiteIn),
      field('Industry', industrySel)),
    actions: [button('Cancel', { onClick: () => m.close() }), createBtn],
  });

  createBtn.addEventListener('click', async () => {
    if (!nameIn.value.trim()) { toast('Company name is required', 'warning'); return; }
    setBusy(createBtn, true, 'Creating…');
    try {
      const lead = await call('leads.create', { body: {
        company_name: nameIn.value.trim(),
        website: websiteIn.value.trim(),
        country: countrySel.value,
        city: cityIn.value.trim(),
        industry: industrySel.value,
      } });
      m.close();
      toast(`Lead created: ${lead.company_name}`, 'success');
      if (onCreated) onCreated(lead);
    } catch (err) {
      setBusy(createBtn, false);
      toast(err.message, 'error');
    }
  });
}

/* ---------------- List ---------------- */
export async function mountList(root, ctx) {
  let disposed = false;
  const filters = {
    country: ctx.query.country || '',
    status: ctx.query.status || '',
    scan: ctx.query.scan || '',
    band: '',
    q: '',
  };

  const host = el('div', {});
  root.append(pageHead({
    title: 'Leads',
    sub: 'Companies the agent discovered — plus any you add manually.',
    actions: [
      button('Export CSV', { icon: 'download', onClick: async () => {
        await exportCsv('exports.leads');
      } }),
      button('Add lead', { kind: 'primary', icon: 'plus', onClick: () => openLeadCreateModal({ onCreated: (lead) => ctx.navigate(`/app/leads/${lead.id}`) }) }),
    ],
  }), host);

  await call('leadScans.list');

  async function render() {
    const res = await call('leads.list', { query: filters });
    if (disposed) return;

    let rows = res.items.slice();
    if (filters.country) rows = rows.filter(lead => lead.country === filters.country);
    if (filters.status) rows = rows.filter(lead => lead.status === filters.status);
    if (filters.scan) rows = rows.filter(lead => lead.scan_id === filters.scan);
    if (filters.band) rows = rows.filter(lead => lead.score?.band === filters.band);
    if (filters.q) {
      const q = filters.q.toLowerCase();
      rows = rows.filter(lead => [lead.company_name, lead.city, lead.industry]
        .some(value => String(value || '').toLowerCase().includes(q)));
    }
    const countries = [...new Set(res.items.map(l => l.country).filter(Boolean))];
    const chip = (label, on, fn) => el('button', { class: `ifz-filter-chip${on ? ' on' : ''}`, onclick: fn }, label);
    const searchIn = el('input', {
      class: 'ifz-input', type: 'search', placeholder: 'Search company or city…',
      value: filters.q,
    });
    searchIn.classList.add('ifz-filter-search');
    searchIn.addEventListener('input', () => { filters.q = searchIn.value; renderSoon(); });

    const filterBar = el('div', { class: 'ifz-filters' },
      searchIn,
      chip('All', !filters.country && !filters.status && !filters.scan && !filters.band, () => {
        Object.assign(filters, { country: '', status: '', scan: '', band: '' }); render();
      }),
      countries.map(c => chip(COUNTRY_NAMES[c] || c, filters.country === c, () => { filters.country = filters.country === c ? '' : c; render(); })),
      el('span', { class: 'ifz-filter-spacer', 'aria-hidden': 'true' }),
      STATUSES.slice(0, 5).map(s => chip(s.replace(/_/g, ' '), filters.status === s, () => { filters.status = filters.status === s ? '' : s; render(); })),
      chip('High score', filters.band === 'high', () => { filters.band = filters.band === 'high' ? '' : 'high'; render(); }),
      filters.scan ? chip(`scan: ${(db.leadScans.find(s => s.id === filters.scan) || {}).name || filters.scan} ×`, true, () => { filters.scan = ''; render(); }) : null);

    const table = dataTable({
      columns: [
        { key: 'company_name', label: 'Company', render: l => el('div', {},
          el('div', { class: 'cell-strong' }, l.company_name),
          el('div', { class: 'cell-muted ifz-small' }, l.industry)) },
        { key: 'country', label: 'Market', render: l => el('span', {}, `${COUNTRY_NAMES[l.country] || l.country}`, el('span', { class: 'cell-muted' }, l.city ? ` · ${l.city}` : '')) },
        { key: 'score', label: 'Score', width: '70px', render: l => scoreBadge(l.score) },
        { key: 'status', label: 'Status', render: l => badge(l.status) },
        { key: 'source', label: 'Source', render: l => el('span', { class: 'cell-muted' }, l.source.replace(/_/g, ' ')) },
        { key: 'created_at', label: 'Added', render: l => el('span', { class: 'cell-muted ifz-nowrap' }, fmt.ago(l.created_at)) },
      ],
      rows: rows.sort((a, b) => (b.score?.value || 0) - (a.score?.value || 0)),
      onRowClick: (l) => ctx.navigate(`/app/leads/${l.id}`),
      empty: emptyState({
        icon: 'leads', title: 'No leads match',
        hint: 'Adjust the filters, or run a scan from the Lead Map to discover new companies.',
        action: button('Open Lead Map', { kind: 'primary', onClick: () => ctx.navigate('/app/lead-map') }),
      }),
    });

    host.replaceChildren(filterBar, card({ flush: true, body: table }),
      el('div', { class: 'ifz-hint ifz-mt-2' }, `${rows.length} lead${rows.length === 1 ? '' : 's'} · sorted by score`));
  }

  let pending = null;
  function renderSoon() {
    if (pending) clearTimeout(pending);
    pending = setTimeout(() => { pending = null; render().catch(console.error); }, 250);
  }

  await render();
  const unsub = subscribe('leads', renderSoon);
  return () => { disposed = true; unsub(); if (pending) clearTimeout(pending); };
}

/* ---------------- Detail ---------------- */
export async function mountDetail(root, ctx) {
  let disposed = false;
  const leadId = ctx.params.leadId;
  const host = el('div', {});
  root.append(host);

  async function render() {
    let lead;
    try {
      lead = await call('leads.get', { params: { leadId } });
    } catch {
      host.replaceChildren(emptyState({
        icon: 'leads', title: 'Lead not found',
        action: button('All leads', { kind: 'primary', onClick: () => ctx.navigate('/app/leads') }),
      }));
      return;
    }
    const [research, contactsRes, messagesRes, activityRes, currentScore] = await Promise.all([
      call('research.leadInsights', { params: { leadId } }),
      call('contacts.list', { query: { lead_id: leadId } }),
      call('messages.list', { query: { lead_id: leadId } }),
      call('activity.forLead', { params: { leadId } }),
      call('leads.score', { params: { leadId } }),
    ]);
    lead.score = currentScore;
    contactsRes.items = contactsRes.items.filter(contact => contact.lead_id === leadId);
    messagesRes.items = messagesRes.items.filter(message => message.lead_id === leadId);
    if (disposed) return;

    /* --- header actions --- */
    const researchBtn = button(research.status === 'completed' ? 'Re-research' : 'Research', { icon: 'search', onClick: async (e) => {
      setBusy(e.currentTarget, true, 'Researching…');
      const res = await call('leads.research', { params: { leadId } });
      toast('Research started', 'success', { actionLabel: 'Watch', onAction: () => ctx.navigate(`/app/agent-runs/${res.run_id}`) });
    } });
    const contactsBtn = button('Find contacts', { icon: 'contact', onClick: async (e) => {
      setBusy(e.currentTarget, true, 'Searching…');
      const res = await call('leads.findContacts', { params: { leadId } });
      toast('Contact discovery started', 'success', { actionLabel: 'Watch', onAction: () => ctx.navigate(`/app/agent-runs/${res.run_id}`) });
    } });
    const emailBtn = button('Generate email', { kind: 'primary', icon: 'send', onClick: async (e) => {
      setBusy(e.currentTarget, true, 'Generating…');
      try {
        const result = await call('leads.generateOutreach', { params: { leadId }, body: {} });
        if (result?.type) await waitForRun(result);
        toast('Email generated — review it below', 'success');
        await render();
      } finally {
        setBusy(e.currentTarget, false);
      }
    } });
    const moreBtn = button('Do not contact', { kind: 'ghost', icon: 'ban', onClick: async () => {
      await call('leads.markDoNotContact', { params: { leadId } });
      toast('Marked as do-not-contact', 'warning');
      render();
    } });

    /* --- score explanation --- */
    const hasEvidenceScore = Number.isFinite(Number(lead.fit_score)) && Number.isFinite(Number(lead.evidence_confidence));
    const scorePanel = card({
      title: 'Fit & evidence',
      body: el('div', {},
        hasEvidenceScore ? el('div', { class: 'ifz-grid cols-2 ifz-mb-4' },
          el('div', { class: 'ifz-evidence-score-block' },
            el('span', { class: 'ifz-overline' }, 'Fit score'),
            el('strong', {}, `${lead.fit_score} / 100`),
            el('span', { class: 'ifz-hint' }, `Priority ${lead.priority_band} · business relevance`)),
          el('div', { class: 'ifz-evidence-score-block' },
            el('span', { class: 'ifz-overline' }, 'Evidence confidence'),
            el('strong', {}, Number(lead.evidence_confidence).toFixed(2)),
            el('span', { class: 'ifz-hint' }, 'Authority, corroboration, freshness, conflicts and estimate share'))) : null,
        el('div', { class: 'ifz-row', style: { gap: '12px', marginBottom: '12px' } },
          el('span', { class: 'ifz-stat-value', style: { fontSize: '34px' } }, String(lead.score.value)),
          el('div', {},
            badge(lead.score.band === 'high' ? 'active' : lead.score.band === 'mid' ? 'pending' : 'new', `${lead.score.band} priority`),
            el('div', { class: 'ifz-hint ifz-mt-2' }, 'Weighted against the Company Brain ICP'))),
        lead.score.factors.map(f =>
          el('div', { class: 'ifz-hbar-row' },
            el('span', { class: 'ifz-bars-label' }, f.label),
            el('div', { class: 'ifz-hbar-track', title: f.note },
              el('div', { class: 'ifz-hbar-fill', style: { width: `${f.weight * 2.4}%` } })),
            el('span', { class: 'ifz-hbar-val' }, `${f.weight}%`))),
        hasEvidenceScore ? button('Inspect claim evidence', { size: 'sm', icon: 'search', onClick: () => openLeadEvidence(lead) }) : null,
        button('Recalculate', { size: 'sm', icon: 'refresh', onClick: async () => {
          await call('leads.scoreRecalculate', { params: { leadId } });
          toast('Score recalculated', 'success');
          render();
        } })),
    });

    /* --- research panel --- */
    const researchPanel = card({
      title: 'Research & insights',
      actions: research.status === 'completed' ? badge('completed') : badge('pending', 'not researched'),
      body: research.status === 'completed'
        ? el('div', {},
            el('p', { style: { lineHeight: 1.6, marginBottom: '14px' } }, research.summary),
            research.insights.map(i =>
              el('div', { style: { marginBottom: '12px' } },
                el('div', { class: 'ifz-strong', style: { marginBottom: '2px' } }, i.title),
                el('div', { class: 'ifz-muted', style: { lineHeight: 1.55 } }, i.body))))
        : emptyState({
            icon: 'search', title: 'Not researched yet',
            hint: 'Let the agent crawl this company and produce insights.',
          }),
    });

    /* --- contacts panel --- */
    const contactRows = contactsRes.items;
    const contactsPanel = card({
      title: `Contacts (${contactRows.length})`,
      flush: contactRows.length > 0,
      body: contactRows.length
        ? dataTable({
            columns: [
              { key: 'name', label: 'Name', render: c => el('div', {},
                el('div', { class: 'cell-strong' }, c.name),
                el('div', { class: 'cell-muted ifz-small' }, c.title)) },
              { key: 'email', label: 'Email', render: c => el('div', {},
                el('span', { class: 'ifz-mono' }, c.email || '—'),
                ' ', badge(c.email_status)) },
              { key: 'linkedin_url', label: '', width: '46px', render: c => c.linkedin_url
                ? el('a', { href: c.linkedin_url, target: '_blank', rel: 'noopener', title: 'Open LinkedIn profile', onclick: (e) => e.stopPropagation() }, icon('linkedin', 15))
                : '' },
            ],
            rows: contactRows,
            onRowClick: (c) => ctx.navigate(`/app/contacts/${c.id}`),
          })
        : emptyState({ icon: 'contact', title: 'No contacts yet', hint: 'Run contact discovery to find buyers at this company.' }),
    });

    /* --- outreach panel --- */
    const outreachPanel = card({
      title: `Outreach (${messagesRes.items.length})`,
      body: messagesRes.items.length
        ? el('div', {}, messagesRes.items.map(m =>
            el('div', { class: 'ifz-actionrow', style: { cursor: 'pointer' }, onclick: () => ctx.navigate(`/app/outreach?message=${m.id}`) },
              el('span', { class: 'ifz-actionrow-icon' }, icon(m.channel === 'whatsapp' ? 'whatsapp' : 'mail', 15)),
              el('div', { class: 'ifz-actionrow-body' },
                el('div', { class: 'ifz-actionrow-title' }, m.subject || '(WhatsApp message)'),
                el('div', { class: 'ifz-actionrow-sub' }, `${m.language.toUpperCase()} · ${m.sent_at ? fmt.ago(m.sent_at) : 'not sent'}`)),
              badge(m.status))))
        : emptyState({ icon: 'mail', title: 'No outreach yet', hint: 'Generate a personalized email — you approve before anything is sent.' }),
    });

    /* --- activity --- */
    const activityPanel = card({
      title: 'Activity',
      body: timeline(activityRes.items.map(a => ({
        label: a.label,
        time: fmt.ago(a.at),
        tone: a.kind === 'reply' ? 'success' : a.kind === 'agent' ? 'accent' : '',
      }))),
    });

    host.replaceChildren(
      pageHead({
        title: lead.company_name,
        sub: null,
        actions: [researchBtn, contactsBtn, emailBtn, moreBtn],
      }),
      el('div', { class: 'ifz-row wrap ifz-mb-4', style: { gap: '10px' } },
        scoreBadge(lead.score),
        badge(lead.status),
        el('span', { class: 'ifz-tag' }, lead.industry),
        el('span', { class: 'ifz-muted ifz-small' }, `${COUNTRY_NAMES[lead.country] || lead.country}${lead.city ? ' · ' + lead.city : ''}`),
        lead.website ? el('a', { href: lead.website, target: '_blank', rel: 'noopener', class: 'ifz-small' }, lead.website.replace(/^https?:\/\//, '')) : null,
        el('span', { class: 'ifz-faint ifz-small' }, `via ${lead.source.replace(/_/g, ' ')}`)),
      el('div', { class: 'ifz-grid cols-2' },
        el('div', { class: 'ifz-col', style: { gap: '16px' } }, researchPanel, outreachPanel),
        el('div', { class: 'ifz-col', style: { gap: '16px' } }, scorePanel, contactsPanel, activityPanel)));
  }

  await render();
  let pending = null;
  const unsub = subscribe('*', () => {
    if (pending) return;
    pending = setTimeout(() => { pending = null; render().catch(console.error); }, 1200);
  });
  return () => { disposed = true; unsub(); if (pending) clearTimeout(pending); };
}
