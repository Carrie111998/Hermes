/* Agent run detail — live log for operators.
   Admin-only since Phase 5: run logs are workflow mechanics, and customers get
   business reports instead (company-packs/silverline/business-rules.md:20-21).
   The run list lives in pages/admin.js; this module owns the detail view. */

import { el, card, badge, button, fmt, pageHead, emptyState, kv, toast } from '../ui.js';
import { call } from '../api.js';
import { subscribe } from '../mocks/db.js';
import { evidenceTable } from './admin-documents.js';

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

const RELATED_ROUTES = {
  document_id: id => `/admin/documents/${id}`,
  source_document_id: id => `/admin/documents/${id}`,
};

/* What the run produced and what it looked at.

   Credentials, prompt internals, and raw tool arguments are already stripped
   server-side (server/agent_evidence.py) — this only renders what survived,
   as escaped text nodes rather than parsed markup. */
function resultCards(detail, ctx) {
  if (!detail) return null;

  const related = Object.entries(detail.related || {});
  return el('div', { class: 'ifz-mt-4' },
    card({
      title: 'Final output',
      body: detail.output
        ? el('pre', { class: 'ifz-runlog ifz-small' },
            el('code', {}, JSON.stringify(detail.output, null, 2)))
        : el('div', { class: 'ifz-small ifz-muted' }, 'No structured output recorded'),
    }),
    related.length
      ? el('div', { class: 'ifz-mt-4' }, card({
          title: 'Related',
          body: el('div', { class: 'ifz-row' }, related.map(([key, value]) => {
            const href = RELATED_ROUTES[key]?.(value);
            return href
              ? button(`${key.replace(/_/g, ' ')}: ${value}`, {
                  size: 'sm', onClick: () => ctx.navigate(href),
                })
              : el('span', { class: 'ifz-small ifz-muted' }, `${key}: ${value}`);
          })),
        }))
      : null,
    el('div', { class: 'ifz-mt-4' },
      card({ title: 'Sources', flush: true, body: evidenceTable(detail.evidence) })));
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
      const [record, logs, detail] = await Promise.all([
        call('agentRuns.get', { params: { runId } }),
        call('agentRuns.events', { params: { runId } }),
        // Cross-company: this page is admin-only and a run may belong to any
        // tenant. Optional so a live run still renders if detail lags.
        call('admin.agentRuns.detail', { params: { runId } }).catch(() => null),
      ]);
      run = { ...record, logs: logs.items, detail };
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
            ? el('div', { class: 'ifz-progress ifz-mt-2' }, el('div', { class: 'ifz-progress-fill', style: { transform: `scaleX(${(run.progress || 0) / 100})` } }))
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
      card({ title: 'Live log', body: logHost }),
      resultCards(run.detail, ctx));

    if (stickToBottom) logHost.scrollTop = logHost.scrollHeight;
  }

  await render();
  const poll = setInterval(() => render().catch(console.error), 1000);
  const unsub = subscribe('runs', (run) => {
    if (run && run.id === runId) render().catch(console.error);
  });
  return () => { disposed = true; unsub(); clearInterval(poll); };
}
