import { call } from '../api.js';
import { badge, button, card, dataTable, el, emptyState, fmt, input, pageHead } from '../ui.js';

const unwrap = value => value?.items || value || [];

function campaignFunnel(campaign) {
  const metrics = campaign.metrics || {};
  const q = metrics.qualified_leads ?? '—';
  const e = metrics.eligible_companies ?? '—';
  const n = metrics.named_candidates ?? '—';
  return `${q} / ${e} / ${n}`;
}

export async function mount(root, ctx) {
  const result = await call('researchCampaigns.list');
  const campaigns = unwrap(result);
  let status = 'all';
  let query = '';
  const content = el('div');
  const filters = ['all', 'draft', 'running', 'completed', 'partial', 'failed'];

  function render() {
    const term = query.trim().toLowerCase();
    const rows = campaigns.filter(campaign => {
      const config = campaign.config || {};
      const haystack = [campaign.name, ...(config.target_countries || []), ...(config.sector_ids || []), ...(config.product_ids || [])]
        .join(' ').toLowerCase();
      return (status === 'all' || campaign.status === status) && (!term || haystack.includes(term));
    });
    content.replaceChildren(card({ flush: true, class: 'ifz-research-ledger', body: dataTable({
      columns: [
        { key: 'name', label: 'Campaign', render: row => el('div', {},
          el('strong', { class: 'cell-strong' }, row.name),
          el('div', { class: 'cell-muted' }, `Seller ${(row.config?.seller_countries || []).join(', ')}`)) },
        { key: 'targets', label: 'Targets', render: row => (row.config?.target_countries || []).slice(0, 3).join(' · ') || '—' },
        { key: 'sectors', label: 'Sectors', render: row => (row.config?.sector_ids || []).slice(0, 2).join(', ') || '—' },
        { key: 'sources', label: 'Sources', render: row => `${row.config?.enabled_source_ids?.length || 0} selected` },
        { key: 'funnel', label: 'Qualified / eligible / named', render: campaignFunnel },
        { key: 'status', label: 'Status', render: row => badge(row.status) },
        { key: 'updated_at', label: 'Updated', render: row => fmt.relTime(row.updated_at) },
      ],
      rows,
      onRowClick: row => ctx.navigate(`/admin/research/${row.id}`),
      empty: emptyState({
        icon: 'search', title: campaigns.length ? 'No campaigns match' : 'Build your first research campaign',
        hint: campaigns.length
          ? 'Change the status filter or search term.'
          : 'Choose a seller market, target countries, a sector or product, buyer types, and at least one evidence source.',
        action: campaigns.length ? null : button('New research campaign', {
          kind: 'primary', onClick: () => ctx.navigate('/admin/research/new'),
        }),
      }),
    }) }));
  }

  const search = input({ type: 'search', placeholder: 'Search campaign, country, sector or product',
    'aria-label': 'Search research campaigns' });
  search.addEventListener('input', () => { query = search.value; render(); });
  const filterHost = el('div', { class: 'ifz-research-filters', role: 'group', 'aria-label': 'Campaign status' },
    filters.map(value => button(value === 'all' ? 'All' : value, {
      kind: value === status ? 'primary' : 'ghost', size: 'sm', onClick: event => {
        status = value;
        filterHost.querySelectorAll('button').forEach(node => node.classList.remove('primary'));
        event.currentTarget.classList.add('primary');
        render();
      },
    })));
  root.append(
    pageHead({
      title: 'Research',
      sub: 'An evidence ledger for finding and qualifying plausible buyers—source by source, claim by claim.',
      actions: [
        button('New research campaign', { kind: 'primary', icon: 'search', onClick: () => ctx.navigate('/admin/research/new') }),
      ],
    }),
    el('div', { class: 'ifz-research-toolbar' }, search, filterHost),
    content,
  );
  render();
}
