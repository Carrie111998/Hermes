/* Contacts - buyer people found by the agent or added manually. */

import {
  el, card, badge, dataTable, button, fmt, pageHead, emptyState, toast, kv, timeline,
  input,
} from '../ui.js';
import { call, config } from '../api.js';
import { db, subscribe } from '../mocks/db.js';
import {
  countryName, leadFor, recordTitle, openContactModal, compactList, emailPreview, exportCsv,
} from './_page-utils.js';

const EMAIL_STATUSES = ['', 'verified', 'unverified', 'not_found'];

export async function mountList(root, ctx) {
  let disposed = false;
  const filters = {
    q: ctx.query.q || '',
    status: ctx.query.status || '',
    country: ctx.query.country || '',
    dnc: '',
  };
  const host = el('div', {});

  root.append(pageHead({
    title: 'Contacts',
    sub: 'Buyer contacts discovered from research, uploaded lists, and manual adds.',
    actions: [
      button('Export CSV', { icon: 'download', onClick: async () => {
        await exportCsv('exports.contacts');
      } }),
      button('Add contact', { kind: 'primary', icon: 'plus', onClick: () => openContactModal({ onCreated: c => ctx.navigate(`/app/contacts/${c.id}`) }) }),
    ],
  }), host);

  await call('leads.list');

  async function render() {
    const res = await call('contacts.list', { query: { q: filters.q, email_status: filters.status } });
    if (disposed) return;
    let rows = res.items.slice();
    if (filters.q) {
      const q = filters.q.toLowerCase();
      rows = rows.filter(contact => [contact.name, contact.title, contact.email]
        .some(value => String(value || '').toLowerCase().includes(q)));
    }
    if (filters.status) rows = rows.filter(contact => contact.email_status === filters.status);
    if (filters.country) rows = rows.filter(c => leadFor(c.lead_id)?.country === filters.country);
    if (filters.dnc === 'yes') rows = rows.filter(c => c.do_not_contact);
    if (filters.dnc === 'no') rows = rows.filter(c => !c.do_not_contact);

    const countries = [...new Set(db.contacts.map(c => leadFor(c.lead_id)?.country).filter(Boolean))];
    const chip = (label, on, fn) => el('button', { class: `ifz-filter-chip${on ? ' on' : ''}`, onclick: fn }, label);
    const search = input({ type: 'search', value: filters.q, placeholder: 'Search name or email' });
    search.classList.add('ifz-filter-search');
    search.addEventListener('input', () => { filters.q = search.value; renderSoon(); });

    const filterBar = el('div', { class: 'ifz-filters' },
      search,
      chip('All', !filters.status && !filters.country && !filters.dnc, () => { Object.assign(filters, { status: '', country: '', dnc: '' }); render(); }),
      EMAIL_STATUSES.filter(Boolean).map(s => chip(s.replace(/_/g, ' '), filters.status === s, () => { filters.status = filters.status === s ? '' : s; render(); })),
      el('span', { class: 'ifz-filter-spacer', 'aria-hidden': 'true' }),
      countries.map(c => chip(countryName(c), filters.country === c, () => { filters.country = filters.country === c ? '' : c; render(); })),
      chip('Do-not-contact', filters.dnc === 'yes', () => { filters.dnc = filters.dnc === 'yes' ? '' : 'yes'; render(); }));

    const table = dataTable({
      columns: [
        { key: 'name', label: 'Contact', render: c => recordTitle(c.name, c.title || 'No title') },
        { key: 'company', label: 'Company', render: c => {
          const lead = leadFor(c.lead_id);
          return lead
            ? recordTitle(lead.company_name, `${countryName(lead.country)}${lead.city ? ` / ${lead.city}` : ''}`)
            : el('span', { class: 'cell-muted' }, 'Unlinked');
        } },
        { key: 'email', label: 'Email', render: c => el('div', {},
          el('span', { class: 'ifz-mono' }, c.email || '-'),
          ' ',
          badge(c.email_status)) },
        { key: 'phone', label: 'Phone', render: c => el('span', { class: 'cell-muted' }, c.phone || '-') },
        { key: 'linkedin', label: 'LinkedIn', width: '76px', render: c => c.linkedin_url
          ? el('a', { href: c.linkedin_url, target: '_blank', rel: 'noopener', onclick: e => e.stopPropagation() }, 'Open')
          : el('span', { class: 'cell-muted' }, '-') },
        { key: 'created_at', label: 'Added', render: c => el('span', { class: 'cell-muted ifz-nowrap' }, fmt.ago(c.created_at)) },
      ],
      rows: rows.sort((a, b) => new Date(b.created_at) - new Date(a.created_at)),
      onRowClick: c => ctx.navigate(`/app/contacts/${c.id}`),
      empty: emptyState({
        icon: 'contact',
        title: 'No contacts match',
        hint: 'Find contacts from a lead detail page, or add one manually.',
        action: button('Add contact', { kind: 'primary', icon: 'plus', onClick: () => openContactModal({ onCreated: c => ctx.navigate(`/app/contacts/${c.id}`) }) }),
      }),
    });

    host.replaceChildren(filterBar, card({ flush: true, body: table }),
      el('div', { class: 'ifz-hint ifz-mt-2' }, `${rows.length} contact${rows.length === 1 ? '' : 's'}`));
  }

  let pending = null;
  function renderSoon() {
    if (pending) clearTimeout(pending);
    pending = setTimeout(() => { pending = null; render().catch(console.error); }, 220);
  }

  await render();
  const unsub = subscribe('*', renderSoon);
  return () => { disposed = true; unsub(); if (pending) clearTimeout(pending); };
}

