import { call } from '../api.js';
import { badge, button, card, csvDownload, dataTable, el, emptyState, fmt, kv, pageHead, tabs, toast } from '../ui.js';
import { openLeadEvidence } from './research-evidence.js';

const unwrap = value => value?.items || value || [];
const FUNNEL = [
  ['raw_records', 'Raw records'], ['named_candidates', 'Named candidates'],
  ['resolved_organizations', 'Resolved organizations'], ['eligible_companies', 'Eligible companies'],
  ['qualified_leads', 'Qualified leads'], ['contactable_leads', 'Contactable leads'],
];

export async function mount(root, ctx) {
  const campaignId = ctx.params.campaignId;
  const [campaign, metricsRes, sourceRes, issueRes, leadRes] = await Promise.all([
    call('researchCampaigns.get', { params: { campaignId } }),
    call('researchCampaigns.metrics', { params: { campaignId } }),
    call('researchCampaigns.sources', { params: { campaignId } }),
    call('researchCampaigns.issues', { params: { campaignId } }),
    call('researchCampaigns.leads', { params: { campaignId } }),
  ]);
  const metrics = unwrap(metricsRes)[0] || {};
  const sources = unwrap(sourceRes);
  const issues = unwrap(issueRes);
  const leads = unwrap(leadRes);
  let active = 'overview';
  const body = el('div');

  function overview() {
    return el('div', { class: 'ifz-grid cols-2' },
      card({ title: 'Campaign contract', body: kv([
        ['Status', campaign.status], ['Seller countries', campaign.config.seller_countries.join(', ')],
        ['Targets', campaign.config.target_countries.join(', ')], ['Sectors', campaign.config.sector_ids.join(', ')],
        ['Buyer types', campaign.config.buyer_types.join(', ')], ['Sources', campaign.config.enabled_source_ids.length],
        ['Updated', fmt.dateTime(campaign.updated_at)], ['Run', campaign.run_id || 'Not started'],
      ]) }),
      card({ title: 'Estimate', body: campaign.estimate?.status === 'available'
        ? el('div', { class: 'ifz-estimate-panel' },
            el('strong', {}, `Qualified leads: ${campaign.estimate.qualified_range.join('–')}`),
            el('p', {}, campaign.estimate.basis),
            el('span', { class: 'ifz-hint' }, `Confidence: ${campaign.estimate.confidence} · ${campaign.estimate.expected_partitions} partitions`))
        : el('div', {}, el('strong', {}, 'No defensible lead-volume estimate yet'),
            el('p', { class: 'ifz-hint' }, campaign.estimate?.basis || 'Estimate the draft after selecting an available source.')) }),
    );
  }

  function funnel() {
    const first = Math.max(1, Number(metrics.raw_records || 0));
    return card({ title: 'Actual ordered funnel', body: el('div', { class: 'ifz-research-funnel' }, FUNNEL.map(([key, label], index) =>
      el('div', { class: 'ifz-funnel-stage' },
        el('span', { class: 'ifz-funnel-index' }, String(index + 1).padStart(2, '0')),
        el('div', {}, el('span', { class: 'ifz-overline' }, label),
          el('strong', {}, String(metrics[key] ?? 0))),
        el('div', { class: 'ifz-funnel-bar' }, el('span', { style: { transform: `scaleX(${Math.max(0.02, Number(metrics[key] || 0) / first)})` } })))) ) });
  }

  function runCost() {
    return card({ title: 'What this run cost', body: el('div', {},
      kv([
        ['Provider requests', metrics.provider_requests ?? '—'],
        ['Bundles reused', metrics.reused_bundles ?? '—'],
        ['Companies enriched', metrics.enriched_companies ?? '—'],
      ]),
      el('p', { class: 'ifz-hint' },
        'A paid fetch per page, so requests are the bill. Reused bundles are evidence still inside its source freshness window and cost nothing.')) });
  }

  function sourceProgress() {
    return card({ title: 'Source progress', flush: true, body: dataTable({
      columns: [
        { key: 'source_id', label: 'Source' }, { key: 'target_country', label: 'Partition' },
        { key: 'status', label: 'Status', render: row => badge(row.status) },
        // These read the keys the run actually writes. They previously read
        // records/normalized/eligible, which nothing has ever stored, so every
        // cell in this table rendered as a dash.
        { key: 'selected', label: 'Selected', render: row => row.metrics?.selected_candidates ?? '—' },
        { key: 'verified', label: 'Verified', render: row => row.metrics?.verified_candidates ?? '—' },
        { key: 'reused', label: 'Reused', render: row => row.metrics?.reused_candidates ?? '—' },
        { key: 'requests', label: 'Requests', render: row => row.metrics?.provider_requests ?? '—' },
        { key: 'error_category', label: 'Coverage note', render: row => row.error_category?.replace(/_/g, ' ') || '—' },
      ], rows: sources,
      empty: emptyState({ icon: 'clock', title: 'No source runs yet', hint: 'Start this campaign to create bounded provider partitions.' }),
    }) });
  }

  function leadTable() {
    return card({ title: 'Qualified leads', flush: true, body: dataTable({
      columns: [
        { key: 'company_name', label: 'Company', render: row => el('div', {}, el('strong', {}, row.company_name), el('div', { class: 'cell-muted' }, row.country)) },
        { key: 'fit_score', label: 'Fit score', render: row => `${row.fit_score} / 100` },
        { key: 'evidence_confidence', label: 'Evidence confidence', render: row => `${Math.round(row.evidence_confidence * 100)}%` },
        { key: 'priority_band', label: 'Priority', render: row => badge(row.priority_band, row.priority_band) },
        { key: 'buyer_type', label: 'Buyer type' },
        { key: 'applicable_feature_completeness', label: 'Completeness', render: row => `${row.applicable_feature_completeness}%` },
        { key: 'evidence', label: '', render: row => button('Inspect evidence', { kind: 'ghost', size: 'sm', onClick: () => openLeadEvidence(row) }) },
      ], rows: leads,
      empty: emptyState({ icon: 'search', title: 'No qualified leads', hint: 'Inspect the funnel and source coverage; this does not mean research failed.' }),
    }) });
  }

  function issueTable() {
    return card({ title: 'Evidence issues', flush: true, body: dataTable({
      columns: [
        { key: 'issue_type', label: 'Issue', render: row => row.issue_type.replace(/_/g, ' ') },
        { key: 'status', label: 'Status', render: row => badge(row.status) },
        { key: 'organization_id', label: 'Organization' },
        { key: 'created_at', label: 'Detected', render: row => fmt.relTime(row.created_at) },
      ], rows: issues,
      empty: emptyState({ icon: 'check', title: 'No unresolved evidence issues' }),
    }) });
  }

  function configuration() {
    return card({ title: 'Effective configuration', body: el('pre', { class: 'ifz-research-config' }, JSON.stringify(campaign.config, null, 2)) });
  }

  const views = {
    overview,
    // Cost sits beside the funnel: what the run moved, and what moving it
    // was worth. It is not a funnel stage — that list is monotonic and its
    // bars are scaled against raw_records.
    funnel: () => el('div', { class: 'ifz-research-stack' }, funnel(), runCost()),
    leads: leadTable, sources: sourceProgress, issues: issueTable, configuration,
  };
  function render() {
    tabHost.replaceWith(tabHost = tabs([
      ['overview', 'Overview'], ['funnel', 'Funnel'], ['leads', `Leads (${leads.length})`],
      ['sources', `Sources (${sources.length})`], ['issues', `Evidence issues (${issues.length})`], ['configuration', 'Configuration'],
    ].map(([key, label]) => ({ key, label })), active, key => { active = key; render(); }));
    body.replaceChildren(views[active]());
  }

  const actions = [];
  if (campaign.status === 'draft') {
    actions.push(button('Edit', { kind: 'ghost', onClick: () => ctx.navigate(`/admin/research/${campaignId}/edit`) }));
    actions.push(button('Start research', { kind: 'primary', onClick: async () => {
      const result = await call('researchCampaigns.start', { params: { campaignId } });
      toast(result.status === 'partial' ? 'Research completed with partial source coverage' : 'Research completed', result.status === 'partial' ? 'warning' : 'success');
      ctx.navigate(`/admin/research/${campaignId}`);
    } }));
  } else {
    actions.push(button('Clone', { kind: 'ghost', onClick: async () => {
      const clone = await call('researchCampaigns.clone', { params: { campaignId } });
      ctx.navigate(`/admin/research/${clone.id}/edit`);
    } }));
    actions.push(button('Export leads', { kind: 'primary', onClick: () => csvDownload(`research-${campaignId}.csv`, leads) }));
  }
  let tabHost = tabs([], active, () => {});
  root.append(pageHead({
    title: campaign.name,
    sub: 'Fit score and evidence confidence remain separate. Unknown claims remain unknown.',
    actions,
  }), el('div', { class: 'ifz-research-statusline' }, badge(campaign.status),
    el('span', {}, `${campaign.config.target_countries.length} target markets`),
    el('span', {}, `${campaign.config.enabled_source_ids.length} evidence sources`)), tabHost, body);
  render();
}
