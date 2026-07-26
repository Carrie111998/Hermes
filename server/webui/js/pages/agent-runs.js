/* Agent runs — history table + live-log run detail. */

import { el, card, badge, dataTable, button, fmt, pageHead, emptyState, kv, toast } from '../ui.js';
import { call } from '../api.js';
import { subscribe } from '../mocks/db.js';

const RUN_TYPE_LABELS = {
  company_brain_build: 'Company Brain',
  document_processing: 'Documents',
  product_extraction: 'Products',
  lead_scan: 'Lead scan',
  lead_research: 'Research',
  contact_discovery: 'Contacts',
  outreach_generation: 'Outreach',
  email_send: 'Email send',
  whatsapp_send: 'WhatsApp',
  linkedin_note_generation: 'LinkedIn',
  analytics_refresh: 'Analytics',
};

export async function mountList(root, ctx) {
  let disposed = false;
  const host = el('div', {});
  root.append(pageHead({
    title: 'Agent Runs',
    sub: 'Everything the agent does is logged here — watch running tasks live or audit past work.',
  }), host);

  let typeFilter = '';
  let statusFilter = '';

  async function render() {
    const res = await call('agentRuns.list', { query: { type: typeFilter, status: statusFilter } });
    if (disposed) return;
    let rows = res.items.slice();
    if (typeFilter) rows = rows.filter(run => run.type === typeFilter);
    if (statusFilter) rows = rows.filter(run => run.status === statusFilter);

    const filters = el('div', { class: 'ifz-filters' },
      ['', 'running', 'completed', 'cancelled'].map(s =>
        el('button', {
          class: `ifz-filter-chip${statusFilter === s ? ' on' : ''}`,
          onclick: () => { statusFilter = s; render(); },
        }, s === '' ? 'All statuses' : s)),
      el('span', { class: 'ifz-filter-spacer', 'aria-hidden': 'true' }),
      ['', 'lead_scan', 'lead_research', 'contact_discovery', 'outreach_generation', 'email_send', 'company_brain_build'].map(t =>
        el('button', {
          class: `ifz-filter-chip${typeFilter === t ? ' on' : ''}`,
          onclick: () => { typeFilter = t; render(); },
        }, t === '' ? 'All types' : RUN_TYPE_LABELS[t] || t)));

    const table = dataTable({
      columns: [
        { key: 'label', label: 'Run', render: r => el('span', { class: 'cell-strong' }, r.label) },
        { key: 'type', label: 'Type', render: r => el('span', { class: 'ifz-tag' }, RUN_TYPE_LABELS[r.type] || r.type) },
        { key: 'status', label: 'Status', render: r => badge(r.status) },
        { key: 'progress', label: 'Progress', width: '140px', render: r =>
          r.status === 'running'
            ? el('div', { class: 'ifz-progress' }, el('div', { class: 'ifz-progress-fill', style: { width: `${r.progress}%` } }))
            : el('span', { class: 'cell-muted' }, r.status === 'completed' ? '100%' : '—') },
        { key: 'created_at', label: 'Started', render: r => el('span', { class: 'cell-muted' }, fmt.ago(r.created_at)) },
      ],
      rows,
      onRowClick: (r) => ctx.navigate(`/app/agent-runs/${r.id}`),
      empty: emptyState({ icon: 'bolt', title: 'No runs match', hint: 'Start a lead scan from the Lead Map to see the agent at work.' }),
    });

    host.replaceChildren(filters, card({ flush: true, body: table }));
  }

  await render();
  const poll = setInterval(() => render().catch(console.error), 1800);
  let pending = null;
  const unsub = subscribe('runs', () => {
    if (pending) return;
    pending = setTimeout(() => { pending = null; render().catch(console.error); }, 900);
  });
  return () => { disposed = true; unsub(); clearInterval(poll); if (pending) clearTimeout(pending); };
}

