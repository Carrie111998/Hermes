import { call } from '../api.js';
import { badge, button, card, el, emptyState, fmt, kv, modal } from '../ui.js';

const unwrap = value => value?.items || value || [];

export async function openLeadEvidence(lead) {
  const loading = el('div', { class: 'ifz-research-evidence-loading' }, 'Loading verified claims…');
  const dialog = modal({ title: `${lead.company_name} · evidence`, body: loading, wide: true });
  try {
    const claims = unwrap(await call('research.leadClaims', { params: { leadId: lead.id } }));
    loading.replaceWith(claims.length ? el('div', { class: 'ifz-research-claims' }, claims.map(claim =>
      card({
        class: 'ifz-claim-card',
        title: claim.field.replace(/_/g, ' '),
        actions: [badge(claim.status), el('span', { class: 'ifz-confidence-mark' }, `${Math.round(claim.confidence * 100)}% evidence confidence`)],
        body: el('div', {},
          el('div', { class: 'ifz-claim-value' }, claim.value == null ? 'unknown' : Array.isArray(claim.value) ? claim.value.join(', ') : String(claim.value)),
          kv([
            ['Method', claim.method], ['Period', claim.period || 'not time-bound'],
            ['Applicability', claim.applicability], ['Verified', fmt.dateTime(claim.verified_at)],
          ]),
          el('div', { class: 'ifz-evidence-links' }, (claim.evidence || []).map(evidence =>
            el('a', {
              href: evidence.provenance_url || '#', target: evidence.provenance_url ? '_blank' : null,
              rel: evidence.provenance_url ? 'noreferrer' : null,
            }, `${evidence.source_id} · retrieved ${fmt.date(evidence.retrieved_at)}`)))),
      }))) : emptyState({
        icon: 'search', title: 'No feature evidence yet',
        hint: 'Unknown is preserved as a valid state; the next bounded enrichment run can target missing applicable fields.',
      }));
  } catch (error) {
    loading.replaceWith(emptyState({ icon: 'warning', title: 'Evidence unavailable', hint: error.message }));
  }
  return dialog;
}
