/* Admin Documents — what was uploaded, what the agent read, and what it found.

   This is the operator's side of the document pipeline. Customers see a status
   and a sentence; here we show both stored forms, every attempt with its
   technical reason code, the extracted records, and the sources the run
   consulted — because "why is this document empty?" is only answerable with
   all four in one place.

   Internal vocabulary is allowed on this page and nowhere else. The processed
   sidecar is called "Processed (.md) artifact"; the processor behind it is
   still not named, so swapping it never turns into a UI change. */

import { call } from '../api.js';
import {
  badge, blobDownload, button, card, dataTable, el, emptyState, fmt, kv, modal, toast,
} from '../ui.js';
import { withAdmin } from './admin.js';

const STATUS_LABELS = {
  uploaded: 'Uploaded',
  processing: 'Processing',
  ready: 'Ready',
  needs_attention: 'Needs attention',
  failed: 'Failed',
};

const ORIGIN_LABELS = {
  onboarding_upload: 'Onboarding upload',
  desktop: 'Desktop attachment',
  messaging: 'Messaging attachment',
  cli: 'CLI',
};

const statusBadge = (status) => badge(status, STATUS_LABELS[status] || status);

/* JSON rendered as escaped text nodes, never as parsed markup: this content is
   model-authored and passes through an admin's browser. */
function jsonBlock(value) {
  if (value == null) return el('div', { class: 'ifz-small ifz-muted' }, 'None');
  return el('pre', { class: 'ifz-runlog ifz-small' },
    el('code', {}, JSON.stringify(value, null, 2)));
}

function recordList(items, empty) {
  if (!items || !items.length) return el('div', { class: 'ifz-small ifz-muted' }, empty);
  return jsonBlock(items);
}

async function fetchArtifact(documentId, role) {
  return call('admin.documents.artifact', { params: { documentId, role } });
}

async function previewArtifact(documentId, role, filename) {
  let file;
  try {
    file = await fetchArtifact(documentId, role);
  } catch (err) {
    toast(err.message || 'Could not open the file', 'error');
    return;
  }

  const isText = /\.(md|txt|csv|json|xml|ya?ml|html?)$/i.test(file.filename || filename || '');
  const body = isText
    ? el('pre', { class: 'ifz-runlog' }, el('code', {}, await file.blob.text()))
    : el('div', { class: 'ifz-small ifz-muted' },
        'This file has no text preview. Download it to inspect the original bytes.');

  modal({
    title: file.filename || filename || role,
    wide: true,
    body,
    actions: [
      button('Download', {
        kind: 'primary',
        icon: 'download',
        onClick: () => blobDownload(file.filename || filename || role, file.blob),
      }),
    ],
  });
}

async function downloadArtifact(documentId, role, filename) {
  try {
    const file = await fetchArtifact(documentId, role);
    blobDownload(file.filename || filename || role, file.blob);
  } catch (err) {
    toast(err.message || 'Could not download the file', 'error');
  }
}

/* ---------------- List ---------------- */

export async function mountList(root, ctx) {
  const [documents, companies] = await Promise.all([
    call('admin.documents.list'),
    call('admin.companies.list').catch(() => ({ items: [] })),
  ]);

  const rows = documents.items || [];
  const companyName = new Map((companies.items || []).map(c => [c.id, c.name]));

  const body = rows.length
    ? card({ flush: true, body: dataTable({
        columns: [
          {
            key: 'name',
            label: 'Document',
            render: r => el('div', { class: 'ifz-col' },
              el('span', { class: 'ifz-strong' }, r.name),
              el('span', { class: 'ifz-small ifz-muted' },
                `${r.content_type || 'unknown type'} · ${fmt.num(r.size_bytes)} bytes`)),
          },
          {
            key: 'company',
            label: 'Company',
            render: r => r.company_name || companyName.get(r.company_id) || r.company_id,
          },
          { key: 'status', label: 'Status', render: r => statusBadge(r.status) },
          {
            key: 'origin',
            label: 'Origin',
            render: r => ORIGIN_LABELS[r.origin] || r.origin || '—',
          },
          {
            key: 'processed',
            label: 'Processed (.md) artifact',
            render: r => r.has_processed_artifact ? 'Available' : '—',
          },
          { key: 'created', label: 'Uploaded', render: r => fmt.ago(r.created_at) },
        ],
        rows,
        onRowClick: r => ctx.navigate(`/admin/documents/${r.id}`),
      }) })
    : emptyState({
        icon: 'search',
        title: 'No documents yet',
        hint: 'Uploads from onboarding, chat, and messaging appear here.',
      });

  withAdmin(root, ctx, 'Documents',
    'Every uploaded document, both stored forms, and what the agent read.',
    '/admin/documents', body);
}

/* ---------------- Detail ---------------- */