export async function mountDetail(root, ctx) {
  let disposed = false;
  const runId = ctx.params.runId;
  const host = el('div', {});
  root.append(host);

  let stickToBottom = true;

  async function render() {
    let run;
    try {
      const [record, logs] = await Promise.all([
        call('agentRuns.get', { params: { runId } }),
        call('agentRuns.events', { params: { runId } }),
      ]);
      run = { ...record, logs: logs.items };
    } catch {
      host.replaceChildren(emptyState({
        icon: 'bolt', title: 'Run not found',
        action: button('All runs', { kind: 'primary', onClick: () => ctx.navigate('/app/agent-runs') }),
      }));
      return;
    }
    if (disposed) return;

    const logHost = el('div', { class: 'ifz-runlog' },
      run.logs.length
        ? run.logs.map(l => el('span', { class: 'line' },
            el('span', { class: 't' }, fmt.time(l.t)),
            el('span', { class: l.cls || '' }, l.line)))
        : el('span', { class: 'line' }, el('span', { class: 't' }, '·'), 'Waiting for log output…'));
    logHost.addEventListener('scroll', () => {
      stickToBottom = logHost.scrollTop + logHost.clientHeight >= logHost.scrollHeight - 24;
    });

    const actions = [];
    actions.push(button('Back', { icon: 'arrowLeft', onClick: () => ctx.navigate('/app/agent-runs') }));
    if (run.status === 'running') {
      actions.push(button('Cancel run', { kind: 'danger', onClick: async () => {
        await call('agentRuns.cancel', { params: { runId } });
        toast('Run cancelled', 'warning');
        render();
      } }));
    } else {
      actions.push(button('Retry', { icon: 'refresh', onClick: async () => {
        const res = await call('agentRuns.retry', { params: { runId } });
        toast('Run restarted', 'success');
        ctx.navigate(`/app/agent-runs/${res.run_id || res.id}`);
      } }));
    }
    if (run.related?.scan_id) {
      actions.push(button('View scan leads', { icon: 'leads', onClick: () => ctx.navigate(`/app/leads?scan=${run.related.scan_id}`) }));
    }
    if (run.related?.campaign_id) {
      actions.push(button('View campaign', { icon: 'mail', onClick: () => ctx.navigate(`/app/outreach/campaigns/${run.related.campaign_id}`) }));
    }

    host.replaceChildren(
      pageHead({ title: run.label, sub: null, actions }),
      el('div', { class: 'ifz-grid cols-3 ifz-mb-4' },
        card({ body: el('div', { class: 'ifz-col' },
          el('span', { class: 'ifz-overline' }, 'Status'),
          el('div', { class: 'ifz-row' }, badge(run.status),
            run.status === 'running' ? el('span', { class: 'ifz-small ifz-muted' }, `${run.progress}%`) : null),
          run.status === 'running'
            ? el('div', { class: 'ifz-progress ifz-mt-2' }, el('div', { class: 'ifz-progress-fill', style: { width: `${run.progress}%` } }))
            : null) }),
        card({ body: el('div', { class: 'ifz-col' },
          el('span', { class: 'ifz-overline' }, 'Type'),
          el('span', { class: 'ifz-strong' }, RUN_TYPE_LABELS[run.type] || run.type)) }),
        card({ body: el('div', { class: 'ifz-col' },
          el('span', { class: 'ifz-overline' }, 'Timing'),
          kv([
            ['Started', fmt.ago(run.created_at)],
            ['Finished', run.finished_at ? fmt.ago(run.finished_at) : '—'],
          ])) })),
      card({ title: 'Live log', body: logHost }));

    if (stickToBottom) logHost.scrollTop = logHost.scrollHeight;
  }

  await render();
  const poll = setInterval(() => render().catch(console.error), 1000);
  const unsub = subscribe('runs', (run) => {
    if (run && run.id === runId) render().catch(console.error);
  });
  return () => { disposed = true; unsub(); clearInterval(poll); };
}
