/* App chrome: sidebar + topbar. Mounted once; pages render into shell.pageRoot. */

import {
  el, icon, button, input, isApprovalActionable, MESSAGE_SUPERSESSION_EVENT,
  runSentence,
} from './ui.js';
import { navigate } from './router.js';
import { call, config } from './api.js';
import { db, resetReal, subscribe } from './mocks/db.js';
import { getSession, clearSession } from './session.js';
import { askHermes, isHermesAvailable } from './hermes-client.js';

/* Phase 5: four customer destinations, no groups. At four items a group heading
   is noise, so the customer list is deliberately flat. Operator machinery —
   run logs, research configuration, data sources — lives under Admin, per
   company-packs/silverline/business-rules.md: supervisors get business reports,
   never technical logs or workflow mechanics. */
const NAV_GROUPS = [
  { label: '', items: [
    { path: '/app/today', label: 'Today', icon: 'dashboard' },
    { path: '/app/approvals', label: 'Approvals', icon: 'check', badgeTopic: 'approvals' },
    { path: '/app/buyers', label: 'Buyers', icon: 'leads' },
    { path: '/app/setup', label: 'Setup', icon: 'gear' },
  ]},
  { label: 'Admin', items: [
    { path: '/admin/dashboard', label: 'Admin Home', icon: 'building' },
    { path: '/admin/companies', label: 'Companies', icon: 'building' },
    { path: '/admin/users', label: 'Users', icon: 'contact' },
    { path: '/admin/research', label: 'Research', icon: 'search' },
    { path: '/admin/data-sources', label: 'Data Sources', icon: 'plug' },
    { path: '/admin/agent-runs', label: 'Agent Runs', icon: 'bolt', badgeTopic: 'runs' },
  ]},
];

const LOGO_MARKS = {
  sidebar: `
    <rect width="32" height="32" rx="4" fill="rgba(255,255,255,0.22)"/>
    <path d="M16 7l7 9-7 9-7-9z" fill="#FFFFFF"/>
    <path d="M16 12l3.5 4.5L16 21l-3.5-4.5z" fill="rgba(255,255,255,0.72)"/>`,
  light: `
    <rect width="32" height="32" rx="4" fill="#1C1B18"/>
    <path d="M16 7l7 9-7 9-7-9z" fill="#FAF9F5"/>
    <path d="M16 12l3.5 4.5L16 21l-3.5-4.5z" fill="#F3F1EB"/>`,
};

function askReviewCount() {
  return (db.messages || []).filter(message => isApprovalActionable(message, {
    lead: (db.leads || []).find(lead => lead.id === message.lead_id),
    contact: (db.contacts || []).find(contact => contact.id === message.contact_id),
  })).length;
}

function askSnapshot() {
  const waiting = askReviewCount();
  const buyers = (db.leads || []).length;
  const contacts = (db.contacts || []).length;
  const sent = (db.messages || []).filter(message =>
    ['sent', 'sent_manually', 'replied'].includes(message.status)).length;
  const replies = (db.messages || []).filter(message =>
    message.status === 'replied' || message.replied_at).length;
  const running = (db.agentRuns || []).find(run => run.status === 'running');
  return [
    '[Read-only workspace snapshot]',
    `Buyers: ${buyers}`,
    `Contacts: ${contacts}`,
    `Emails waiting for review: ${waiting}`,
    `Emails sent: ${sent}`,
    `Replies: ${replies}`,
    `Current work: ${running ? runSentence(running) : 'Nothing is running.'}`,
    'Do not claim to send, approve, edit, schedule, or start anything.',
  ].join('\n');
}

function localAskAnswer(question) {
  const text = question.toLowerCase();
  const waiting = askReviewCount();
  const buyers = (db.leads || []).length;
  const contacts = (db.contacts || []).length;
  const sent = (db.messages || []).filter(message =>
    ['sent', 'sent_manually', 'replied'].includes(message.status)).length;
  const replies = (db.messages || []).filter(message =>
    message.status === 'replied' || message.replied_at).length;
  const running = (db.agentRuns || []).find(run => run.status === 'running');

  if (/approv|review|draft|email/.test(text)) {
    return waiting
      ? `${waiting} email${waiting === 1 ? '' : 's'} ${waiting === 1 ? 'is' : 'are'} waiting in Approvals. I can explain the queue, but I can't approve or send anything.`
      : `No emails are waiting for review. ${sent} email${sent === 1 ? '' : 's'} ${sent === 1 ? 'has' : 'have'} been sent so far.`;
  }
  if (/repl|response/.test(text)) {
    return replies
      ? `${replies} repl${replies === 1 ? 'y has' : 'ies have'} come back so far.`
      : 'No replies have been recorded yet.';
  }
  if (/run|working|happen|doing|progress/.test(text)) {
    return running ? runSentence(running) : 'Nothing is running right now.';
  }
  if (/buyer|lead|contact|pipeline/.test(text)) {
    return `The workspace has ${buyers} buyer${buyers === 1 ? '' : 's'} and ${contacts} contact${contacts === 1 ? '' : 's'}.`;
  }
  return `The workspace has ${buyers} buyer${buyers === 1 ? '' : 's'}, ${waiting} email${waiting === 1 ? '' : 's'} waiting for review, and ${replies} recorded repl${replies === 1 ? 'y' : 'ies'}. I can answer questions about those records, but I can't take actions.`;
}

