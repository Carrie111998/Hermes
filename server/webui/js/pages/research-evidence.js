import { call } from '../api.js';
import { badge, card, el, emptyState, fmt, modal } from '../ui.js';

const unwrap = value => value?.items || value || [];
const SOURCE_LABELS = Object.freeze({
  web: 'Public company source',
  web_search: 'Public company source',
  trade_data: 'Trade data',
  exhibitor_lists: 'Trade fair listing',
  company_registries: 'Company registry',
  linkedin_reference: 'Public professional profile',
  uploaded_internal_data: 'Your company records',
  manual: 'Added by your team',
});

function sentenceLabel(value, fallback = 'Buyer signal') {
  const text = String(value || fallback).replace(/[_-]+/g, ' ').trim();
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : fallback;
}

function claimValue(value) {
  if (value == null || value === '') return 'Not known';
  if (Array.isArray(value)) return value.length ? value.join(', ') : 'Not known';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  return String(value);
}

function confidenceCopy(claim) {
  if (claim.status === 'estimated' || claim.method === 'estimated') return 'Partly estimated';
  const confidence = Number(claim.confidence);
  if (!Number.isFinite(confidence)) return 'Source quality not known';
  const normalized = confidence <= 1 ? confidence * 100 : confidence;
  if (normalized >= 75) return 'Supported by a strong source';
  if (normalized >= 45) return 'Supported by limited evidence';
  return 'Early evidence';
}

function evidenceLink(evidence) {
  const label = SOURCE_LABELS[evidence.source_id]
    || sentenceLabel(evidence.source_id, 'Saved source');
  const retrieved = evidence.retrieved_at ? ` · checked ${fmt.date(evidence.retrieved_at)}` : '';
  if (!evidence.provenance_url) return el('span', {}, `${label}${retrieved}`);
  return el('a', {
    href: evidence.provenance_url,
    target: '_blank',
    rel: 'noreferrer',
  }, `${label}${retrieved}`);
}

export async function openLeadEvidence(lead) {
  const loading = el('div', { class: 'ifz-research-evidence-loading' }, 'Loading source evidence…');
  const dialog = modal({
    title: `Why ${lead.company_name} may fit`,
    body: loading,
    wide: true,
  });
  try {
    const claims = unwrap(await call('research.leadClaims', { params: { leadId: lead.id } }));
    loading.replaceWith(claims.length
      ? el('div', { class: 'ifz-research-claims' }, claims.map(claim =>
          card({
            class: 'ifz-claim-card',
            title: sentenceLabel(claim.field),
            actions: [
              badge(claim.status || 'unknown'),
              el('span', { class: 'ifz-confidence-mark' }, confidenceCopy(claim)),
            ],
            body: el('div', {},
              el('div', { class: 'ifz-claim-value' }, claimValue(claim.value)),
              claim.period || claim.verified_at
                ? el('p', { class: 'ifz-hint ifz-mt-2' },
                    [
                      claim.period ? sentenceLabel(claim.period, 'No date range') : null,
                      claim.verified_at ? `Checked ${fmt.dateTime(claim.verified_at)}` : null,
                    ].filter(Boolean).join(' · '))
                : null,
              el('div', { class: 'ifz-evidence-links' },
                (claim.evidence || []).map(evidence => evidenceLink(evidence)))),
          })))
      : emptyState({
          icon: 'search',
          title: 'No source evidence yet',
          hint: 'Anything we do not know stays marked as Not known until the buyer is researched.',
        }));
  } catch {
    loading.replaceWith(emptyState({
      icon: 'warning',
      title: 'Source evidence is unavailable',
      hint: 'The buyer record is safe. Try opening the evidence again.',
    }));
  }
  return dialog;
}