export async function mountDetail(root, ctx) {
  let disposed = false;
  const contactId = ctx.params.contactId;
  const host = el('div', {});
  root.append(host);

  async function render() {
    let contact;
    try {
      contact = await call('contacts.get', { params: { contactId } });
    } catch {
      host.replaceChildren(emptyState({
        icon: 'contact',
        title: 'Contact not found',
        action: button('All contacts', { kind: 'primary', onClick: () => ctx.navigate('/app/contacts') }),
      }));
      return;
    }
    if (disposed) return;
    if (contact.lead_id) {
      try { await call('leads.get', { params: { leadId: contact.lead_id } }); } catch { /* unlinked */ }
    }
    const lead = leadFor(contact.lead_id);
    const [messagesRes, activityRes, linkedinRes] = await Promise.all([
      call('messages.list', { query: { lead_id: contact.lead_id || '' } }),
      call('activity.forContact', { params: { contactId } }),
      call('linkedin.actions'),
    ]);
    if (disposed) return;
    const messages = messagesRes.items.filter(m => !m.contact_id || m.contact_id === contact.id);
    const linkedInActions = linkedinRes.items.filter(a => a.contact_id === contact.id);

    const verifyBtn = button('Verify email', { icon: 'check', onClick: async () => {
      await call('contacts.verify', { params: { contactId } });
      toast('Email verification refreshed', 'success');
      render();
    } });
    const dncBtn = button('Do not contact', { kind: 'danger', icon: 'ban', onClick: async () => {
      await call('contacts.markDoNotContact', { params: { contactId } });
      toast('Contact marked do-not-contact', 'warning');
      render();
    } });
    const findLiBtn = button('Find LinkedIn', { icon: 'linkedin', onClick: async () => {
      const result = await call('linkedin.findProfile', { body: { contact_id: contact.id, lead_id: contact.lead_id } });
      if (config.mode === 'mock') {
        await call('linkedin.generateNote', { body: { action_id: result.id } });
        toast('LinkedIn profile and note prepared', 'success');
      } else {
        toast('LinkedIn research started', 'success', {
          actionLabel: 'Watch', onAction: () => ctx.navigate(`/app/agent-runs/${result.run_id}`),
        });
      }
      render();
    } });
    const leadBtn = lead
      ? button('Open lead', { icon: 'leads', onClick: () => ctx.navigate(`/app/leads/${lead.id}`) })
      : button('Link later', { disabled: true });

    const activity = activityRes.items.map(a => ({
      label: a.label,
      time: fmt.ago(a.at),
      tone: a.kind === 'reply' ? 'success' : a.kind === 'agent' ? 'accent' : '',
    }));

    host.replaceChildren(
      pageHead({
        title: contact.name,
        sub: `${contact.title || 'Buyer contact'}${lead ? ` at ${lead.company_name}` : ''}`,
        actions: [button('Back', { icon: 'arrowLeft', onClick: () => ctx.navigate('/app/contacts') }), leadBtn, verifyBtn, findLiBtn, dncBtn],
      }),
      el('div', { class: 'ifz-grid cols-3 ifz-mb-4' },
        card({ body: el('div', { class: 'ifz-col' },
          el('span', { class: 'ifz-overline' }, 'Email status'),
          badge(contact.do_not_contact ? 'do_not_contact' : contact.email_status),
          el('span', { class: 'ifz-muted ifz-small' }, contact.email || 'No email captured')) }),
        card({ body: el('div', { class: 'ifz-col' },
          el('span', { class: 'ifz-overline' }, 'Market'),
          el('span', { class: 'ifz-strong' }, lead ? countryName(lead.country) : 'Unlinked'),
          el('span', { class: 'ifz-muted ifz-small' }, lead?.city || 'No city')) }),
        card({ body: el('div', { class: 'ifz-col' },
          el('span', { class: 'ifz-overline' }, 'LinkedIn'),
          contact.linkedin_url ? el('a', { href: contact.linkedin_url, target: '_blank', rel: 'noopener' }, 'Open profile') : el('span', { class: 'ifz-muted' }, 'Not found yet'),
          el('span', { class: 'ifz-muted ifz-small' }, `${linkedInActions.length} action${linkedInActions.length === 1 ? '' : 's'}`)) })),
      el('div', { class: 'ifz-grid cols-2' },
        card({
          title: 'Contact profile',
          body: kv([
            ['Name', contact.name],
            ['Title', contact.title],
            ['Email', contact.email],
            ['Phone', contact.phone],
            ['Company', lead?.company_name],
            ['Industry', lead?.industry],
            ['Source', lead?.source?.replace(/_/g, ' ')],
          ]),
        }),
        card({
          title: 'LinkedIn workflow',
          body: linkedInActions.length
            ? el('div', {}, linkedInActions.map(a => el('div', { class: 'ifz-actionrow' },
                el('span', { class: 'ifz-actionrow-icon' }, el('span', {}, 'in')),
                el('div', { class: 'ifz-actionrow-body' },
                  el('div', { class: 'ifz-actionrow-title' }, a.profile_url ? 'Profile found' : 'Profile pending'),
                  el('div', { class: 'ifz-actionrow-sub' }, a.note || 'Generate a compliant manual connection note')),
                badge(a.status),
                a.status !== 'opened' ? button('Opened', { size: 'sm', onClick: async () => { await call('linkedin.markOpened', { params: { actionId: a.id } }); render(); } }) : null,
                a.status !== 'connection_sent' ? button('Sent', { size: 'sm', onClick: async () => { await call('linkedin.markConnectionSent', { params: { actionId: a.id } }); render(); } }) : null)))
            : emptyState({ icon: 'linkedin', title: 'No LinkedIn action yet', hint: 'Find a profile and generate a note for manual sending.' }),
        }),
        card({
          title: `Outreach messages (${messages.length})`,
          body: messages.length
            ? el('div', {}, messages.map(m => el('div', { class: 'ifz-actionrow' },
                el('span', { class: 'ifz-actionrow-icon' }, badge(m.channel === 'whatsapp' ? 'active' : 'draft', m.channel)),
                el('div', { class: 'ifz-actionrow-body' },
                  el('div', { class: 'ifz-actionrow-title' }, m.subject || '(WhatsApp message)'),
                  el('div', { class: 'ifz-actionrow-sub' }, `${m.language?.toUpperCase()} / ${m.sent_at ? fmt.ago(m.sent_at) : 'not sent'}`)),
                badge(m.status))))
            : emptyState({ icon: 'mail', title: 'No outreach yet', hint: 'Generate email from the lead detail or custom outreach.' }),
        }),
        card({
          title: 'Activity',
          body: activity.length ? timeline(activity) : emptyState({ icon: 'clock', title: 'No activity yet' }),
        })),
      messages[0] ? el('div', { class: 'ifz-mt-4' }, card({ title: 'Latest message preview', body: emailPreview(messages[0]) })) : null,
      linkedInActions.length ? el('div', { class: 'ifz-mt-4' }, card({
        title: 'Generated notes',
        body: compactList(linkedInActions.map(a => a.note).filter(Boolean), { empty: 'No note generated yet.' }),
      })) : null);
  }

  await render();
  const unsub = subscribe('*', () => render().catch(console.error));
  return () => { disposed = true; unsub(); };
}
