/* Agent run detail — live log for operators.
   Admin-only since Phase 5: run logs are workflow mechanics, and customers get
   business reports instead (company-packs/silverline/business-rules.md:20-21).
   The run list lives in pages/admin.js; this module owns the detail view. */

import { el, card, badge, button, fmt, pageHead, emptyState, kv, toast } from '../ui.js';
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
        action: button('All runs', { kind: 'primary', onClick: () => ctx.navigate('/admin/agent-runs') }),
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
    actions.push(button('Back', { icon: 'arrowLeft', onClick: () => ctx.navigate('/admin/agent-runs') }));
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
        ctx.navigate(`/admin/agent-runs/${res.run_id || res.id}`);
      } }));
    }
    if (run.related?.scan_id) {
      actions.push(button('View buyers', { icon: 'leads', onClick: () => ctx.navigate('/app/buyers') }));
    }
    if (run.related?.campaign_id) {
      actions.push(button('View emails', { icon: 'mail', onClick: () => ctx.navigate('/app/approvals') }));
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