export async function mountDetail(root, ctx) {
  const documentId = ctx.params.documentId;

  async function render() {
    let detail;
    try {
      detail = await call('admin.documents.detail', { params: { documentId } });
    } catch {
      withAdmin(root, ctx, 'Document', null, '/admin/documents',
        emptyState({
          icon: 'search',
          title: 'Document not found',
          action: button('All documents', {
            kind: 'primary', onClick: () => ctx.navigate('/admin/documents'),
          }),
        }));
      return;
    }

    const doc = detail.document;
    const original = (detail.artifacts || []).find(a => a.role === 'original');
    const processed = (detail.artifacts || []).find(
      a => a.id === doc.active_processed_artifact_id,
    );
    const run = detail.agent_run;

    const artifactCard = (title, artifact, role) => card({
      title,
      body: artifact
        ? el('div', { class: 'ifz-col' },
            kv([
              ['File', artifact.filename],
              ['Type', artifact.content_type],
              ['Size', `${artifact.size_bytes} bytes`],
              ['Checksum', artifact.checksum.slice(0, 16) + '…'],
            ]),
            el('div', { class: 'ifz-row ifz-mt-2' },
              button('Preview', {
                size: 'sm',
                onClick: () => previewArtifact(documentId, role, artifact.filename),
              }),
              button('Download', {
                size: 'sm',
                icon: 'download',
                onClick: () => downloadArtifact(documentId, role, artifact.filename),
              })))
        : el('div', { class: 'ifz-small ifz-muted' }, 'Not available'),
    });

    const attemptsCard = card({
      title: 'Processing attempts',
      flush: true,
      body: (detail.attempts || []).length
        ? dataTable({
            columns: [
              { key: 'status', label: 'Result', render: a => statusBadge(a.public_status) },
              { key: 'stage', label: 'Stage', render: a => a.internal_stage || '—' },
              { key: 'reason', label: 'Reason code', render: a => a.reason_code || '—' },
              { key: 'diagnostic', label: 'Diagnostic', render: a => a.diagnostic || '—' },
              { key: 'started', label: 'Started', render: a => fmt.ago(a.started_at) },
              {
                key: 'duration',
                label: 'Duration',
                render: a => a.completed_at
                  ? `${(a.completed_at - a.started_at).toFixed(1)}s`
                  : 'running',
              },
            ],
            rows: detail.attempts,
          })
        : el('div', { class: 'ifz-small ifz-muted' }, 'No attempts recorded'),
    });

    const actions = [
      button('All documents', {
        icon: 'arrowLeft', onClick: () => ctx.navigate('/admin/documents'),
      }),
      button('Retry processing', {
        icon: 'refresh',
        onClick: async () => {
          await call('admin.documents.retry', { params: { documentId } });
          toast('Processing restarted', 'success');
          render();
        },
      }),
      button('Delete', {
        kind: 'danger',
        onClick: () => modal({
          title: 'Delete this document?',
          body: el('div', { class: 'ifz-col' },
            el('p', {}, `“${doc.name}” and both stored forms will be removed. This cannot be undone.`)),
          actions: [
            button('Delete permanently', {
              kind: 'danger',
              onClick: async () => {
                await call('admin.documents.delete', { params: { documentId } });
                toast('Document deleted', 'warning');
                ctx.navigate('/admin/documents');
              },
            }),
          ],
        }),
      }),
    ];

    withAdmin(root, ctx, doc.name,
      `${STATUS_LABELS[doc.status] || doc.status}${doc.status_detail ? ` — ${doc.status_detail}` : ''}`,
      '/admin/documents',
      el('div', {},
        el('div', { class: 'ifz-grid cols-3 ifz-mb-4' },
          card({ body: el('div', { class: 'ifz-col' },
            el('span', { class: 'ifz-overline' }, 'Status'),
            statusBadge(doc.status)) }),
          card({ body: el('div', { class: 'ifz-col' },
            el('span', { class: 'ifz-overline' }, 'Origin'),
            el('span', { class: 'ifz-strong' }, ORIGIN_LABELS[doc.origin] || doc.origin || '—')) }),
          card({ body: el('div', { class: 'ifz-col' },
            el('span', { class: 'ifz-overline' }, 'Timing'),
            kv([
              ['Uploaded', fmt.ago(doc.created_at)],
              ['Ready', doc.ready_at ? fmt.ago(doc.ready_at) : '—'],
            ])) })),
        el('div', { class: 'ifz-grid cols-2 ifz-mb-4' },
          artifactCard('Original', original, 'original'),
          artifactCard('Processed (.md) artifact', processed, 'processed')),
        attemptsCard,
        el('div', { class: 'ifz-grid cols-2 ifz-mt-4' },
          card({ title: 'Extracted records', body: recordList(detail.records, 'No records extracted') }),
          card({ title: 'Rejects', body: recordList(detail.rejects, 'No rejects') })),
        run
          ? el('div', { class: 'ifz-mt-4' },
              card({
                title: 'Agent run',
                actions: [button('Open run', {
                  size: 'sm', onClick: () => ctx.navigate(`/admin/agent-runs/${run.id}`),
                })],
                body: el('div', { class: 'ifz-col' },
                  kv([
                    ['Run', run.id],
                    ['Status', run.status],
                    ['Related', Object.entries(run.related || {})
                      .map(([k, v]) => `${k}=${v}`).join(', ') || '—'],
                  ]),
                  el('span', { class: 'ifz-overline ifz-mt-2' }, 'Final output'),
                  jsonBlock(run.output),
                  el('span', { class: 'ifz-overline ifz-mt-2' }, 'Evidence'),
                  evidenceTable(run.evidence)),
              }))
          : null),
      actions);
  }

  await render();
}

export function evidenceTable(evidence) {
  if (!evidence || !evidence.length) {
    return el('div', { class: 'ifz-small ifz-muted' }, 'No sources recorded');
  }
  return dataTable({
    columns: [
      { key: 'type', label: 'Type', render: e => e.source_type },
      {
        key: 'source',
        label: 'Source',
        render: e => e.source_url || e.file_reference || '—',
      },
      { key: 'title', label: 'Title', render: e => e.title || '—' },
      {
        key: 'retrieved',
        label: 'Retrieved',
        render: e => e.retrieved_at ? fmt.ago(e.retrieved_at) : '—',
      },
      {
        key: 'result',
        label: 'Result',
        render: e => el('span', { class: 'ifz-small' },
          e.result == null ? '—' : JSON.stringify(e.result).slice(0, 120)),
      },
    ],
    rows: evidence,
  });
}
