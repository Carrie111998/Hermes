import { call } from '../api.js';
import { badge, card, el, emptyState, fmt, modal } from '../ui.js';
import { renderEvidence } from './research-results.js';

const unwrap = value => value?.items || value || [];
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

export async function openLeadEvidence(lead, requestedLocale = null) {
  const locale = String(requestedLocale || document.documentElement?.lang || 'en')
    .toLowerCase().startsWith('tr') ? 'tr' : 'en';
  const loading = el('div', { class: 'ifz-research-evidence-loading' }, 'Loading source evidence…');
  const dialog = modal({
    title: `Why ${lead.company_name} may fit`,
    body: loading,
    wide: true,
  });
  try {
    const claims = unwrap(await call('research.leadClaims', {
      params: { leadId: lead.id },
      ...(locale === 'tr' ? { query: { locale } } : {}),
    }));
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
                      claim.verified_at ? `Checked ${fmt.date(claim.verified_at)} ${fmt.time(claim.verified_at)}` : null,
                    ].filter(Boolean).join(' · '))
                : null,
              (claim.evidence || []).length
                ? el('div', { class: 'ifz-result-citations' },
                    claim.evidence.map(evidence => renderEvidence(evidence, locale)))
                : el('p', { class: 'ifz-hint ifz-mt-2' }, 'No cited source is attached to this claim.')),
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