export function logoNode({ compactTag = false, variant = 'sidebar' } = {}) {
  const mark = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  mark.setAttribute('viewBox', '0 0 32 32');
  mark.setAttribute('width', '26');
  mark.setAttribute('height', '26');
  mark.setAttribute('aria-hidden', 'true');
  mark.innerHTML = LOGO_MARKS[variant] || LOGO_MARKS.sidebar;
  return el('div', { class: `ifz-logo${variant === 'light' ? ' light' : ''}` },
    mark,
    el('span', { class: 'ifz-logo-word' }, 'inter', el('em', {}, 'faze')),
    compactTag ? null : el('span', { class: 'ifz-logo-tag' }, 'agent'));
}

let _shell = null;

export function mountShell(root) {
  if (_shell) return _shell;

  const session = getSession();
  const isAdmin = session?.user?.role === 'admin';
  const visibleGroups = NAV_GROUPS.filter(group => group.label === 'Admin'
    ? isAdmin
    : !isAdmin || Boolean(session?.company?.id));
  const runBadge = el('span', { class: 'ifz-nav-badge', style: { display: 'none' }, 'aria-label': 'Running agents' });
  const approvalBadge = el('span', { class: 'ifz-nav-badge', style: { display: 'none' }, 'aria-label': 'Emails waiting for review' });
  const itemBadge = item => item.badgeTopic === 'runs'
    ? runBadge
    : item.badgeTopic === 'approvals' ? approvalBadge : null;
  const navHost = el('nav', { class: 'ifz-nav', 'aria-label': 'Primary' }, visibleGroups.map(group =>
    // The customer group is unlabelled — four items need no heading.
    [group.label ? el('div', { class: 'ifz-nav-group-label', id: `nav-g-${group.label}` }, group.label) : null,
     group.items.map(item => el('a', {
       class: 'ifz-nav-item',
       href: `#${item.path}`,
       dataset: { path: item.path },
       onclick: () => closeNav(),
       title: item.label,
     }, icon(item.icon, 16), el('span', {}, item.label), itemBadge(item)))]));

  function refreshRunBadge() {
    const running = (db.agentRuns || []).filter(r => r.status === 'running').length;
    runBadge.textContent = running;
    runBadge.style.display = running ? '' : 'none';
    runBadge.setAttribute('aria-label', `${running} agent run${running === 1 ? '' : 's'} in progress`);
  }
  const unsubscribeRunBadge = subscribe('runs', refreshRunBadge);
  refreshRunBadge();

  function setApprovalBadge(count) {
    const waiting = Math.max(0, Number(count) || 0);
    approvalBadge.textContent = waiting;
    approvalBadge.style.display = waiting ? '' : 'none';
    approvalBadge.setAttribute(
      'aria-label',
      `${waiting} email${waiting === 1 ? '' : 's'} waiting for review`,
    );
  }
  function refreshApprovalBadge() {
    const waiting = (db.messages || []).filter(message => isApprovalActionable(message, {
      lead: (db.leads || []).find(lead => lead.id === message.lead_id),
      contact: (db.contacts || []).find(contact => contact.id === message.contact_id),
    })).length;
    setApprovalBadge(waiting);
  }
  const unsubscribeApprovalBadges = [
    subscribe('messages', refreshApprovalBadge),
    subscribe('leads', refreshApprovalBadge),
    subscribe('contacts', refreshApprovalBadge),
  ];
  const onApprovalCount = event => {
    setApprovalBadge(event.detail?.count);
  };
  document.addEventListener('ifz:approval-count', onApprovalCount);
  document.addEventListener(MESSAGE_SUPERSESSION_EVENT, refreshApprovalBadge);
  refreshApprovalBadge();
  if (!isAdmin || session?.company?.id) {
    Promise.allSettled([
      call('messages.list'),
      call('leads.list'),
      call('contacts.list'),
    ]).then(refreshApprovalBadge);
  }

  const sidebar = el('aside', { class: 'ifz-sidebar', id: 'ifz-sidebar', 'aria-label': 'App navigation' },
    logoNode(),
    navHost);

  const titleNode = el('div', { class: 'ifz-topbar-title', id: 'ifz-page-title' }, 'Today');
  const themeBtn = el('button', { class: 'ifz-iconbtn', type: 'button', title: 'Toggle theme', 'aria-label': 'Toggle theme' });
  function paintThemeIcon() {
    themeBtn.replaceChildren(icon(document.documentElement.classList.contains('dark') ? 'sun' : 'moon', 15));
  }
  themeBtn.addEventListener('click', () => {
    const dark = document.documentElement.classList.toggle('dark');
    try { localStorage.setItem('ifz-theme', dark ? 'dark' : 'light'); } catch { /* ignore */ }
    paintThemeIcon();
  });
  paintThemeIcon();

  const askShortcut = /Mac|iPhone|iPad/.test(navigator.platform || '') ? '⌘K' : 'Ctrl K';
  const askTrigger = el('button', {
    class: 'ifz-ask-trigger',
    type: 'button',
    'aria-label': `Ask a question (${askShortcut})`,
    'aria-haspopup': 'dialog',
    'aria-expanded': 'false',
  },
  icon('search', 15),
  el('span', {}, 'Ask'),
  el('kbd', {}, askShortcut));
  let askOverlay = null;
  let askAbort = null;
  let askBusy = false;

  function closeAsk({ restoreFocus = true } = {}) {
    askAbort?.abort();
    askAbort = null;
    askBusy = false;
    askOverlay?.remove();
    askOverlay = null;
    askTrigger.setAttribute('aria-expanded', 'false');
    if (restoreFocus) askTrigger.focus();
  }

  function openAsk() {
    if (askOverlay) {
      askOverlay.querySelector('.ifz-ask-input')?.focus();
      return;
    }

    const feed = el('div', {
      class: 'ifz-ask-feed',
      role: 'log',
      'aria-live': 'polite',
      'aria-label': 'Question and answer history',
    });
    const question = input({
      class: 'ifz-input ifz-ask-input',
      type: 'text',
      placeholder: 'Ask about buyers, emails, replies, or current work…',
      autocomplete: 'off',
      'aria-label': 'Question',
    });
    const send = button('Ask', { kind: 'primary', icon: 'arrowRight' });
    const close = el('button', {
      class: 'ifz-modal-x',
      type: 'button',
      'aria-label': 'Close Ask',
      onclick: () => closeAsk(),
    }, '×');

    function appendMessage(role, text, { streaming = false } = {}) {
      const copy = el('div', { class: 'ifz-ask-message-copy' }, text);
      const message = el('div', {
        class: `ifz-ask-message ${role}${streaming ? ' streaming' : ''}`,
      },
      el('span', { class: 'ifz-ask-message-label' }, role === 'user' ? 'You' : 'Answer'),
      copy);
      feed.append(message);
      feed.scrollTop = feed.scrollHeight;
      return { message, copy };
    }

    appendMessage(
      'assistant',
      'Ask what needs your attention, what has happened, or where a buyer stands.',
    );

    async function submitQuestion(value) {
      const text = value.trim();
      if (!text || askBusy) return;
      askBusy = true;
      question.value = '';
      question.disabled = true;
      send.disabled = true;
      appendMessage('user', text);
      const answer = appendMessage('assistant', 'Looking through your workspace…', {
        streaming: true,
      });

      await Promise.allSettled([
        call('messages.list'),
        call('leads.list'),
        call('contacts.list'),
        call('agentRuns.list'),
      ]);
      if (!askOverlay) return;

      const connected = config.chatEnabled ? await isHermesAvailable() : false;
      if (!askOverlay) return;
      if (!connected) {
        answer.copy.textContent = localAskAnswer(text);
        answer.message.classList.remove('streaming');
        askBusy = false;
        question.disabled = false;
        send.disabled = false;
        question.focus();
        return;
      }

      askAbort = new AbortController();
      try {
        const response = await askHermes(text, {
          context: askSnapshot(),
          signal: askAbort.signal,
          onToken: (fullText) => {
            if (!askOverlay) return;
            answer.copy.textContent = fullText || 'Looking through your workspace…';
            feed.scrollTop = feed.scrollHeight;
          },
        });
        if (askOverlay) {
          answer.copy.textContent = response || "I couldn't find an answer in this workspace.";
        }
      } catch (error) {
        if (askOverlay && error?.message !== 'aborted') {
          answer.copy.textContent = "I couldn't answer that right now. Try again in a moment.";
        }
      } finally {
        if (askOverlay) {
          answer.message.classList.remove('streaming');
          question.disabled = false;
          send.disabled = false;
          question.focus();
        }
        askAbort = null;
        askBusy = false;
      }
    }

    const form = el('form', { class: 'ifz-ask-form' },
      question,
      send);
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      submitQuestion(question.value);
    });
    send.addEventListener('click', () => submitQuestion(question.value));

    const suggestions = el('div', { class: 'ifz-ask-suggestions' },
      ['What needs my attention?', 'What is happening now?', 'How many buyers have replied?']
        .map(label => button(label, {
          size: 'sm',
          onClick: () => submitQuestion(label),
        })));

    const panel = el('section', {
      class: 'ifz-ask-panel',
      role: 'dialog',
      'aria-modal': 'true',
      'aria-labelledby': 'ifz-ask-title',
      'aria-describedby': 'ifz-ask-subtitle',
    },
    el('header', { class: 'ifz-ask-head' },
      el('div', {},
        el('h2', { id: 'ifz-ask-title' }, 'Ask a question'),
        el('p', { id: 'ifz-ask-subtitle' },
          "I can answer questions about your workspace. I can't send anything.")),
      close),
    feed,
    suggestions,
    form);

    askOverlay = el('div', { class: 'ifz-overlay ifz-ask-overlay' }, panel);
    askOverlay.addEventListener('mousedown', event => {
      if (event.target === askOverlay) closeAsk();
    });
    askOverlay.addEventListener('keydown', event => {
      if (event.key !== 'Tab') return;
      const focusable = [...askOverlay.querySelectorAll(
        'button:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
      )];
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });
    document.body.append(askOverlay);
    askTrigger.setAttribute('aria-expanded', 'true');
    requestAnimationFrame(() => question.focus());
  }
  askTrigger.addEventListener('click', openAsk);

  const userName = session?.user?.name || 'User';
  const initials = userName.split(' ').map(w => w[0]).slice(0, 2).join('').toUpperCase();

  const menuHost = el('div', { class: 'ifz-usermenu' });
  const avatarBtn = el('button', {
    class: 'ifz-iconbtn',
    type: 'button',
    style: { border: 'none', background: 'transparent' },
    'aria-label': 'User menu',
    'aria-haspopup': 'menu',
    'aria-expanded': 'false',
  }, el('span', { class: 'ifz-avatar', 'aria-hidden': 'true' }, initials));
  let pop = null;
  function closeMenu() {
    if (pop) {
      pop.remove();
      pop = null;
      avatarBtn.setAttribute('aria-expanded', 'false');
      document.removeEventListener('mousedown', onDoc);
      document.removeEventListener('keydown', onMenuKey);
    }
  }
  function onDoc(e) { if (!menuHost.contains(e.target)) closeMenu(); }
  function onMenuKey(e) {
    if (e.key === 'Escape') {
      closeMenu();
      avatarBtn.focus();
    }
  }
  avatarBtn.addEventListener('click', () => {
    if (pop) { closeMenu(); return; }
    pop = el('div', { class: 'ifz-usermenu-pop', role: 'menu' },
      el('div', { class: 'ifz-usermenu-head' },
        el('div', { class: 'ifz-usermenu-name' }, userName),
        el('div', { class: 'ifz-usermenu-email' }, session?.user?.email || '')),
      el('button', {
        class: 'ifz-usermenu-item',
        type: 'button',
        role: 'menuitem',
        onclick: () => { closeMenu(); navigate('/app/setup'); },
      }, icon('gear', 14), 'Settings'),
      el('button', {
        class: 'ifz-usermenu-item danger',
        type: 'button',
        role: 'menuitem',
        onclick: async () => {
          closeMenu();
          if (!window.confirm('Log out of interfaze-agent?')) return;
          try { await call('auth.logout'); } catch { /* local session is cleared regardless */ }
          clearSession();
          resetReal();
          navigate('/login');
        },
      }, icon('logout', 14), 'Log out'));
    menuHost.append(pop);
    avatarBtn.setAttribute('aria-expanded', 'true');
    document.addEventListener('mousedown', onDoc);
    document.addEventListener('keydown', onMenuKey);
    pop.querySelector('button')?.focus();
  });
  menuHost.append(avatarBtn);

  const menuBtn = el('button', {
    class: 'ifz-iconbtn ifz-menu-btn',
    type: 'button',
    'aria-label': 'Open navigation',
    'aria-controls': 'ifz-sidebar',
    'aria-expanded': 'false',
  }, icon('menu', 18));

  const mq = window.matchMedia('(min-width: 901px)');
  let appFrame = null;
  function setNavOpen(open, { restoreFocus = false } = {}) {
    if (!appFrame) return;
    const drawerOpen = !mq.matches && Boolean(open);
    appFrame.classList.toggle('nav-open', drawerOpen);
    menuBtn.setAttribute('aria-expanded', drawerOpen ? 'true' : 'false');
    menuBtn.setAttribute('aria-label', drawerOpen ? 'Close navigation' : 'Open navigation');
    document.body.style.overflow = drawerOpen ? 'hidden' : '';
    sidebar.inert = !mq.matches && !drawerOpen;
    if (sidebar.inert) sidebar.setAttribute('aria-hidden', 'true');
    else sidebar.removeAttribute('aria-hidden');
    if (drawerOpen) {
      requestAnimationFrame(() => {
        sidebar.querySelector('.ifz-nav-item[aria-current="page"], .ifz-nav-item')?.focus();
      });
    } else if (restoreFocus) {
      menuBtn.focus();
    }
  }
  function closeNav(restoreFocus = false) { setNavOpen(false, { restoreFocus }); }
  function toggleNav() { setNavOpen(!appFrame?.classList.contains('nav-open')); }
  menuBtn.addEventListener('click', toggleNav);

  const scrim = el('button', {
    class: 'ifz-app-scrim',
    type: 'button',
    'aria-label': 'Close navigation',
    onclick: () => closeNav(true),
  });

  const initialCompanyName = session?.company?.name || db.company?.name || '';
  const companyAvatar = el('span', { class: 'ifz-avatar', 'aria-hidden': 'true' }, initialCompanyName.slice(0, 1).toUpperCase() || '—');
  const companyNameNode = el('span', {}, initialCompanyName || (isAdmin ? 'No workspace selected' : 'Workspace'));
  const unsubscribeCompany = subscribe('company', company => {
    const name = company?.name || getSession()?.company?.name || '';
    companyAvatar.textContent = name.slice(0, 1).toUpperCase() || '—';
    companyNameNode.textContent = name || (isAdmin ? 'No workspace selected' : 'Workspace');
  });

  const topbar = el('header', { class: 'ifz-topbar' },
    menuBtn,
    titleNode,
    el('div', { class: 'ifz-topbar-spacer' }),
    askTrigger,
    el('span', { class: 'ifz-company-chip' },
      companyAvatar,
      companyNameNode),
    themeBtn,
    menuHost);

  const pageRoot = el('div', { class: 'ifz-page', id: 'ifz-main', tabindex: '-1' });
  const main = el('main', { class: 'ifz-main' }, topbar, pageRoot);

  appFrame = el('div', { class: 'ifz-app' }, scrim, sidebar, main);
  root.replaceChildren(appFrame);
  setNavOpen(false);

  const onShellKeydown = (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      openAsk();
      return;
    }
    if (e.key === 'Escape' && askOverlay) {
      closeAsk();
      return;
    }
    if (e.key === 'Escape' && appFrame.classList.contains('nav-open')) closeNav(true);
  };
  document.addEventListener('keydown', onShellKeydown);

  // Close drawer when resizing to desktop
  const onMq = () => setNavOpen(false);
  mq.addEventListener?.('change', onMq);

  _shell = {
    pageRoot,
    setTitle(t) {
      titleNode.textContent = t;
      document.title = `${t} · interfaze-agent`;
    },
    setActiveNav(path) {
      navHost.querySelectorAll('.ifz-nav-item').forEach(btn => {
        const p = btn.dataset.path;
        const active = p === path || (p !== '/app/today' && path.startsWith(p));
        btn.classList.toggle('active', active);
        if (active) btn.setAttribute('aria-current', 'page');
        else btn.removeAttribute('aria-current');
      });
    },
    closeNav,
    destroy() {
      unsubscribeRunBadge();
      unsubscribeApprovalBadges.forEach(unsubscribe => unsubscribe());
      unsubscribeCompany();
      document.removeEventListener('ifz:approval-count', onApprovalCount);
      document.removeEventListener(MESSAGE_SUPERSESSION_EVENT, refreshApprovalBadge);
      document.removeEventListener('keydown', onShellKeydown);
      mq.removeEventListener?.('change', onMq);
      closeAsk({ restoreFocus: false });
      closeMenu();
    },
  };
  return _shell;
}

export function destroyShell() {
  _shell?.destroy?.();
  _shell = null;
  document.body.style.overflow = '';
}
